#!/usr/bin/env python3
"""RESOLVE component-level performance benchmark — runs INSIDE the backend image context.

Invoked via:
  MSYS_NO_PATHCONV=1 docker compose run --rm \
    -v "D:/RAZORPAY/resolve/scripts:/perf-scripts:ro" \
    backend python /perf-scripts/perf_bench_incontainer.py

Measures component costs that the host-side HTTP journey cannot reach because they sit
behind the payment_verified gate, plus the pure-CPU and database floors:

  1. Authority Engine evaluate()  — pure deterministic Python, no I/O (the authorization floor)
  2. Database SELECT 1 round-trip through the app's own async engine
  3. bcrypt password verification (auth floor)
  4. Evidence store round-trip (put / get / delete of THIS script's own scratch object
     under perf-scratch/, deleted immediately after; no existing evidence is touched,
     read, or modified)

Prints one JSON document to stdout. Redirect it on the host to keep the artifact.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import statistics
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

# Running the script by absolute path puts /perf-scripts on sys.path, not the image's
# app root — restore the app package for the imports below.
_APP_ROOT = os.environ.get("RESOLVE_APP_ROOT", "/app")
if os.path.isdir(_APP_ROOT) and _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)


def _summarize(samples: list[float]) -> dict:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, max(0, int(round(0.95 * len(ordered))) - 1))]
    return {
        "n": len(ordered),
        "min_ms": round(ordered[0], 3),
        "median_ms": round(statistics.median(ordered), 3),
        "mean_ms": round(statistics.mean(ordered), 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(ordered[-1], 3),
    }


def bench_authority_engine(iterations: int = 3000) -> dict:
    from app.authority.engine import ProposedAction, evaluate
    from app.models.mission import AllowedAction, Authority

    authority = Authority(
        allowed_actions=[AllowedAction.REQUEST_REFUND, AllowedAction.REQUEST_MORE_INFORMATION],
        recovery_budget=Decimal("2000"),
        refund_limit=Decimal("2000"),
    )
    proposal = ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=Decimal("799"))

    # one warm-up evaluation so first-call import/caching cost does not pollute samples
    decision = evaluate(authority, proposal)
    if decision.decision.value != "approved":
        decision = None  # recorded honestly below; the bench does not assert policy

    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        evaluate(authority, proposal)
        samples.append((time.perf_counter() - start) * 1000.0)
    return {
        "authority_engine_evaluate": {
            **_summarize(samples),
            "iterations": iterations,
            "warmup_decision": decision.decision.value if decision else "not-approved",
            "note": "pure synchronous Python, zero I/O — the authorization-time floor",
        }
    }


async def bench_database(rounds: int = 300) -> dict:
    from sqlalchemy import text

    from app.database import async_session

    samples: list[float] = []
    async with async_session() as session:
        await session.execute(text("SELECT 1"))  # warm-up (pool + connection)
        for _ in range(rounds):
            start = time.perf_counter()
            await session.execute(text("SELECT 1"))
            samples.append((time.perf_counter() - start) * 1000.0)
    return {
        "db_select_1": {
            **_summarize(samples),
            "rounds": rounds,
            "note": "application async engine, pooled connection — the DB floor",
        }
    }


def bench_bcrypt(rounds: int = 20) -> dict:
    import bcrypt

    password = b"perf-bench-password-not-a-credential"
    hashed = bcrypt.hashpw(password, bcrypt.gensalt(rounds=12))
    samples: list[float] = []
    for _ in range(rounds):
        start = time.perf_counter()
        bcrypt.checkpw(password, hashed)
        samples.append((time.perf_counter() - start) * 1000.0)
    return {
        "bcrypt_verify_cost12": {
            **_summarize(samples),
            "rounds": rounds,
            "note": "login-path password check floor",
        }
    }


async def bench_evidence_store(rounds: int = 10, size_bytes: int = 1024 * 1024) -> dict:
    from app.config import get_settings
    from app.evidence_store import EvidenceObjectStore

    settings = get_settings()
    if not (
        settings.evidence_object_store_endpoint
        and settings.evidence_object_store_bucket
        and settings.evidence_object_store_access_key
        and settings.evidence_object_store_secret_key
    ):
        return {
            "evidence_store_roundtrip": {
                "status": "STORE_DISABLED",
                "note": "evidence endpoint/bucket/credentials are not fully configured in "
                        "this context — honest absence, not a measurement of zero",
            }
        }
    store = EvidenceObjectStore(
        endpoint_url=settings.evidence_object_store_endpoint,
        region=settings.evidence_object_store_region,
        bucket=settings.evidence_object_store_bucket,
        access_key=settings.evidence_object_store_access_key,
        secret_key=settings.evidence_object_store_secret_key,
        timeout_seconds=settings.evidence_object_store_timeout_seconds,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"perf-scratch/{stamp}/{secrets.token_hex(8)}.bin"
    payload = secrets.token_bytes(size_bytes)

    put_samples, get_samples, delete_samples = [], [], []
    total_start = time.perf_counter()
    for _ in range(rounds):
        start = time.perf_counter()
        result = await store.put_object(key, payload, "application/octet-stream")
        put_samples.append((time.perf_counter() - start) * 1000.0)

        start = time.perf_counter()
        obj = await store.get_object(key)
        get_samples.append((time.perf_counter() - start) * 1000.0)
        byte_match = obj.body == payload

        start = time.perf_counter()
        await store.delete_object(key)
        delete_samples.append((time.perf_counter() - start) * 1000.0)

    total_ms = (time.perf_counter() - total_start) * 1000.0
    return {
        "evidence_store_roundtrip": {
            "status": "MEASURED",
            "key": key,
            "size_bytes": size_bytes,
            "rounds": rounds,
            "put": _summarize(put_samples),
            "get": _summarize(get_samples),
            "delete": _summarize(delete_samples),
            "total_wall_ms": round(total_ms, 1),
            "bytes_roundtrip_intact": byte_match,
            "note": "scratch object under perf-scratch/ only; deleted after each round; "
                    "no pre-existing evidence object read or modified",
        }
    }


def main() -> None:
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "context": "backend image, compose run (same network + env as the running service)",
    }
    report.update(bench_authority_engine())
    report.update(bench_bcrypt())
    report.update(asyncio.run(bench_database()))
    report.update(asyncio.run(bench_evidence_store()))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
