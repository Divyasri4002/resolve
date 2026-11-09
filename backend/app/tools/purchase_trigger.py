"""
The backend's only path to the merchant purchase trigger: one authenticated HTTP submission.

The backend and the merchant channel are separate processes that share nothing — this module
MUST NOT import merchant_channel (no backend module may) — so the contract here is the
trigger's documented HTTP surface: POST {MERCHANT_TRIGGER_URL}/purchase with a JSON body of
exactly ``{"mission_id": ..., "resolve_order_id": ...}`` and the ``X-Merchant-Trigger-Token``
header. The caller supplies only the two identifiers: every purchase fact (product GID,
variant GID, currency, store, amount) is read back from RESOLVE's own API by the trigger
itself, and everything the deployment is willing to purchase against lives in the trigger's
environment — none of it passes through here.

The trigger's response is an UNTRUSTED REPORT. Every value it returns is carried under
``trigger_reported_`` names by the caller and never treated as authoritative merchant state:
the authoritative merchant linkage is the order row's ``provider_order_reference``, written
only by the linkage receiver after Shopify itself confirmed the pairing.

The classification this module returns is the caller's ONLY basis for the persisted
authorization state, and it is deliberately conservative:

  confirmed_linked          the trigger reported a confirmed linkage (still verified against
                            the authoritative linkage record, never trusted as proof)
  refused_before_create     the trigger refused before creating anything — the evidence
                            establishes that this submission created nothing and no prior
                            attempt is unresolved, so nothing exists on the merchant side
  refused_after_create      the trigger reports a merchant order exists (or may exist) while
                            the linkage was refused or not yet settled — never marked failed,
                            never retried
  unknown                   the outcome is unresolved: a lost or unreadable answer, any 5xx,
                            a redirect, an unparseable or unrecognized body, or the trigger
                            reporting that a previous attempt's outcome is still unknown.
                            "Unknown" never becomes "failed" — a blind retry is exactly the
                            duplicate-purchase bet this classification refuses to make
  not_sent                  the submission DEFINITIVELY never executed: the connection failed
                            before anything was sent, or the trigger refused pre-execution
                            (401 authentication / 409 lock / 422 validation). Only this class
                            leaves the persisted claim resubmittable.

Outbound values that are never sent, logged, or embedded in any reason: the trigger token.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import get_settings

TRIGGER_TOKEN_HEADER = "X-Merchant-Trigger-Token"

# The trigger's documented outcome classes (kept as literals: no backend module may import
# merchant_channel to name them).
_TRIGGER_CONFIRMED_LINKED = "confirmed_linked"
_TRIGGER_REFUSED_BEFORE_CREATE = "refused_before_create"
_TRIGGER_REFUSED_AFTER_CREATE = "refused_after_create"
_TRIGGER_UNKNOWN_RECONCILIATION = "unknown_requires_reconciliation"

# Backend-side classifications (the vocabulary the authorization record is written in).
CONFIRMED_LINKED = "confirmed_linked"
REFUSED_BEFORE_CREATE = "refused_before_create"
REFUSED_AFTER_CREATE = "refused_after_create"
UNKNOWN = "unknown"
NOT_SENT = "not_sent"

# Statuses the trigger service provably returns BEFORE any purchase execution runs. Anything
# else — redirects, other 4xx, 5xx — cannot prove the submission did not execute.
_PRE_EXECUTION_REFUSAL_STATUSES = frozenset({401, 409, 422})

# Matches the trigger service's own outbound timeout; the submission is a single POST.
_SUBMIT_TIMEOUT_SECONDS = 30.0


class TriggerNotConfigured(Exception):
    """The deployment has no trigger URL or no trigger token — fail closed before any claim."""


@dataclass(frozen=True)
class TriggerReport:
    """What the submission did, plus the trigger's untrusted reported values, verbatim."""

    classification: str  # one of the five classes above
    outcome_class: str | None = None
    outcome_status: str | None = None
    correlation_id: str | None = None
    external_order_id: str | None = None
    event_id: str | None = None
    reason: str | None = None


def _default_transport() -> httpx.AsyncBaseTransport | None:
    """
    Injection seam for hermetic tests: None means a real network client. Tests install an
    httpx.MockTransport here so the endpoint exercises the REAL client code — request
    construction, headers, response parsing, exception mapping — without any network.
    """
    return None


def _classify_outcome_class(outcome_class: str) -> str:
    if outcome_class == _TRIGGER_UNKNOWN_RECONCILIATION:
        return UNKNOWN
    if outcome_class in (
        _TRIGGER_CONFIRMED_LINKED,
        _TRIGGER_REFUSED_BEFORE_CREATE,
        _TRIGGER_REFUSED_AFTER_CREATE,
    ):
        return outcome_class
    # An unrecognized class from the trigger is unknown by design: if the channel ever grows
    # a state this backend has not heard of, the safe answer is never "confirmed" and never
    # a refusal that would invite a resubmission.
    return UNKNOWN


def _report_from_200(response: httpx.Response) -> TriggerReport:
    try:
        payload = response.json()
    except ValueError:
        return TriggerReport(
            classification=UNKNOWN,
            reason="the trigger answered 200 with a body that is not JSON — the outcome is unknown",
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("outcome_class"), str):
        return TriggerReport(
            classification=UNKNOWN,
            reason="the trigger answered 200 without an outcome_class — the outcome is unknown",
        )
    outcome_class = payload["outcome_class"]
    reason = payload.get("reason")
    return TriggerReport(
        classification=_classify_outcome_class(outcome_class),
        outcome_class=outcome_class,
        outcome_status=(
            payload["outcome_status"]
            if isinstance(payload.get("outcome_status"), str)
            else None
        ),
        correlation_id=(
            payload["correlation_id"]
            if isinstance(payload.get("correlation_id"), str)
            else None
        ),
        external_order_id=(
            payload["external_order_id"]
            if isinstance(payload.get("external_order_id"), str)
            else None
        ),
        event_id=payload["event_id"] if isinstance(payload.get("event_id"), str) else None,
        reason=reason if isinstance(reason, str) else None,
    )


async def submit_purchase(
    mission_id: str,
    resolve_order_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TriggerReport:
    """
    Submit ONE purchase to the configured trigger and classify what happened.

    Never raises for a submission-level failure: every transport error, refused request, or
    unintelligible answer becomes a TriggerReport whose classification is honest about what
    can still be true. Configuration problems raise TriggerNotConfigured BEFORE the caller
    has persisted anything, so a misconfigured deployment never leaves a claim behind.
    """
    settings = get_settings()
    base_url = settings.merchant_trigger_url.strip().rstrip("/")
    token = settings.merchant_trigger_token
    if not base_url or not token:
        raise TriggerNotConfigured(
            "merchant purchase is not configured for this deployment: the trigger URL and "
            "token must both be set before a purchase can be submitted"
        )

    resolved_transport = transport if transport is not None else _default_transport()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_SUBMIT_TIMEOUT_SECONDS), transport=resolved_transport
    ) as client:
        try:
            response = await client.post(
                f"{base_url}/purchase",
                json={"mission_id": mission_id, "resolve_order_id": resolve_order_id},
                headers={TRIGGER_TOKEN_HEADER: token},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The connection was never established, so nothing was sent — the submission
            # definitively did not execute.
            return TriggerReport(
                classification=NOT_SENT,
                reason=(
                    "the connection to the purchase trigger failed before anything was "
                    f"sent, so no submission ran: {exc}"
                ),
            )
        except httpx.HTTPError as exc:
            # The request may have reached the trigger and the answer was lost or
            # unreadable — the outcome is genuinely unknown.
            return TriggerReport(
                classification=UNKNOWN,
                reason=(
                    "the purchase submission's answer was lost or unreadable, so it is "
                    f"unknown whether the trigger executed it: {exc}"
                ),
            )

    if response.status_code == 200:
        return _report_from_200(response)

    if response.status_code in _PRE_EXECUTION_REFUSAL_STATUSES:
        # The trigger refuses these before any execution: 401 before the gates, 422 before
        # the gates, 409 while another execution holds the run lock. Our submission ran
        # nothing.
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("reason"), str):
                detail = f": {body['reason']}"
        except ValueError:
            pass
        return TriggerReport(
            classification=NOT_SENT,
            reason=(
                f"the trigger refused the submission before executing anything "
                f"(HTTP {response.status_code}){detail}"
            ),
        )

    # 3xx / other 4xx / 5xx: the response cannot prove what happened to the submission.
    return TriggerReport(
        classification=UNKNOWN,
        reason=(
            f"the purchase submission received an unexpected HTTP {response.status_code} "
            "answer that proves nothing about whether the trigger executed it"
        ),
    )
