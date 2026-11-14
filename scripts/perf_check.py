#!/usr/bin/env python3
"""RESOLVE performance baseline — host-side HTTP journey measurement.

Standard library only (host Python 3.10 has no project deps). Drives ONE honest
journey against a running backend through its real endpoints and reports per-operation
latency. No payment success is ever fabricated: the Razorpay TEST checkout popup is
interactive and not drivable headlessly, and the checkout signature is a credential the
script does not hold — so payment/verify is measured on its honest refusal path
(an invalid signature pair, expected 400, mission settles to payment_failed).

Records created are real API-created records in the dev database, clearly labeled:
  user   "PERF RUN (automated baseline)"  email perf+<timestamp>@resolve-perf.local
  missions "PERF <timestamp>: ..." prompts

NOT measured here (payment_verified-gated live): issue creation, assessment,
evidence upload/retrieval, refund execution. Those component costs are measured
separately by scripts/perf_bench_incontainer.py inside the backend image context.

Usage:
  python scripts/perf_check.py [--base http://localhost:8000] [--out DIR]
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any

DEFAULT_BASE = os.environ.get("RESOLVE_PERF_BASE_URL", "http://localhost:8000")

# Component tags used to split the report the way §7 requires.
COMPONENT = {
    "health": "infra",
    "root": "infra",
    "register": "backend_db",
    "login": "backend_db",
    "auth_me": "backend_db",
    "mission_create_discovery": "external_provider+model",  # SerpApi + reasoning model
    "agent_turn": "model",  # reasoning loop
    "select_product": "backend_db",
    "checkout_get": "backend_db",
    "checkout_confirm": "backend_db",
    "payment_create_order": "external_provider",  # Razorpay TEST order create
    "authority_delegate": "backend_db",
    "payment_verify_invalid": "refusal_path",  # expected 400, honest refusal
    "mission_get": "backend_db",
    "missions_list": "backend_db",
    "agent_conversation_get": "backend_db",
}

# Provider-paced operations: repeat count kept small deliberately.
REPEATS = {
    "mission_create_discovery": 2,
    "agent_turn": 1,
    "payment_create_order": 2,  # second call reuses the pending order, no 2nd Razorpay call
    "checkout_confirm": 2,  # idempotent re-confirm
    "checkout_get": 3,
    "authority_delegate": 2,
    "payment_verify_invalid": 1,  # settles mission to payment_failed; cannot repeat
    "register": 1,
    "login": 3,
    "auth_me": 5,
    "mission_get": 5,
    "missions_list": 5,
    "agent_conversation_get": 3,
}


class PerfClient:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any, float]:
        """One timed call. Returns (status, parsed-json-or-text, elapsed_ms)."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        start = time.perf_counter()
        try:
            with self.opener.open(req, timeout=180) as res:
                raw = res.read()
                status = res.status
        except urllib.error.HTTPError as exc:  # 4xx/5xx — still a measured response
            raw = exc.read()
            status = exc.code
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            parsed = raw.decode("utf-8", "replace")[:500]
        return status, parsed, elapsed_ms


def summarize(samples: list[float]) -> dict:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, max(0, int(round(0.95 * len(ordered))) - 1))]
    return {
        "n": len(ordered),
        "min_ms": round(ordered[0], 1),
        "median_ms": round(median(ordered), 1),
        "mean_ms": round(mean(ordered), 1),
        "p95_ms": round(p95, 1),
        "max_ms": round(ordered[-1], 1),
    }


def run_journey(base: str) -> dict:
    client = PerfClient(base)
    results: dict[str, dict] = {}
    journey_total_ms = 0.0

    def measure(name: str, method: str, path: str, body: dict | None = None,
                expect_status: int | None = None) -> Any:
        nonlocal journey_total_ms
        reps = REPEATS.get(name, 1)
        samples: list[float] = []
        statuses: list[int] = []
        last_payload: Any = None
        for _ in range(reps):
            status, payload, ms = client.call(method, path, body)
            samples.append(ms)
            statuses.append(status)
            last_payload = payload
        results[name] = {
            "component": COMPONENT[name],
            "path": path,
            "method": method,
            "statuses": statuses,
            **summarize(samples),
        }
        if expect_status is not None and statuses[-1] != expect_status:
            results[name]["UNEXPECTED_STATUS"] = True
        journey_total_ms += samples[0]  # one pass contributes to the end-to-end figure
        return last_payload

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    perf_email = f"perf+{stamp.lower()}@resolve-perf.local"
    perf_password = secrets.token_hex(16)  # never printed, never logged

    # -- infra / auth --------------------------------------------------------
    measure("health", "GET", "/health")
    measure("root", "GET", "/")
    reg = measure("register", "POST", "/auth/register",
                  {"name": "PERF RUN (automated baseline)", "email": perf_email,
                   "password": perf_password}, expect_status=200)
    measure("login", "POST", "/auth/login", {"email": perf_email, "password": perf_password})
    measure("auth_me", "GET", "/auth/me")

    # -- discovery + model ---------------------------------------------------
    mission_payloads = []
    for i in range(REPEATS["mission_create_discovery"]):
        status, payload, ms = client.call("POST", "/missions/shopping", {
            "prompt": f"PERF {stamp} run {i}: find wireless headphones under Rs 1000"
        })
        mission_payloads.append((status, payload, ms))
        results.setdefault("mission_create_discovery", {
            "component": COMPONENT["mission_create_discovery"],
            "path": "/missions/shopping", "method": "POST", "statuses": [], "samples": [],
        })
        results["mission_create_discovery"]["statuses"].append(status)
        results["mission_create_discovery"].setdefault("samples", []).append(ms)
        if results["mission_create_discovery"].get("n"):
            pass
    # normalize the ad-hoc entry into the standard shape
    mc = results["mission_create_discovery"]
    if "samples" in mc:
        mc.update(summarize(mc.pop("samples")))
    journey_total_ms += mission_payloads[0][2]
    if mission_payloads[0][0] != 200:
        print(f"FATAL: shopping mission creation failed: {mission_payloads[0]}", file=sys.stderr)
        return {"error": "mission creation failed", "results": results}

    mission_id = mission_payloads[0][1].get("mission_id")
    discovered = ((mission_payloads[0][1].get("shopping_intent") or {}).get("discovered_products")) or []
    product_id = discovered[0].get("product_id") if discovered else None

    # -- one agent turn -------------------------------------------------------
    measure("agent_turn", "POST", f"/missions/{mission_id}/agent",
            {"user_input": "Which of these offers the best value for money?"})

    # -- selection, checkout, payment boundary --------------------------------
    if product_id:
        measure("select_product", "POST", f"/missions/{mission_id}/shopping/select-product",
                {"product_id": product_id})
        measure("checkout_get", "GET", f"/missions/{mission_id}/shopping/checkout")
        measure("checkout_confirm", "POST", f"/missions/{mission_id}/shopping/checkout/confirm", {})
        measure("payment_create_order", "POST",
                f"/missions/{mission_id}/shopping/payment/create-order", {})
        measure("authority_delegate", "POST", f"/missions/{mission_id}/authority/delegate",
                {"allowed_actions": ["request_refund", "request_more_information"]})
        order = results["payment_create_order"]
        measure("payment_verify_invalid", "POST",
                f"/missions/{mission_id}/shopping/payment/verify", {
                    "razorpay_order_id": "order_PERF_INVALID",
                    "razorpay_payment_id": "pay_PERF_INVALID",
                    "razorpay_signature": "deliberately-invalid-signature-not-a-credential",
                }, expect_status=400)
    else:
        results["SKIPPED_SELECTION_TO_PAYMENT"] = (
            "discovery returned no products; selection->payment ops not run this pass"
        )

    # -- read paths -----------------------------------------------------------
    measure("mission_get", "GET", f"/missions/{mission_id}")
    measure("missions_list", "GET", "/missions")
    measure("agent_conversation_get", "GET", f"/missions/{mission_id}/agent/conversation?limit=200")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base,
        "perf_user_email": perf_email,
        "mission_id": mission_id,
        "discovered_products_count": len(discovered),
        "journey_total_ms_one_pass_register_to_verify": round(journey_total_ms, 1),
        "honest_boundaries": [
            "payment verification is measured on its refusal path (invalid signature, 400): "
            "the Razorpay TEST checkout popup is interactive and the HMAC secret is never held "
            "by this script, so a verified payment cannot be fabricated",
            "issue creation, assessment, evidence upload/retrieval and refund execution are "
            "payment_verified-gated and are measured at component level by "
            "scripts/perf_bench_incontainer.py instead",
        ],
        "operations": results,
    }
    return report


def render_markdown(report: dict) -> str:
    lines = [
        "# RESOLVE performance baseline (host-side HTTP journey)",
        "",
        f"- generated: {report['generated_at']}",
        f"- base: {report['base_url']}",
        f"- perf mission: `{report.get('mission_id')}`",
        f"- one-pass journey total (register .. payment-verify refusal): "
        f"{report.get('journey_total_ms_one_pass_register_to_verify')} ms",
        "",
        "| operation | component | method path | n | median ms | mean ms | p95 ms | max ms | status |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, op in report["operations"].items():
        if not isinstance(op, dict) or "median_ms" not in op:
            continue
        path = f"{op['method']} {op['path']}"
        lines.append(
            f"| {name} | {op['component']} | {path} | {op['n']} | {op['median_ms']} | "
            f"{op['mean_ms']} | {op['p95_ms']} | {op['max_ms']} | {op['statuses']} |"
        )
    if report["operations"].get("SKIPPED_SELECTION_TO_PAYMENT"):
        lines.append("")
        lines.append(f"> {report['operations']['SKIPPED_SELECTION_TO_PAYMENT']}")
    lines += ["", "## Honest boundaries", ""]
    for b in report["honest_boundaries"]:
        lines.append(f"- {b}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    report = run_journey(args.base)
    if "error" in report:
        print(json.dumps(report, indent=2))
        return 1

    out_dir = args.out or os.path.join(
        os.environ.get("TEMP", "/tmp"), "resolve-perf",
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    )
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "perf_baseline_http.json")
    md_path = os.path.join(out_dir, "perf_baseline_http.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))

    print(render_markdown(report))
    print(f"artifacts: {json_path} , {md_path}")
    bad = [n for n, op in report["operations"].items()
           if isinstance(op, dict) and op.get("UNEXPECTED_STATUS")]
    if bad:
        print(f"UNEXPECTED STATUS on: {bad}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
