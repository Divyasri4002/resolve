"""
Authenticated order-linkage integration (Task 14C.7B).

What this is: the inbound half of the ONLY legitimate way a RESOLVE order can ever learn which
Shopify order it corresponds to. A merchant-side integration — the system that actually placed
the order in the store — signs an event naming both identifiers, and RESOLVE records the pairing.

What this is NOT: an authority. The event supplies two references and nothing else. Money,
product, quantity, reason, fulfillment lines and user identity are all read from RESOLVE's own
persisted rows or from Shopify's own reads, never from this payload — the schema forbids unknown
fields outright, so there is no channel through which an extra field could become authoritative.
And even a perfectly signed event cannot store a Shopify order id that Shopify does not confirm:
`app.tools.shopify.linkage.link_order_to_shopify_order` remains the single writer and verifies
against the store first. Since Task 14C.7F that verification has a second half: when the RESOLVE
order carries canonical Shopify identity from a purchase RESOLVE made out of this store's own
catalog, Shopify's own order lines must sell the same product and variant before the pairing is
stored. A signature proves who said it; only Shopify's data can corroborate what was said.

Trust chain, in order, with nothing skipped:

    shared-secret HMAC over the raw body   (authenticate the sender)
    → strict schema + freshness window     (bound what it may say, and for how long)
    → event_id unique index                (make replay harmless, not merely unlikely)
    → RESOLVE order lookup                 (name the order being linked)
    → Shopify read verification            (the id must be real at this store)
    → immutable, 1:1 linkage               (written once, never re-pointed)

A normal user session authenticates none of this. The route takes no session dependency, no
cookie, and no `Authorization` value: a browser, the frontend, and the reasoning model all have
exactly zero way to reach it, signed or not.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import logger as audit
from app.config import get_settings
from app.models.db_models import IntegrationEventRecord, ResolveOrderRecord
from app.tools.shopify.linkage import ShopifyLinkageError, link_order_to_shopify_order

logger = logging.getLogger(__name__)

EVENT_TYPE = "resolve.order_linkage.v1"
SIGNATURE_HEADER = "x-resolve-signature"
# The audit trail distinguishes this principal from an agent: nothing an integration says is
# a decision, and nothing here is labelled as one.
ACTOR = "integration"
AUDIT_ACTION = "shopify_order_linkage"
# A signed body is only worth as much as the sender's clock plus the window it may drift. Beyond
# this the event is refused as stale, so a captured request cannot be replayed at leisure.
MAX_CLOCK_SKEW_SECONDS = 60

# Every outcome this module can record. 'linked' and 'already_linked' are successes; 'rejected'
# covers refusal, which is still remembered, because a rejected event that arrives twice must
# answer the same way both times. 'received' is not terminal — it means a claim exists and its
# processing is still in flight.
RECEIVED = "received"
LINKED = "linked"
ALREADY_LINKED = "already_linked"
REJECTED = "rejected"


class LinkageIntegrationError(Exception):
    """A refusal to process an event. `message` is safe to return: it names no secret."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class OrderLinkageEvent(BaseModel):
    """
    The whole contract. `extra="forbid"` is the load-bearing line: an event that carries an
    amount, a currency, a product title, a fulfillment line id, a return reason, a payment id, or
    a `user_id` to override ownership is not "mostly valid" — it is rejected, 400, unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    event_type: Literal["resolve.order_linkage.v1"]
    shopify_order_id: str = Field(min_length=1, max_length=128)
    issued_at: datetime
    resolve_order_id: str | None = Field(default=None, max_length=64)
    mission_id: str | None = Field(default=None, max_length=64)
    # Which integration sent it — for the audit trail only. Not an authority of any kind, and
    # never used to decide anything.
    source: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _name_exactly_one_resolve_order(self) -> "OrderLinkageEvent":
        if bool(self.resolve_order_id) == bool(self.mission_id):
            raise ValueError(
                "exactly one of resolve_order_id or mission_id is required"
            )
        return self

    @field_validator("shopify_order_id")
    @classmethod
    def _reference_is_not_blank(cls, value: str) -> str:
        # A shape check, not a repair: whitespace is never part of an identifier, and nothing here
        # strips or canonicalises the value into something Shopify might recognise.
        if not value.strip():
            raise ValueError("shopify_order_id must name a Shopify order, not whitespace")
        return value


def sign_body(secret: str, body: bytes) -> str:
    """The canonical signature: HMAC-SHA256 over the RAW request body, hex digest."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _expected_shape(signature: object) -> str | None:
    if not isinstance(signature, str) or not signature:
        return None
    cleaned = signature.strip().lower()
    # 64 hex chars or nothing. This is not a security check (compare_digest is), it just keeps a
    # 2 MB header from being fed to hmac.
    if len(cleaned) != 64 or any(c not in "0123456789abcdef" for c in cleaned):
        return None
    return cleaned


def verify_signature(body: bytes, signature: object, secret: str) -> bool:
    """
    Constant-time verification over the exact bytes received, never over re-serialised JSON.

    False for a missing, malformed, or wrong signature. An unset secret never verifies anything:
    the endpoint is disabled, not open.
    """
    if not secret:
        return False
    received = _expected_shape(signature)
    if received is None:
        return False
    expected = sign_body(secret, body)
    return hmac.compare_digest(expected, received)


def parse_event(body: bytes) -> OrderLinkageEvent:
    """Strict, size-bounded parse. Error text never echoes the payload back."""
    if not body or len(body) > 16_384:
        raise LinkageIntegrationError(400, "The event body must be a small JSON object.")
    try:
        raw = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LinkageIntegrationError(400, "The event body is not valid JSON.")
    if not isinstance(raw, dict):
        raise LinkageIntegrationError(400, "The event body must be a JSON object.")
    try:
        return OrderLinkageEvent.model_validate(raw)
    except Exception:
        raise LinkageIntegrationError(
            400,
            "The event does not match the order-linkage contract: unknown, missing, or invalid "
            "fields are all refused.",
        )


def check_freshness(event: OrderLinkageEvent) -> None:
    """Reject an event whose `issued_at` is outside the allowed window, in either direction."""
    issued = event.issued_at
    if issued.tzinfo is None:
        raise LinkageIntegrationError(400, "issued_at must be a timezone-aware timestamp.")
    limit = get_settings().linkage_event_max_age_seconds
    age = (datetime.now(timezone.utc) - issued.astimezone(timezone.utc)).total_seconds()
    if age > limit + MAX_CLOCK_SKEW_SECONDS or age < -MAX_CLOCK_SKEW_SECONDS:
        raise LinkageIntegrationError(
            400, "issued_at is outside the accepted window, so this event was not processed."
        )


def _client():
    """The one existing Shopify credential path — no second auth mechanism here."""
    from app.tools.shopify import get_shopify_client

    return get_shopify_client()


async def _find_order(db: AsyncSession, event: OrderLinkageEvent) -> ResolveOrderRecord | None:
    if event.resolve_order_id:
        return await db.scalar(
            select(ResolveOrderRecord).where(
                ResolveOrderRecord.order_id == event.resolve_order_id
            )
        )
    return await db.scalar(
        select(ResolveOrderRecord).where(ResolveOrderRecord.mission_id == event.mission_id)
    )


async def _claim(
    db: AsyncSession, event: OrderLinkageEvent, body: bytes
) -> tuple[IntegrationEventRecord, bool]:
    """
    Take ownership of this event_id. Returns (row, won_the_claim).

    Losing the INSERT is normal and safe: the row already exists, so the only honest answers are
    "the same event was already delivered" (idempotent replay) or "someone reused an event id for
    a different payload" (refused). The unique constraint decides, not a read that could race.
    """
    row = IntegrationEventRecord(
        event_id=event.event_id,
        event_type=event.event_type,
        body_sha256=hashlib.sha256(body).hexdigest(),
        mission_id=event.mission_id,
        resolve_order_id=event.resolve_order_id,
        outcome=RECEIVED,
    )
    db.add(row)
    try:
        await db.flush()
        return row, True
    except IntegrityError:
        await db.rollback()
        existing = await db.get(IntegrationEventRecord, event.event_id)
        if existing is None:  # vanished between conflict and read: refuse, never guess
            raise LinkageIntegrationError(503, "The event could not be recorded; retry later.")
        return existing, False


async def handle_linkage_event(
    db: AsyncSession, *, body: bytes, signature: object, client=None
) -> tuple[int, dict]:
    """
    Verify, de-duplicate, and delegate. Returns (http_status, response_body).

    The order of operations is the security design, so it is not rearranged for convenience:
    nothing is read, stored, or acted on before the signature over the raw bytes has passed.
    """
    settings = get_settings()
    if not settings.linkage_integration_secret:
        # Disabled is stated, not implied: an unconfigured deployment must not look like an
        # endpoint that accepts unsigned events.
        raise LinkageIntegrationError(
            503, "The order-linkage integration is not configured on this deployment."
        )
    if not verify_signature(body, signature, settings.linkage_integration_secret):
        # Same answer for missing, malformed, and wrong. Nothing here may say which.
        raise LinkageIntegrationError(401, "The event signature is not valid.")

    event = parse_event(body)
    check_freshness(event)

    row, won = await _claim(db, event, body)
    if not won:
        if row.body_sha256 != hashlib.sha256(body).hexdigest():
            raise LinkageIntegrationError(
                409, "That event_id was already recorded for a different payload."
            )
        if row.outcome == RECEIVED:
            # Another delivery of this exact event is still being processed. Answering "done" here
            # would be a claim about work that has not happened yet, so the sender is told to retry
            # — and the row it is retrying against cannot produce a second linkage.
            raise LinkageIntegrationError(
                503, "This event is already being processed, so it was not processed again."
            )
        # Byte-identical replay of a settled event: answer with what already happened.
        return 200, {
            "event_id": row.event_id,
            "outcome": row.outcome,
            "mission_id": row.mission_id,
            "resolve_order_id": row.resolve_order_id,
            "provider_order_reference": row.provider_order_reference,
            "already_processed": True,
        }
    await db.commit()

    order = await _find_order(db, event)
    if order is None:
        return await _finish(
            db, row, REJECTED, 404,
            detail="No RESOLVE order matches this event, so nothing was linked.",
            response_detail="No RESOLVE order matches this event, so nothing was linked.",
        )

    row.mission_id = order.mission_id
    row.resolve_order_id = order.order_id
    was_linked = order.provider_order_reference
    try:
        # The single writer. Ownership is taken from the persisted order, never from the event;
        # and Shopify must confirm this exact order before anything is stored.
        linked = await link_order_to_shopify_order(
            db,
            mission_id=order.mission_id,
            actor_user_id=order.user_id,
            shopify_order_id=event.shopify_order_id,
            client=client if client is not None else _client(),
        )
    except ShopifyLinkageError as exc:
        # Refused, and still auditable: the mission exists, the attempt happened, and an auditor
        # is entitled to see that a signed event was turned down and why.
        await audit.append_event(
            db,
            mission_id=order.mission_id,
            actor=ACTOR,
            action=AUDIT_ACTION,
            recorded=False,
            reason=(
                f"order-linkage event {event.event_id} refused "
                f"(source={event.source or 'unspecified'}): {exc.message}"
            ),
        )
        return await _finish(
            db, row, REJECTED, exc.status_code,
            detail=exc.message, response_detail=_public_detail(exc),
        )

    outcome = LINKED if was_linked is None else ALREADY_LINKED
    row.provider_order_reference = linked.provider_order_reference
    await audit.append_event(
        db,
        mission_id=order.mission_id,
        actor=ACTOR,
        action=AUDIT_ACTION,
        recorded=True,
        reason=(
            f"order-linkage event {event.event_id} ({event.event_type}, "
            f"source={event.source or 'unspecified'}) "
            f"{'established' if outcome == LINKED else 'confirmed as already established'} "
            f"the merchant order reference {linked.provider_order_reference} on "
            f"{order.order_id}"
        ),
    )
    return await _finish(
        db, row, outcome, 200,
        response={
            "event_id": row.event_id,
            "outcome": outcome,
            "mission_id": order.mission_id,
            "resolve_order_id": order.order_id,
            "provider_order_reference": linked.provider_order_reference,
            "already_processed": False,
        },
    )


def _public_detail(exc: ShopifyLinkageError) -> str:
    """
    What the caller is told. The linkage's own wording is about RESOLVE's rules and the caller's
    own input, so it is safe and useful to relay. A 503 is the one case that carries text from the
    merchant store, and an external caller has no claim on the internals of a failed exchange —
    it is told the outcome, and the recorded detail keeps the reason for the auditor.
    """
    if exc.status_code == 503:
        return (
            "The merchant store could not confirm that order, so no linkage was stored. This "
            "event id is now spent; a further attempt must arrive as a new event."
        )
    return exc.message


async def _finish(
    db: AsyncSession,
    row: IntegrationEventRecord,
    outcome: str,
    status_code: int,
    *,
    detail: str | None = None,
    response_detail: str | None = None,
    response: dict | None = None,
) -> tuple[int, dict]:
    row.outcome = outcome
    row.detail = detail
    row.processed_at = datetime.utcnow()
    await db.commit()
    body = response if response is not None else {
        "event_id": row.event_id,
        "outcome": outcome,
        "detail": response_detail or detail or "",
    }
    return status_code, body


__all__ = [
    "ACTOR",
    "ALREADY_LINKED",
    "AUDIT_ACTION",
    "EVENT_TYPE",
    "LINKED",
    "LinkageIntegrationError",
    "OrderLinkageEvent",
    "RECEIVED",
    "REJECTED",
    "SIGNATURE_HEADER",
    "check_freshness",
    "handle_linkage_event",
    "parse_event",
    "sign_body",
    "verify_signature",
]
