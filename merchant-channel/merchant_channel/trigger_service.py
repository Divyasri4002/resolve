"""
The standalone merchant purchase trigger: an authenticated HTTP edge over the one purchase path
that already exists.

This service owns no purchase logic of its own. A valid request walks exactly the path the scratch
runner walks — the shared gates in ``merchant_channel.gates``, the same handoff construction, the
same outbox state checks, the same exclusive run lock, and ``PurchaseChannel.fulfil`` — so there is
no second purchase execution mechanism to drift out of sync. The caller supplies only ``mission_id``
and ``resolve_order_id``; every fact a purchase needs (product GID, variant GID, currency, store) is
read from RESOLVE's own API, and everything this deployment is willing to purchase against comes
from this process's environment, never from the caller.

The response is honest about what the execution did and did not do: ``fulfil``'s outcome status is
returned verbatim beside a response class, and the class is chosen by what can still be true:

- ``confirmed_linked``                the order exists, matches, and its linkage event was delivered.
- ``refused_before_create``           a pre-execution gate refused (configuration, authoritative
                                      facts, order identity), or the gateway failed before creating
                                      anything (``GatewayDefiniteFailure``'s documented meaning).
                                      This class is returned only when the evidence establishes that
                                      this execution created nothing and no prior attempt is
                                      unresolved — an outbox-state refusal is never reported here.
- ``refused_after_create``            a Shopify order exists, but the linkage was refused (wrong
                                      store, mismatched facts), the linkage event could not be
                                      delivered as claimed, or the outbox row already records this
                                      purchase as created or linked. The purchase must not be
                                      retried blind.
- ``unknown_requires_reconciliation`` the outcome may or may not have reached Shopify (lost answer,
                                      unreadable read-back), or a previous attempt's outcome is
                                      still unresolved (PENDING row, unknown outcome). Never retried
                                      blind; an operator reconciles. Unknown outcomes answer 200 on
                                      purpose: a 5xx invites an infrastructure-level retry, which is
                                      exactly the blind retry this design forbids.
- ``invalid_request`` (422)           the body was not exactly two well-formed identifiers.
- ``authentication_failure`` (401)    the trigger token was missing or wrong.
- ``concurrent_execution`` (409)      another run holds this outbox's exclusive run lock.

Configuration is read fresh on every request from this process's environment only. Besides the
channel's own ``MERCHANT_*`` values (loaded by ``load_config``, which still refuses RESOLVE-owned
names), the trigger requires its own three:

    MERCHANT_TRIGGER_TOKEN           the only credential that may call this service; compared with
                                     ``hmac.compare_digest``, never logged, never returned.
    MERCHANT_TRIGGER_OUTBOX          the durable outbox file this service purchases through.
    MERCHANT_EXPECTED_STORE_DOMAIN   the deployment's store pin passed to ``order_gates``; it must
                                     equal the channel's ``MERCHANT_SHOPIFY_STORE_DOMAIN``, so a run
                                     can never verify facts against one store and purchase against
                                     another.

``MERCHANT_RESOLVE_BASE_URL``, ``MERCHANT_RESOLVE_SERVICE_TOKEN`` and ``MERCHANT_EXPECTED_CURRENCY``
are the channel's existing names: the base URL and currency come from the loaded config, and the
service token is read by ``fetch_order_facts`` itself and fails closed when absent.

The service runs as its own process (``python -m merchant_channel.trigger_service``, or any uvicorn
invocation of ``merchant_channel.trigger_service:app``). Nothing in the FastAPI backend imports this
package, and this package imports nothing from the backend; ``merchant_channel.config`` keeps
refusing RESOLVE-owned environment names.
"""
from __future__ import annotations

import hmac
import os
import sqlite3
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from merchant_channel import outbox as outbox_states
from merchant_channel.channel import (
    AWAITING_RECONCILIATION,
    CONFIRMED_LINKED,
    DELIVERY_UNKNOWN,
    FAILED,
    OUTCOME_UNKNOWN,
    PurchaseChannel,
    REFUSED_HANDOFF,
    REFUSED_IDENTITY,
    REFUSED_LINKAGE,
)
from merchant_channel.config import ChannelConfig, ChannelConfigError, load_config
from merchant_channel.correlation import (
    correlation_id as derive_correlation,
    payload_hash as derive_payload_hash,
)
from merchant_channel.gates import (
    ORDER_ID_SHAPE,
    OUTBOX_REFUSAL_AMBIGUOUS,
    OUTBOX_REFUSAL_ORDER_EXISTS,
    GateReport,
    fetch_order_facts,
    order_gates,
    outbox_state_gates,
)
from merchant_channel.gateway import ShopifyAdminGateway
from merchant_channel.outbox import Outbox
from scratch_purchase import (
    acquire_run_lock,
    handoff_from_facts,
    outbox_path_gate,
    release_run_lock,
)

TRIGGER_TOKEN_ENV = "MERCHANT_TRIGGER_TOKEN"
TRIGGER_TOKEN_HEADER = "X-Merchant-Trigger-Token"
TRIGGER_OUTBOX_ENV = "MERCHANT_TRIGGER_OUTBOX"
EXPECTED_STORE_DOMAIN_ENV = "MERCHANT_EXPECTED_STORE_DOMAIN"

CONFIRMED_CLASS = "confirmed_linked"
REFUSED_BEFORE_CREATE = "refused_before_create"
REFUSED_AFTER_CREATE = "refused_after_create"
UNKNOWN_CLASS = "unknown_requires_reconciliation"

# The one place outcome statuses are classified. An unrecognized status classifies as unknown:
# if the channel ever grows a state this module has not heard of, the safe answer is "requires
# reconciliation", never "confirmed" and never a refusal that would invite a retry.
_CLASS_BY_OUTCOME = {
    CONFIRMED_LINKED: CONFIRMED_CLASS,
    REFUSED_HANDOFF: REFUSED_BEFORE_CREATE,
    FAILED: REFUSED_BEFORE_CREATE,
    REFUSED_IDENTITY: REFUSED_AFTER_CREATE,
    REFUSED_LINKAGE: REFUSED_AFTER_CREATE,
    OUTCOME_UNKNOWN: UNKNOWN_CLASS,
    DELIVERY_UNKNOWN: UNKNOWN_CLASS,
    AWAITING_RECONCILIATION: UNKNOWN_CLASS,
}

# How an outbox-gate refusal is reported, by the category the gate stamped. A PENDING or
# unknown-outcome row means a prior attempt's outcome is unresolved, so "no order exists"
# would be a lie — that is ``unknown_requires_reconciliation``. A linkage state or a
# confirmed/reconciled-matched row is the record of a verified create, so that refusal is
# honestly ``refused_after_create``. An unstamped category is unknown, fail-closed.
_OUTBOX_REFUSAL_CLASS = {
    OUTBOX_REFUSAL_AMBIGUOUS: UNKNOWN_CLASS,
    OUTBOX_REFUSAL_ORDER_EXISTS: REFUSED_AFTER_CREATE,
}

# The pinned quantity: the caller cannot ask for more than one unit, and the mandate forbids the
# caller from supplying any purchase fact — amount and quantity included.
TRIGGER_QUANTITY = 1

_MISSION_ID_MAX_LENGTH = 120

_AUTH_FAILURE_REASON = (
    "the X-Merchant-Trigger-Token header is missing or does not match the configured trigger token"
)


class PurchaseRequest(BaseModel):
    """Exactly two identifiers, nothing more. Extra fields, wrong types, missing fields: refused."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    resolve_order_id: str


def _validation_problems(exc: ValidationError) -> list[str]:
    # Only the location and the rejection kind are reported. The offending value is the caller's
    # input, and echoing it back would let a caller write into this service's responses.
    problems = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ())) or "body"
        kind = error.get("type", "invalid")
        problems.append(f"{loc}: rejected ({kind})")
    return problems


def _identifier_problems(request: PurchaseRequest) -> list[str]:
    problems = []
    if not request.mission_id or not request.mission_id.strip():
        problems.append("mission_id must be a non-empty identifier")
    elif len(request.mission_id) > _MISSION_ID_MAX_LENGTH:
        problems.append(f"mission_id must be at most {_MISSION_ID_MAX_LENGTH} characters")
    if not ORDER_ID_SHAPE.match(request.resolve_order_id):
        problems.append(
            "resolve_order_id must match the shared RESOLVE order shape: order_ followed by 12 "
            "hexadecimal characters"
        )
    return problems


def _authorized(request: Request) -> bool:
    expected = os.environ.get(TRIGGER_TOKEN_ENV, "")
    supplied = request.headers.get(TRIGGER_TOKEN_HEADER)
    if not expected or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _config_gate(env) -> tuple[ChannelConfig | None, GateReport]:
    """
    The trigger's own deployment pin, checked fresh on every request.

    Deliberately not ``scratch_purchase.config_gates``: that gate pins the scratch run's deployment
    (its shop name, its Docker-host base URL). The trigger pins nothing but its own three values
    plus the channel's own completeness — and one consistency rule the scratch run does not need,
    because the scratch runner is the same deployment as its pins: the store the facts are verified
    against (``MERCHANT_EXPECTED_STORE_DOMAIN``) must be the store the channel is configured to
    purchase from, or a mismatch would let facts from one store license a purchase on another.
    """
    report = GateReport()
    try:
        config = load_config(env)
        report.ok("config loaded from MERCHANT_* values (no RESOLVE-owned name present)")
    except ChannelConfigError as exc:
        report.fail(f"config refused: {exc}")
        return None, report
    if config.configured:
        report.ok("the channel config is fully configured")
    else:
        report.fail(
            "the channel config is not fully configured: store domain, linkage secret, base url "
            "and a Shopify credential path are all required before anything may be created"
        )
    if config.api_version:
        report.ok("the Shopify API version is set")
    else:
        report.fail("MERCHANT_SHOPIFY_API_VERSION is required and was empty")
    if config.expected_currency:
        report.ok("the expected currency is configured")
    else:
        report.fail("MERCHANT_EXPECTED_CURRENCY is required and was empty")
    if config.expected_shop_name:
        report.ok("the expected shop name is configured, so the channel's store-identity guard bites")
    else:
        report.fail(
            "MERCHANT_EXPECTED_SHOP_NAME is required: without it the channel's shop-identity "
            "guard would not bite"
        )
    try:
        mode = config.credential_mode()
        report.ok(f"the Shopify credential path is valid ({mode})")
    except ChannelConfigError as exc:
        report.fail(f"no usable Shopify credential: {exc}")

    expected_store = env.get(EXPECTED_STORE_DOMAIN_ENV, "")
    if not expected_store:
        report.fail(f"{EXPECTED_STORE_DOMAIN_ENV} is required and was empty")
    elif expected_store.strip().lower() != config.store_domain.strip().lower():
        report.fail(
            f"{EXPECTED_STORE_DOMAIN_ENV} is {expected_store!r} but the channel is configured for "
            f"{config.store_domain!r}: verifying facts against one store and purchasing against "
            "another is refused"
        )
    else:
        report.ok("the trigger's expected store domain matches the channel's own store")

    if env.get(TRIGGER_TOKEN_ENV):
        report.ok("the trigger token is configured (its value is never read into any response)")
    else:
        report.fail(f"{TRIGGER_TOKEN_ENV} is required; without it every request is refused")
    if env.get(TRIGGER_OUTBOX_ENV):
        report.ok("the trigger outbox path is configured")
    else:
        report.fail(
            f"{TRIGGER_OUTBOX_ENV} is required: the durable outbox is the memory that keeps a "
            "retry from becoming a second order"
        )
    return config, report


def _json(status_code: int, outcome_class: str, **fields) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"outcome_class": outcome_class, **fields})


def _refusal(
    report: GateReport, *, correlation_id: str | None = None, outcome_class: str = REFUSED_BEFORE_CREATE
) -> JSONResponse:
    problems = list(report.problems)
    return _json(
        200,
        outcome_class,
        outcome_status=None,
        correlation_id=correlation_id,
        reason="; ".join(problems) or "the purchase was refused before anything could be created",
        problems=problems,
        checks=[{"name": name, "passed": passed} for name, passed in report.checks],
    )


def _read_outbox_history(
    outbox_path: Path, resolve_order_id: str
) -> list[tuple[str, str | None, str | None]] | None:
    """
    A read-only look at the outbox rows recorded for one RESOLVE order — (status,
    reconciliation_state, linkage) per row — or None whenever the outbox cannot be inspected at
    all. A missing file is None too: an absent outbox is not evidence that no prior attempt
    happened (a lost or recreated volume takes its rows with it), so the caller must fail closed
    on None. The connection is opened in read-only URI mode, so the inspection can never create
    or modify the outbox and never touches a merchant order.
    """
    if not outbox_path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{outbox_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        return connection.execute(
            "SELECT status, reconciliation_state, linkage FROM purchase_outbox "
            "WHERE resolve_order_id = ?",
            (resolve_order_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _prior_attempt_evidence(env, resolve_order_id: str) -> tuple[str, GateReport]:
    """
    The honesty check every pre-outbox refusal must run before it may claim that nothing was
    created: consult what the outbox itself recorded for THIS resolve order and let that
    evidence choose the outcome class. The lookup is by resolve_order_id — the deterministic
    correlation cannot be derived without the facts that just failed to load, and another
    order's rows must never be mistaken for this order's history.

    An unresolved prior attempt (PENDING, unknown outcome, or reconciled-ambiguous) beats a
    verified create, and a verified create beats "no attempt recorded", so several rows for one
    order always answer with the most conservative honest class. When the outbox cannot be
    inspected at all, the answer is unknown, fail-closed — never an assumption that no prior
    attempt exists.
    """
    evidence = GateReport()
    raw_path = env.get(TRIGGER_OUTBOX_ENV)
    if not raw_path:
        evidence.fail(
            "the outbox could not be inspected: MERCHANT_TRIGGER_OUTBOX is not set, so a "
            "previous attempt for this order cannot be ruled out",
            name="prior_attempt_check",
        )
        return UNKNOWN_CLASS, evidence
    history = _read_outbox_history(Path(raw_path), resolve_order_id)
    if history is None:
        evidence.fail(
            "the outbox could not be inspected at the configured path, so a previous attempt "
            "for this order cannot be ruled out",
            name="prior_attempt_check",
        )
        return UNKNOWN_CLASS, evidence

    unresolved = False
    created = False
    for status, reconciliation_state, linkage in history:
        if (
            status == outbox_states.PENDING
            or status == outbox_states.OUTCOME_UNKNOWN
            or reconciliation_state == outbox_states.AMBIGUOUS
        ):
            unresolved = True
        elif (
            linkage
            in (
                outbox_states.LINKAGE_SENT,
                outbox_states.LINKAGE_DELIVERY_UNKNOWN,
                outbox_states.LINKAGE_REFUSED,
            )
            or status == outbox_states.CONFIRMED
            or (
                status == outbox_states.RECONCILED
                and reconciliation_state == outbox_states.MATCHED
            )
        ):
            created = True
    if unresolved:
        evidence.fail(
            "the outbox records an unresolved prior attempt for this order, so whether a "
            "merchant order exists is unknown",
            name="prior_attempt_check",
        )
        return UNKNOWN_CLASS, evidence
    if created:
        evidence.fail(
            "the outbox records a verified merchant order for this order",
            name="prior_attempt_check",
        )
        return REFUSED_AFTER_CREATE, evidence
    evidence.ok("prior_attempt_check")
    return REFUSED_BEFORE_CREATE, evidence


def create_trigger_app(*, order_loader=None, gateway=None, http_client=None, clock=None) -> FastAPI:
    """
    Build the trigger application.

    ``order_loader``/``gateway``/``http_client``/``clock`` are injection seams for hermetic tests,
    exactly as in the scratch runner; a real deployment supplies none of them and gets the real
    loader, the real gateway, a real HTTP client and the real clock.
    """
    loader = order_loader or fetch_order_facts
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/purchase")
    async def purchase(request: Request) -> JSONResponse:
        if not _authorized(request):
            return _json(
                401,
                "authentication_failure",
                outcome_status=None,
                correlation_id=None,
                reason=_AUTH_FAILURE_REASON,
            )
        try:
            parsed = PurchaseRequest.model_validate_json(await request.body())
        except ValidationError as exc:
            return _json(
                422,
                "invalid_request",
                outcome_status=None,
                correlation_id=None,
                problems=_validation_problems(exc),
            )
        problems = _identifier_problems(parsed)
        if problems:
            return _json(
                422,
                "invalid_request",
                outcome_status=None,
                correlation_id=None,
                problems=problems,
            )

        env = os.environ
        report = GateReport()
        config, config_report = _config_gate(env)
        report.absorb(config_report)
        if report.passed:
            report.absorb(outbox_path_gate(Path(env[TRIGGER_OUTBOX_ENV])))
        if not report.passed:
            # A configuration or outbox-path failure is not evidence that nothing was ever
            # created: the prior-attempt check decides what this refusal may honestly claim.
            outcome_class, evidence = _prior_attempt_evidence(env, parsed.resolve_order_id)
            report.absorb(evidence)
            return _refusal(report, outcome_class=outcome_class)

        lock, lock_problem = acquire_run_lock(Path(env[TRIGGER_OUTBOX_ENV]), mission_id=parsed.mission_id)
        if lock is None:
            return _json(
                409,
                "concurrent_execution",
                outcome_status=None,
                correlation_id=None,
                reason=lock_problem
                or "another execution holds the exclusive run lock for this outbox",
            )
        try:
            return await _purchase(parsed, config, report)
        finally:
            release_run_lock(lock)

    async def _purchase(
        parsed: PurchaseRequest, config: ChannelConfig, report: GateReport
    ) -> JSONResponse:
        env = os.environ
        facts, facts_report = await loader(config.resolve_base_url, mission_id=parsed.mission_id)
        report.absorb(facts_report)
        if facts is not None:
            report.absorb(
                order_gates(
                    facts,
                    mission_id=parsed.mission_id,
                    resolve_order_id=parsed.resolve_order_id,
                    quantity=TRIGGER_QUANTITY,
                    expected_currency=config.expected_currency,
                    expected_store_domain=env[EXPECTED_STORE_DOMAIN_ENV],
                )
            )
        if not report.passed:
            # A facts-fetch or order-gate failure is not evidence that nothing was ever created.
            # Server-derived linkage on the order row itself is the strongest evidence and
            # dominates; otherwise the prior-attempt check decides what this refusal may claim.
            if facts is not None and facts.merchant_order_linked:
                return _refusal(report, outcome_class=REFUSED_AFTER_CREATE)
            outcome_class, evidence = _prior_attempt_evidence(env, parsed.resolve_order_id)
            report.absorb(evidence)
            return _refusal(report, outcome_class=outcome_class)

        handoff = handoff_from_facts(facts, quantity=TRIGGER_QUANTITY)
        correlation = derive_correlation(handoff)
        outbox = Outbox(env[TRIGGER_OUTBOX_ENV])
        outbox_report = outbox_state_gates(
            outbox, correlation, handoff.resolve_order_id, derive_payload_hash(handoff)
        )
        report.absorb(outbox_report)
        if not outbox_report.passed:
            # An outbox refusal is not evidence that this execution created nothing: an
            # ambiguous prior attempt must be reported as unknown, and a row that records a
            # verified create as refused_after_create. The gate stamps the category; anything
            # unstamped is unknown, fail-closed.
            return _refusal(
                report,
                correlation_id=correlation,
                outcome_class=_OUTBOX_REFUSAL_CLASS.get(
                    outbox_report.refusal_category, UNKNOWN_CLASS
                ),
            )

        channel_gateway = gateway if gateway is not None else ShopifyAdminGateway(config)
        owns_client = http_client is None
        receiver = (
            http_client if http_client is not None else httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        )
        channel = PurchaseChannel(config, channel_gateway, outbox, clock=clock, http_client=receiver)
        try:
            outcome = await channel.fulfil(handoff)
        finally:
            if owns_client:
                await receiver.aclose()

        outcome_class = _CLASS_BY_OUTCOME.get(outcome.status, UNKNOWN_CLASS)
        return _json(
            200,
            outcome_class,
            outcome_status=outcome.status,
            correlation_id=outcome.correlation_id or correlation,
            external_order_id=outcome.external_order_id,
            event_id=outcome.event_id,
            candidates=outcome.candidates,
            reason=outcome.reason,
        )

    return app


app = create_trigger_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MERCHANT_TRIGGER_PORT", "8100")))
