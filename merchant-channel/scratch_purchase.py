"""
The gated scratch purchase runner: the only sanctioned way the first live purchase happens.

What this is: a single-shot operator tool that walks the already-built channel through one
purchase — and refuses to reach ``PurchaseChannel.fulfil`` unless every gate passes first.
It adds no channel behavior of its own: no direct Shopify GraphQL, no second correlation
scheme, no reconcile, no reclaim, no retry. Everything it does, it does through the existing
interfaces (``load_config``, ``Outbox``, ``ShopifyAdminGateway``, ``PurchaseChannel.fulfil``).
The gates themselves are shared code: ``merchant_channel.gates`` owns the single
implementation of the trust boundary, and this runner supplies its scratch pins (expected
shop name, store domain, currency, callback base url) as the gate functions' parameter
values rather than any copy of the rules.

The order-source trust boundary — why the GIDs are never a command-line argument:

    A handoff's product/variant GIDs are only as trustworthy as their source. Caller-supplied
    GIDs — typed, pasted, or copied out of a psql row — are exactly how a runner would create a
    merchant order for a purchase RESOLVE never selected. So this runner does not accept GIDs
    from its caller at all. It reads them from RESOLVE's own API, which serves the persisted
    rows the application itself wrote:

        GET /missions/{id}/shopping/checkout -> shopping_intent.selected_product
            shopify_product_gid / shopify_variant_gid / shopify_store_domain — written once,
            server-side, from the catalog read at selection; no client can supply them.
        GET /missions/{id}/order -> order_id, status, currency, razorpay_order_id +
            razorpay_payment_id (copied onto the row ONLY by the payment-verify endpoint after
            server-side HMAC verification — the order-create endpoint refuses without them),
            and merchant_order_linked (linkage evidence, never payment evidence).

    The runner then cross-checks the two views (same mission, same product identity) before
    anything else happens. A "paid" boolean is not evidence; the server-copied payment
    references and a post-payment status are.

Dry run is the default. ``--confirm`` is the only way a Shopify mutation becomes possible, and
even then only after every gate passes: the channel object is constructed in the confirm
branch and nowhere else, and the handoff is built once, from the served facts, and never
re-derived. Exit codes: 0 — dry run clean, or CONFIRMED_LINKED; 1 — any gate refused; 2 — a
confirmed run finished with a non-confirmed outcome. Nothing here prints a credential, a
token, a signature, or an environment value, and no failure path retries, reconciles or
reclaims: those are separate, deliberate steps outside this tool.

The whole run holds an exclusive, OS-level lock (one per outbox directory) from before the
preflight until exit: a second simultaneous run — a different mission, or a different outbox
name in the same directory — refuses instead of racing the first toward a create, and a lock
left behind by a dead run is removed by an operator, never by this tool. Quantity is pinned
to exactly one unit; no flag or caller can raise it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from merchant_channel.channel import (
    CONFIRMED_LINKED,
    PurchaseChannel,
    PurchaseHandoff,
)
from merchant_channel.config import ChannelConfig, ChannelConfigError, load_config
from merchant_channel.correlation import (
    correlation_id as derive_correlation,
    payload_hash as derive_payload_hash,
)
from merchant_channel.gates import (
    GateReport,
    OrderFacts,
    fetch_order_facts,
    order_gates,
    outbox_state_gates,
)
from merchant_channel.gateway import ShopifyAdminGateway
from merchant_channel.outbox import Outbox

EXPECTED_SHOP_NAME = "RESOLVE Merchant Test"
EXPECTED_CURRENCY = "INR"
EXPECTED_STORE_DOMAIN = "resolve-merchant-test.myshopify.com"
EXPECTED_BASE_URL = "http://host.docker.internal:8000"
# One scratch run at a time per outbox directory. The name is fixed and the location is the
# outbox's own (resolved) parent, so a different mission, a different outbox file name, or a
# relative spelling of the same directory cannot take a second run past the lock. The directory
# is used because the outbox-path gate has already proven it exists and is writable.
LOCK_NAME = ".scratch-purchase.lock"


# --------------------------------------------------------------------- gates

def config_gates(env) -> tuple[ChannelConfig | None, GateReport]:
    """Load the channel's own configuration and pin the scratch run's semantics. Fail closed."""
    report = GateReport()
    try:
        config = load_config(env)
        report.ok("config loaded from MERCHANT_* values (no RESOLVE-owned name present)")
    except ChannelConfigError as exc:
        report.fail(f"config refused: {exc}")
        return None, report
    if config.configured:
        report.ok("config is fully configured (store, linkage secret, base url, credential)")
    else:
        report.fail(
            "config is not fully configured: store domain, linkage secret, base url and a "
            "Shopify credential path are all required before anything may be created"
        )
    if config.api_version:
        report.ok("Shopify API version is set")
    else:
        report.fail("MERCHANT_SHOPIFY_API_VERSION is required and was empty")
    if config.expected_shop_name == EXPECTED_SHOP_NAME:
        report.ok("expected shop name pins the scratch store")
    else:
        report.fail(
            f"MERCHANT_EXPECTED_SHOP_NAME must be exactly {EXPECTED_SHOP_NAME!r} for the scratch "
            f"run (got {'unset' if not config.expected_shop_name else 'a different name'}); without "
            "it the channel's shop-identity guard would not bite"
        )
    if config.expected_currency == EXPECTED_CURRENCY:
        report.ok("expected currency is INR (nothing converts, ever)")
    else:
        report.fail(f"MERCHANT_EXPECTED_CURRENCY must be exactly {EXPECTED_CURRENCY!r}")
    if config.resolve_base_url == EXPECTED_BASE_URL:
        report.ok("callback base url is the verified Docker-host address")
    else:
        report.fail(
            f"MERCHANT_RESOLVE_BASE_URL must be exactly {EXPECTED_BASE_URL!r} for the scratch run"
        )
    try:
        mode = config.credential_mode()
        report.ok(f"Shopify credential path valid ({mode})")
    except ChannelConfigError as exc:
        report.fail(f"no usable Shopify credential: {exc}")
    return config, report


def outbox_path_gate(path: Path) -> GateReport:
    """The outbox is the memory that keeps a retry from becoming a second order."""
    report = GateReport()
    if path.is_dir():
        report.fail(f"the outbox path is a directory: {path}")
        return report
    parent = path.parent
    if not parent.is_dir():
        report.fail(f"the outbox directory does not exist: {parent}")
        return report
    probe = parent / f".{path.name}.writetest"
    try:
        with open(probe, "w") as handle:
            handle.write("")
        probe.unlink()
        report.ok("outbox location exists and is writable")
    except OSError as exc:
        report.fail(f"the outbox location is not writable ({type(exc).__name__})")
    return report


@dataclass(frozen=True)
class RunLock:
    """One acquired run lock: the file, and the token that proves this run still owns it."""

    path: Path
    token: str


def _run_lock_path(outbox_path: Path) -> Path:
    # Resolved: two spellings of the same directory must name the same lock, not two.
    return outbox_path.resolve().parent / LOCK_NAME


def _held_summary(path: Path) -> str:
    """Who appears to hold the lock — context for a refusal message, never proof of anything."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "holder details unreadable"
    fields = {}
    for line in content.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    return (
        f"pid {fields.get('pid', 'unknown')}, acquired {fields.get('acquired_at', 'unknown time')}, "
        f"mission {fields.get('mission', 'unknown')}"
    )


def acquire_run_lock(outbox_path: Path, *, mission_id: str) -> tuple[RunLock | None, str | None]:
    """
    Take the exclusive run lock for this outbox directory, or refuse with operator guidance.

    The lock is an OS-level exclusive create (``open(..., "x")`` is O_CREAT|O_EXCL): exactly one
    process on this machine can win it, and a loser is told, never queued. It is keyed to the
    resolved directory and to nothing else — a different mission, or a different outbox file name
    in the same directory, cannot silently take a second run past it. A run from a *different
    outbox directory* is not covered by this lock, so the outbox location is part of the run's
    identity and must be kept the same across runs.

    The winner writes its pid, mission, time, and a random token into the file; the token is how
    release avoids deleting a lock that is no longer ours. There is deliberately no staleness
    logic: a lock left behind by a crashed run is never removed automatically, because "looks
    old" is not "owner is gone". The operator verifies no run is active, then removes it by hand.
    """
    path = _run_lock_path(outbox_path)
    token = uuid.uuid4().hex
    try:
        handle = open(path, "x", encoding="utf-8")
    except FileExistsError:
        return None, (
            f"another scratch purchase run already holds the exclusive run lock at {path} "
            f"({_held_summary(path)}). This run did not start. The lock is never removed "
            "automatically: if no scratch run is actually active, verify that, then delete the "
            "lock file by hand and rerun."
        )
    except OSError as exc:
        return None, (
            f"the exclusive run lock at {path} could not be created ({type(exc).__name__}); "
            "failing closed rather than running unlocked"
        )
    try:
        handle.write(
            f"pid={os.getpid()}\n"
            f"token={token}\n"
            f"mission={mission_id}\n"
            f"acquired_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        )
    finally:
        handle.close()
    return RunLock(path=path, token=token), None


def release_run_lock(lock: RunLock) -> str | None:
    """
    Remove the lock only if it is still exactly the one this run created.

    A file that has gone, or whose content no longer carries this run's token, is not ours to
    delete: unlinking by path alone could remove a lock another process now owns. Either anomaly
    is returned so the run can report it, and the file is left for the operator.
    """
    try:
        content = lock.path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"the run lock at {lock.path} was already gone at release; nothing was deleted"
    except OSError as exc:
        return (
            f"the run lock at {lock.path} could not be read at release ({type(exc).__name__}); "
            "it was not deleted"
        )
    if f"token={lock.token}" not in content.splitlines():
        return (
            f"the run lock at {lock.path} is no longer owned by this run, so it was not deleted; "
            "resolve it by hand before the next scratch run"
        )
    try:
        lock.path.unlink()
    except OSError as exc:
        return (
            f"the run lock at {lock.path} could not be removed ({type(exc).__name__}); remove it "
            "by hand before the next scratch run"
        )
    return None


# --------------------------------------------------------------------- runner

def handoff_from_facts(facts: OrderFacts, *, quantity: int) -> PurchaseHandoff:
    """Built once, from the served facts, and never re-derived during the run."""
    return PurchaseHandoff(
        resolve_order_id=facts.resolve_order_id,
        shopify_product_gid=facts.product_gid or "",
        shopify_variant_gid=facts.variant_gid or "",
        quantity=quantity,
        currency=facts.currency or "",
    )


@dataclass
class RunnerResult:
    exit_code: int
    dry_run: bool
    checks: list[tuple[str, bool]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    correlation_id: str | None = None
    outcome: object | None = None


async def run(
    *,
    env,
    outbox_path,
    mission_id: str,
    resolve_order_id: str | None = None,
    quantity: int = 1,
    confirm: bool = False,
    order_loader=None,
    gateway=None,
    http_client=None,
    clock=None,
) -> RunnerResult:
    """
    Gate, then (only on confirm) purchase once.

    The exclusive run lock is taken before any gate that could lead to a purchase and held for
    the rest of the run, so two simultaneous processes cannot both walk the preflight and both
    create; it is released on every exit path, including exceptions and cancellation.

    ``order_loader``/``gateway``/``http_client`` are injection seams for hermetic tests; a real
    run supplies none of them and gets the real loader, the real gateway and a real HTTP client.
    """
    collected = GateReport()

    def absorb(report: GateReport) -> bool:
        return collected.absorb(report)

    config, config_report = config_gates(env)
    absorb(config_report)
    if collected.passed:
        absorb(outbox_path_gate(Path(outbox_path)))
    if not collected.passed:
        return RunnerResult(exit_code=1, dry_run=not confirm, checks=collected.checks,
                            problems=collected.problems)

    lock, lock_problem = acquire_run_lock(Path(outbox_path), mission_id=mission_id)
    if lock is None:
        collected.fail(lock_problem or "the exclusive run lock could not be acquired",
                       name="exclusive run lock refused")
        return RunnerResult(exit_code=1, dry_run=not confirm, checks=collected.checks,
                            problems=collected.problems)

    result: RunnerResult | None = None
    try:
        loader = order_loader or fetch_order_facts
        facts, facts_report = await loader(config.resolve_base_url, mission_id=mission_id)
        absorb(facts_report)
        if facts is not None:
            absorb(order_gates(facts, mission_id=mission_id, resolve_order_id=resolve_order_id,
                               quantity=quantity,
                               expected_currency=EXPECTED_CURRENCY,
                               expected_store_domain=EXPECTED_STORE_DOMAIN))
        if not collected.passed:
            result = RunnerResult(exit_code=1, dry_run=not confirm, checks=collected.checks,
                                  problems=collected.problems)
            return result

        handoff = handoff_from_facts(facts, quantity=quantity)
        correlation = derive_correlation(handoff)
        outbox = Outbox(outbox_path)
        absorb(outbox_state_gates(outbox, correlation, handoff.resolve_order_id,
                                  derive_payload_hash(handoff)))
        if not collected.passed:
            result = RunnerResult(exit_code=1, dry_run=not confirm, checks=collected.checks,
                                  problems=collected.problems, correlation_id=correlation)
            return result

        if not confirm:
            # Dry run ends here: no channel object exists, no Shopify call was possible, nothing
            # was signed and nothing was sent.
            result = RunnerResult(exit_code=0, dry_run=True, checks=collected.checks,
                                  problems=collected.problems, correlation_id=correlation)
            return result

        channel_gateway = gateway if gateway is not None else ShopifyAdminGateway(config)
        owns_client = http_client is None
        receiver = http_client if http_client is not None else httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        channel = PurchaseChannel(config, channel_gateway, outbox, clock=clock, http_client=receiver)
        try:
            outcome = await channel.fulfil(handoff)
        finally:
            if owns_client:
                await receiver.aclose()
        confirmed = outcome.status == CONFIRMED_LINKED
        collected.checks.append((f"fulfil outcome: {outcome.status}", confirmed))
        if not confirmed:
            collected.problems.append(
                f"the confirmed run finished with {outcome.status}; nothing was retried, reconciled "
                f"or reclaimed: {outcome.reason or 'no reason recorded'}"
            )
        result = RunnerResult(exit_code=0 if confirmed else 2, dry_run=False, checks=collected.checks,
                              problems=collected.problems, correlation_id=correlation, outcome=outcome)
        return result
    finally:
        note = release_run_lock(lock)
        if note and result is not None:
            result.problems.append(note)


# --------------------------------------------------------------------- cli

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Gated scratch purchase runner. Dry run by default; --confirm is required before a "
            "live Shopify mutation is even possible, and every gate must pass first."
        )
    )
    parser.add_argument("--mission", required=True, help="the mission id whose order to purchase")
    parser.add_argument("--resolve-order-id", default=None,
                        help="optional cross-check: refuse unless the served order has this id")
    parser.add_argument("--outbox", default="scratch_outbox.sqlite3",
                        help="channel outbox location (must be writable; persist it across runs)")
    parser.add_argument("--confirm", action="store_true",
                        help="required before PurchaseChannel.fulfil may run at all")
    args = parser.parse_args(argv)

    result = asyncio.run(
        run(
            env=os.environ,
            outbox_path=args.outbox,
            mission_id=args.mission,
            resolve_order_id=args.resolve_order_id,
            quantity=1,  # pinned: the scratch run buys one unit, and no flag exists to raise it
            confirm=args.confirm,
        )
    )
    mode = (
        "DRY RUN (no --confirm: the channel was never constructed, nothing was created or sent)"
        if result.dry_run
        else "CONFIRMED RUN"
    )
    print(f"scratch purchase runner — {mode}")
    for name, passed in result.checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    for problem in result.problems:
        print(f"  !! {problem}")
    if result.correlation_id:
        print(f"  correlation: {result.correlation_id}")
    if result.outcome is not None:
        print(f"  outcome: {result.outcome!r}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
