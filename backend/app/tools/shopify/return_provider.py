"""
Real Shopify merchant return provider (Task 14C.1) — implemented, wired to NOTHING.

This module satisfies the existing ``ReturnProvider`` contract (``app/tools/returns/base.py``)
against Shopify's Admin GraphQL `returnCreate` mutation, so that RESOLVE can eventually ask a
merchant to take goods back instead of only recording the intent. It is deliberately isolated:

  - it is NOT registered in ``app.tools.returns.get_return_provider`` (that factory still only
    knows "none"), NOT referenced by the return executor, the Resolution Orchestrator, any
    router, or the Authority Engine, and NOT reachable from any endpoint;
  - it performs no authority evaluation of its own — it never decides whether a return is
    allowed, it only resolves what Shopify says can be returned and formats what it is handed;
  - it calls no reasoning model, touches no RESOLVE database, and reaches no other provider.

Credentials come from the ONE Shopify credential system that already exists. The caller passes a
``ShopifyReadOnlyClient`` for every read, and — since Task 14C.7O — a ``ShopifyReturnWriteClient``
for the one write this module makes. Both are built over the same configured credential by
``get_shopify_clients()``, so there is still no second token, client id, or secret here, and none is
ever logged or returned. The split is not bookkeeping: the read client cannot carry a write at all,
so this module's mutation travels on an object whose only send method is the declared return
operation, and a document that is not that operation is refused locally before a request exists.

Task 14C.4 hardened this module around three findings, each verified against the live 2026-07
schema, and they are why the code looks the way it does:

  1. `ReturnLineItemInput.fulfillmentLineItemId` "expects a `FulfillmentLineItem.id`" — the id of
     an actual fulfilment line, NOT the `FulfillmentOrderLineItem` id the earlier draft validated.
     A return therefore names a line read from `Order.fulfillments.fulfillmentLineItems`.
  2. `FulfillmentLineItem.quantity` (the units that shipped) is the only honest local bound on a
     return quantity. `FulfillmentOrderLineItem.remainingQuantity` counts units AWAITING
     fulfilment and reads 0 on a shipped line, so it must never cap a return.
  3. `returnCreate` is not covered by Shopify's `@idempotent` request key, so a submission whose
     answer was lost cannot be safely re-sent. That is what ``reconcile_return`` is for: it READS
     `Order.returns` and reports whether the lost submission can be identified, and anything it
     cannot pin down exactly one way stays unknown rather than becoming a second mutation.

Everything Shopify has not explicitly said is reported as unknown or as a failure: a non-empty
``userErrors`` list, an unrecognized return status, or a missing return id all raise instead of
being inferred. Nothing here claims an approved, labelled, shipped, or completed return.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.tools.returns.base import (
    ReturnAck,
    ReturnError,
    ReturnOutcomeUnknown,
    ReturnProvider,
    ReturnProviderRejected,
    ReturnProviderUnavailable,
    ReturnReconciliation,
    ReturnRequest,
    ReturnStatusInfo,
    ReturnSubmission,
    ReturnTarget,
)
from app.tools.shopify.client import ShopifyReadOnlyClient
from app.tools.shopify.errors import (
    ShopifyError,
    ShopifyGraphQLError,
    ShopifyMalformedResponseError,
    ShopifyTransportError,
)
from app.tools.shopify.models import (
    ShopifyOrderLineRef,
    ShopifyReturn,
    ShopifyShippedFulfillmentLine,
)
from app.tools.shopify.return_write_client import ShopifyReturnWriteClient
from app.tools.shopify.sanitizer import redact

logger = logging.getLogger(__name__)

# GID shapes this provider will act on. Shopify's own ids, read back from Shopify — never
# derived from a product/variant/order/database id, and never assembled from parts.
ORDER_GID_PREFIX = "gid://shopify/Order/"
# The identifier a 2026-07 return actually references (verified against the live schema).
FULFILLMENT_LINE_ITEM_GID_PREFIX = "gid://shopify/FulfillmentLineItem/"
# A different object, and NOT a return target. Named so the provider can refuse it by name
# instead of forwarding an id that fails with a message about someone else's line.
FULFILLMENT_ORDER_LINE_ITEM_GID_PREFIX = "gid://shopify/FulfillmentOrderLineItem/"
RETURN_GID_PREFIX = "gid://shopify/Return/"
PRODUCT_GID_PREFIX = "gid://shopify/Product/"
VARIANT_GID_PREFIX = "gid://shopify/ProductVariant/"

_GID_TAIL = re.compile(r"[0-9A-Za-z_-]+\Z")

# How far apart Shopify's `createdAt` and RESOLVE's own claim timestamp may sit and still be
# read as the same moment. Clocks are not synchronised; a return predating the claim window is
# not the one being reconciled.
_CLOCK_SKEW = timedelta(minutes=5)

# The ONLY mutation document in this module, and the only one RESOLVE will ever send: no
# returnProcess, returnApproveRequest, returnDeclineRequest, returnRequest, returnCancel,
# returnClose or returnReopen, and no fulfillmentCreate. `write_returns` would permit all of
# those, so the restriction has to be held in code rather than assumed from a scope list.
_RETURN_CREATE_MUTATION = """
mutation ResolveReturnCreate($returnInput: ReturnInput!) {
  returnCreate(returnInput: $returnInput) {
    userErrors { field message }
    return { id status }
  }
}
"""

# Minimal read-back of the created return: only fields the read queries already proved exist.
_RETURN_STATUS_QUERY = """
query ResolveReadReturn($id: ID!) {
  return(id: $id) { id status }
}
"""

# Deterministic, provider-layer reason mapping. The 2026-07 `ReturnReason` enum was introspected
# live from this store and has NO `DAMAGED` value — the closest supported reason is `DEFECTIVE`,
# so damaged/defective/quality_issue map there rather than to an invented enum value. Categories
# with no honest counterpart map to `OTHER`; nothing is guessed at.
RETURN_REASON_BY_ISSUE_TYPE: dict[str, str] = {
    "damaged": "DEFECTIVE",
    "defective": "DEFECTIVE",
    "quality_issue": "DEFECTIVE",
    "wrong_item": "WRONG_ITEM",
    "missing_item": "OTHER",
    "not_received": "OTHER",
    "other": "OTHER",
}

_DEFAULT_REASON = "OTHER"
_SUPPORTED_REASONS = frozenset(RETURN_REASON_BY_ISSUE_TYPE.values())

# Shopify's `ReturnStatus` enum (2026-07, introspected): CANCELED CLOSED OPEN REQUESTED DECLINED.
# Mapped onto the contract's outcome vocabulary. Anything else is unknown, not approximate.
_OUTCOME_BY_RETURN_STATUS: dict[str, str] = {
    "REQUESTED": "accepted",
    "OPEN": "accepted",
    "CLOSED": "completed",
    "DECLINED": "declined",
    "CANCELED": "cancelled",
}


@dataclass(frozen=True)
class ShopifyReturnRequest(ReturnRequest):
    """
    A canonical ``ReturnRequest`` plus the one Shopify-specific extra a return may carry.

    Deliberately NOT here: the Shopify order id of the RETURN target, the fulfillment line id, the
    quantity and the reason. Those are not request material — they are resolved from Shopify's own
    reads by ``resolve_return_target`` below, so nothing arriving over HTTP could name a return
    target even in principle. What the base request does carry (Task 14C.7E) is the identity of the
    item RESOLVE bought, read off RESOLVE's own persisted order row, which narrows that target but
    never names it. What remains here is an optional store-defined reason, which only RESOLVE's
    server-side code can supply.
    """

    return_reason_definition_id: str | None = None


class ShopifyReturnProvider(ReturnProvider):
    """
    Resolve, open and read back returns against one Shopify store.

    Two injected capabilities, never a credential of its own: the read client answers every
    question RESOLVE asks about a merchant order, and the return-write capability is the only
    object in RESOLVE that will carry the declared `returnCreate` document. Holding no write
    capability is a refusal, not a fallback — this provider will never route a mutation through a
    read client, because a read client no longer has a way to carry one.
    """

    name = "shopify"

    def __init__(
        self,
        client: ShopifyReadOnlyClient,
        write_client: ShopifyReturnWriteClient | None = None,
    ) -> None:
        # The injected clients are the only credential path. No settings, no second token store.
        self._client = client
        self._write = write_client

    # ------------------------------------------------------------------ target resolution
    async def resolve_return_target(self, request: ReturnRequest) -> ReturnTarget:
        """
        Ask Shopify which of its own lines a return would have to name. Submits nothing.

        Four things must all hold before a target exists, and each refusal says only that
        RESOLVE does not know — not that the return is impossible:

          * the RESOLVE order is linked to a canonical Shopify order, persisted server-side;
          * any Shopify identity the RESOLVE order carries is canonical, and names the same kind
            of object it claims to (a Product GID never goes in a Variant field);
          * that order has a shipped fulfillment line matching that identity — read, never
            constructed;
          * exactly one such line can be identified as the one being returned.

        The quantity returned is the whole shipped line, because the only per-line quantity
        RESOLVE can read is `FulfillmentLineItem.quantity`; deciding that FEWER units come back
        needs a canonical source RESOLVE does not have, so it is not offered. Final eligibility
        remains Shopify's to judge, and comes back as a userError rather than being predicted.
        """
        linked_order = request.provider_order_reference
        if not isinstance(linked_order, str) or not linked_order.strip():
            raise ReturnProviderUnavailable(
                "No canonical Shopify order is linked to this RESOLVE order, so there is nothing "
                "for a return to reference. RESOLVE does not guess a merchant order id from a "
                "title, an amount, or a timestamp, and nothing was submitted."
            )
        order_gid = _validated_gid(linked_order, ORDER_GID_PREFIX, "order")

        # RESOLVE's own persisted Shopify identity is validated BEFORE anything is asked of the
        # merchant (Task 14C.7E): a Product GID and a ProductVariant GID copied from RESOLVE's order
        # row, each required to be canonical and to name the kind of object it claims to be. An
        # identity that is present but malformed is a contradiction inside RESOLVE's own data, so it
        # is refused here rather than read about — asking Shopify first would add a call and a way to
        # fail without adding one fact.
        product_reference = _validated_optional_identity(
            request.provider_product_reference, PRODUCT_GID_PREFIX, "product"
        )
        variant_reference = _validated_optional_identity(
            request.provider_variant_reference, VARIANT_GID_PREFIX, "variant"
        )
        # A RESOLVE product id that IS a Shopify product gid narrows the choice too, but only as a
        # fallback (Task 14C.7E): when the order row carries its own authoritative product identity,
        # that is what aims the return, and the selection handle must not be allowed to contradict it
        # — they are the same value for any row 14C.7D wrote, so a difference between them means
        # something was edited outside the identity path.
        # When the product id is NOT a Shopify product gid (a discovery catalog's own provider-native
        # id), it narrows nothing: only an order with exactly one shipped line can then be resolved
        # without guessing which product the issue is about.
        legacy_product_gid = None if product_reference else _legacy_product_gid(request)

        state = await self._run(
            lambda: self._client.read_return_targets(order_gid), mutating=False
        )

        candidates: list[ShopifyShippedFulfillmentLine] = [
            line
            for line in state.shipped_lines
            if isinstance(line.quantity, int)
            and not isinstance(line.quantity, bool)
            and line.quantity >= 1
        ]

        for reference, attribute in (
            (product_reference, "product_id"),
            (variant_reference, "variant_id"),
            (legacy_product_gid, "product_id"),
        ):
            if reference is not None:
                candidates = _narrow_by_order_lines(
                    candidates, state.order_lines, attribute, reference
                )

        if not candidates:
            raise ReturnProviderUnavailable(
                "Shopify reports no shipped line on this order that RESOLVE can identify as the "
                "one being returned, so no return was submitted. An unshipped or unrecognized "
                "line is not a return target."
            )
        if len(candidates) > 1:
            raise ReturnProviderUnavailable(
                f"Shopify reports {len(candidates)} candidate shipped lines on this order, so "
                "which one the issue concerns is not determinable from canonical data. RESOLVE "
                "will not pick one at random, and nothing was submitted."
            )

        line = candidates[0]
        return ReturnTarget(
            provider_order_reference=order_gid,
            provider_line_reference=_validated_fulfillment_line_gid(
                line.fulfillment_line_item_id
            ),
            provider_quantity=int(line.quantity),
            provider_reason=_reason_for(request.issue_type),
            line_quantity=int(line.quantity),
            detail=(
                f"fulfillment {redact(str(line.fulfillment_id))} status "
                f"{redact(str(line.fulfillment_status))}; order returnStatus "
                f"{redact(str(state.return_status))}"
            ),
        )

    # ------------------------------------------------------------------ submission
    async def request_return(
        self, request: ReturnRequest, target: ReturnTarget | None = None
    ) -> ReturnAck:
        """
        Ask Shopify to open a return (`returnCreate`).

        Raises ``ReturnError`` (400) when the target is unusable — always before a request leaves —
        ``ReturnProviderRejected`` when Shopify says no, and ``ReturnOutcomeUnknown`` when the
        submission may have landed and the answer was lost. Returns an ack ONLY when Shopify
        returned a created return id.
        """
        payload = self._build_input(request, target)  # validated before any network call
        write = self._write
        if write is None:
            # No capability, no mutation — and this is a definite refusal, not an unknown outcome:
            # nothing was built, so nothing can have landed. The read client is not a fallback,
            # because it has no write path to fall back to.
            raise ReturnProviderUnavailable(
                "RESOLVE's Shopify return provider was not given its return-write capability, so "
                "no return was submitted and nothing reached Shopify. Submitting through the "
                "read-only client is not possible by construction, and is not attempted."
            )
        data = await self._run(
            lambda: write.submit_return_create(
                _RETURN_CREATE_MUTATION, {"returnInput": payload}
            ),
            mutating=True,
        )

        created = data.get("returnCreate")
        if not isinstance(created, dict):
            # The request went out and the answer cannot be read. Whether a return exists is now
            # unknown — calling that a rejection would assert a fact RESOLVE does not have.
            raise ReturnOutcomeUnknown(
                "Shopify's answer to the return request could not be read, so it is unknown "
                "whether a return was created. RESOLVE will read the order back rather than "
                "submit the same request twice."
            )

        user_errors = created.get("userErrors") or []
        if user_errors:
            raise ReturnProviderRejected(
                "Shopify refused this return request: " + _error_text(user_errors)
            )

        returned = created.get("return")
        if not isinstance(returned, dict) or not returned.get("id"):
            # No refusal and no id: Shopify neither said no nor said what it made. Unknown.
            raise ReturnOutcomeUnknown(
                "Shopify answered the return request with neither a refusal nor a return id, so "
                "the outcome cannot be recorded as a failure or as a requested return."
            )

        reference = str(returned["id"])
        logger.info("Shopify return requested: %s", redact(reference))
        # `accepted` means exactly that: Shopify opened a return. Not approved, labelled,
        # shipped, or completed.
        return ReturnAck(provider_reference=reference, accepted=True)

    async def get_return_status(self, provider_reference: str) -> ReturnStatusInfo:
        """Read back what Shopify now says about a previously requested return."""
        reference = _validated_gid(provider_reference, RETURN_GID_PREFIX, "return")
        data = await self._run(
            lambda: self._client.execute_graphql(_RETURN_STATUS_QUERY, {"id": reference}),
            mutating=False,
        )
        node = data.get("return")
        if not isinstance(node, dict):
            raise ReturnProviderUnavailable(
                "Shopify returned no return record for this reference, so its status is unknown."
            )
        outcome = _outcome_of(node.get("status"))
        if outcome is None:
            raise ReturnProviderRejected(
                f"Shopify reported return status {redact(str(node.get('status')))!r}, which "
                "RESOLVE does not recognize. No outcome was inferred from it."
            )
        return ReturnStatusInfo(
            provider_reference=reference, outcome=outcome, detail=str(node.get("status"))
        )

    # ------------------------------------------------------------------ reconciliation
    async def reconcile_return(self, submission: ReturnSubmission) -> ReturnReconciliation:
        """
        Decide by READING whether a submission whose answer was lost actually created a return.

        Correlation uses only what RESOLVE itself sent and can re-read: the order the return was
        filed against, the quantity, the reason token, and the claim timestamp. Nothing personal to
        the customer participates — and since a 2026-07 return line exposes no pointer back to a
        fulfillment line, a perfect identifier is not available. That is exactly why an
        inconclusive read has to stay unknown instead of being settled by a second mutation.
        """
        order_reference = submission.provider_order_reference
        if not isinstance(order_reference, str) or not order_reference.strip():
            return ReturnReconciliation.no_match(
                "The lost submission recorded no Shopify order to look in, so nothing could be "
                "reconciled. Nothing was re-submitted."
            )
        order_gid = _validated_gid(order_reference, ORDER_GID_PREFIX, "order")

        returns = await self._run(
            lambda: self._client.read_order_returns(order_gid), mutating=False
        )

        since = _claim_floor(submission.submitted_at)
        candidates: list[ShopifyReturn] = []
        for record in returns:
            created_at = _parse_ts(record.created_at)
            if since is not None and created_at is not None and created_at < since:
                continue
            if _line_matches(record, submission):
                candidates.append(record)

        if len(candidates) > 1:
            return ReturnReconciliation.ambiguous(
                f"{len(candidates)} Shopify returns on this order match the lost submission, so "
                "none can safely be adopted as RESOLVE's. This needs a human to decide; the "
                "request was not re-sent."
            )
        if not candidates:
            return ReturnReconciliation.no_match(
                "No Shopify return on this order could be positively matched to the lost "
                "submission. That is not proof the return does not exist, so nothing was "
                "re-submitted."
            )

        found = candidates[0]
        outcome = _outcome_of(found.status)
        if outcome is None:
            return ReturnReconciliation.ambiguous(
                f"Shopify reports a matching return as {redact(str(found.status))!r}, a status "
                "RESOLVE does not recognize, so no outcome was inferred from it."
            )
        return ReturnReconciliation.matched(
            provider_reference=str(found.return_id),
            outcome=outcome,
            detail="Adopted by read-only reconciliation of a submission whose answer was lost.",
        )

    # ------------------------------------------------------------------ payload building
    def _build_input(self, request: ReturnRequest, target: ReturnTarget | None) -> dict:
        """
        Validate the resolved target and assemble the smallest `ReturnInput` Shopify accepts.

        Raises ``ReturnError`` (status 400) for anything unusable, always BEFORE a request is
        made, so a return that cannot be aimed never reaches the merchant.
        """
        if target is None:
            raise ReturnError(
                "A Shopify return requires a target resolved from Shopify's own reads. None was "
                "supplied, so nothing was asked of the merchant.",
                status_code=400,
            )
        if not isinstance(target, ReturnTarget):
            raise ReturnError(
                "The Shopify return target must be a ReturnTarget produced by "
                "resolve_return_target, not a caller-shaped substitute.",
                status_code=400,
            )

        order_gid = _validated_gid(target.provider_order_reference, ORDER_GID_PREFIX, "order")
        line_gid = _validated_fulfillment_line_gid(target.provider_line_reference)

        reason = target.provider_reason
        if not isinstance(reason, str) or reason.strip().upper() not in _SUPPORTED_REASONS:
            raise ReturnError(
                "The Shopify return reason is not one the live 2026-07 schema supports, so "
                "nothing was asked of the merchant.",
                status_code=400,
            )
        reason = reason.strip().upper()

        quantity = target.provider_quantity
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ReturnError(
                "A return quantity must be a whole number of at least 1.", status_code=400
            )
        ceiling = target.line_quantity
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
            raise ReturnError(
                "The Shopify return target carries no shipped quantity to bound the return, so "
                "nothing was asked of the merchant.",
                status_code=400,
            )
        # The ceiling is the units Shopify said shipped on THIS line — `FulfillmentLineItem.
        # quantity`, read back from Shopify, never derived. It is a structural bound (no more can
        # come back than went out), not a verdict on returnability: whether these units may still
        # be returned is Shopify's to decide. `FulfillmentOrderLineItem.remainingQuantity` counts
        # units AWAITING fulfilment, reads 0 on a shipped line, and is absent from this file on
        # purpose — using it as a ceiling would refuse every legitimate return.
        if quantity > ceiling:
            raise ReturnError(
                f"A return of {quantity} units exceeds the {ceiling} units Shopify reports as "
                "shipped on that fulfillment line, so nothing was asked of the merchant.",
                status_code=400,
            )

        line_item: dict = {
            "fulfillmentLineItemId": line_gid,
            "quantity": quantity,
            "returnReasonNote": reason,
        }
        definition_id = (
            request.return_reason_definition_id
            if isinstance(request, ShopifyReturnRequest)
            else None
        )
        if definition_id:
            line_item["returnReasonDefinitionId"] = _validated_gid(
                definition_id, "gid://shopify/ReturnReasonDefinition/", "return reason definition"
            )

        return {
            "orderId": order_gid,
            "returnLineItems": [line_item],
            # 2026-07 declares exchangeLineItems as non-null; RESOLVE does not exchange items.
            "exchangeLineItems": [],
        }

    async def _run(self, call, mutating: bool):
        """
        One Shopify exchange, with either capability's errors normalized onto the return error model.

        `mutating` is what keeps the classification honest. Before a request is sent, a refusal or
        an unreachable store means 'nothing happened' — including this module's own refusals, such
        as a write capability refusing a document it does not declare. After a mutation has been
        sent, the same silence can mean 'Shopify did it and the answer got lost' — and only
        ``ReturnOutcomeUnknown`` says that, which is the state that must never be retried blindly.
        """
        try:
            return await call()
        except ReturnError:
            raise
        except ShopifyGraphQLError as exc:
            if mutating and _indeterminate(exc):
                raise ReturnOutcomeUnknown(
                    "Shopify answered the return request with an error that does not say whether "
                    f"the return was created: {redact(exc.message)}"
                ) from exc
            # Shopify understood the request and said no — deterministic, and nothing exists.
            raise ReturnProviderRejected(redact(exc.message)) from exc
        except ShopifyTransportError as exc:
            # The exchange never produced a usable answer.
            if mutating:
                raise ReturnOutcomeUnknown(
                    "The return request may have reached Shopify before the exchange failed, so "
                    f"its outcome is unknown: {redact(exc.message)}"
                ) from exc
            raise ReturnProviderUnavailable(redact(exc.message)) from exc
        except ShopifyMalformedResponseError as exc:
            # Shopify answered the mutation and RESOLVE could not read the answer. Whether a
            # return exists is unknown; a malformed AUTH reply is not, because nothing was sent.
            if mutating and exc.details.get("phase") == "graphql":
                raise ReturnOutcomeUnknown(
                    "Shopify's answer to the return request was not a readable GraphQL response, "
                    "so it is unknown whether a return was created: "
                    f"{redact(exc.message)}"
                ) from exc
            raise ReturnProviderUnavailable(redact(exc.message)) from exc
        except ShopifyError as exc:
            # Unconfigured, unauthenticated, rate-limited — or refused locally by one of RESOLVE's
            # two capability gates — a document RESOLVE does not declare is refused before a
            # request is built. In every case nothing was ever sent, so this reads as a definite
            # unavailability rather than an unknown outcome, whatever `mutating` says.
            raise ReturnProviderUnavailable(redact(exc.message)) from exc
        except Exception as exc:  # never let an unexpected error read as a success
            if mutating:
                raise ReturnOutcomeUnknown(
                    "The Shopify return provider failed while a return was being submitted, so "
                    f"the outcome is unknown: {redact(str(exc))}"
                ) from exc
            raise ReturnProviderUnavailable(
                f"The Shopify return provider failed unexpectedly: {redact(str(exc))}"
            ) from exc


def _reason_for(issue_type: str | None) -> str:
    """Deterministic IssueType -> supported Shopify reason token; unmappable -> OTHER."""
    if not isinstance(issue_type, str):
        return _DEFAULT_REASON
    return RETURN_REASON_BY_ISSUE_TYPE.get(issue_type.strip().lower(), _DEFAULT_REASON)


def _outcome_of(status: object) -> str | None:
    """Shopify return status -> contract outcome; anything unrecognized stays None."""
    return _OUTCOME_BY_RETURN_STATUS.get(str(status or "").upper())


def _line_matches(record: ShopifyReturn, submission: ReturnSubmission) -> bool:
    """Whether one Shopify return can be identified as the submission RESOLVE sent."""
    lines = list(record.lines or [])
    if not lines:
        return False
    wanted_reason = (submission.provider_reason or "").strip().upper()
    if not wanted_reason:
        return False
    return any(
        line.quantity == submission.provider_quantity
        and (line.reason_note or "").strip().upper() == wanted_reason
        for line in lines
    )


def _claim_floor(submitted_at: object) -> datetime | None:
    """The earliest Shopify timestamp a matched return could carry, allowing for clock skew."""
    if not isinstance(submitted_at, datetime):
        return None
    moment = submitted_at if submitted_at.tzinfo else submitted_at.replace(tzinfo=timezone.utc)
    return moment - _CLOCK_SKEW


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp from Shopify. Unparseable stays None and is never filtered on."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _indeterminate(exc: ShopifyGraphQLError) -> bool:
    """
    Whether a GraphQL error leaves it unknown if the mutation ran.

    A code naming the request (a `userErrors`-style refusal, validation, access denied) is
    Shopify speaking about the request, so nothing was created. An internal, timeout or service
    failure is Shopify failing while handling it, and only Shopify knows how far it got — and an
    error with no code at all says nothing about either, so it is treated the same careful way.
    """
    codes = {
        str((extension or {}).get("code") or "").upper()
        for extension in (exc.extensions or [])
    }
    if codes & {
        "INTERNAL_SERVER_ERROR",
        "TIMEOUT",
        "SERVICE_UNAVAILABLE",
        "GATEWAY_TIMEOUT",
        "THROTTLED",
    }:
        return True
    return not codes or codes == {""}


def _validated_optional_identity(raw: object, prefix: str, label: str) -> str | None:
    """
    A Shopify identity RESOLVE persisted for the bought item, checked for the shape it claims.

    Absent or blank returns None: no narrowing, which is the honest state for an order RESOLVE did
    not buy through a catalog it can name. Present-but-wrong raises instead of being ignored — a
    Product GID in a Variant slot, or a Google catalog id in either, means the row claims Shopify
    knowledge it does not have, and quietly dropping that claim would let a return aim at a line
    nothing identified it as.
    """
    if raw is None or not isinstance(raw, str) or not raw.strip():
        return None
    return _validated_gid(raw, prefix, label)


def _legacy_product_gid(request: ReturnRequest) -> str | None:
    """
    The pre-14C.7E clue: a RESOLVE product id that happens to BE a Shopify Product GID.

    A SerpApi/Google product id is not Shopify identity and must never be treated as one, so it
    narrows nothing here rather than raising — the persisted identity slots are what carries a real
    Shopify product, and this path only ever supplements them.
    """
    value = request.product_id
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    tail = cleaned[len(PRODUCT_GID_PREFIX):]
    if not cleaned.startswith(PRODUCT_GID_PREFIX) or not _GID_TAIL.match(tail):
        return None
    return cleaned


def _narrow_by_order_lines(
    candidates: list[ShopifyShippedFulfillmentLine],
    order_lines: list[ShopifyOrderLineRef],
    attribute: str,
    reference: str,
) -> list[ShopifyShippedFulfillmentLine]:
    """
    Keep only shipped lines whose ORDER line Shopify itself tagged with this reference.

    `FulfillmentLineItem` carries no product or variant of its own; the join runs through the
    `lineItem` it points at. So the narrowing is a two-step read of Shopify's ids — order lines
    whose product/variant field equals this reference, then shipped lines by `order_line_item_id` —
    with no title, price or position involved anywhere.
    """
    matching = {
        line.order_line_item_id
        for line in order_lines
        if getattr(line, attribute) == reference
    }
    return [line for line in candidates if line.order_line_item_id in matching]


def _validated_gid(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReturnError(
            f"A Shopify {label} id is required for this return, and none was provided. Nothing "
            "was asked of the merchant.",
            status_code=400,
        )
    cleaned = value.strip()
    if not cleaned.startswith(prefix) or not _GID_TAIL.match(cleaned[len(prefix) :]):
        raise ReturnError(
            f"The Shopify {label} id {redact(cleaned)!r} is not a canonical Shopify global id "
            f"of the form {prefix}<id>. Nothing was asked of the merchant.",
            status_code=400,
        )
    return cleaned


def _validated_fulfillment_line_gid(value: object) -> str:
    """
    Validate the return target's id family, refusing the neighbouring one by name.

    `FulfillmentOrderLineItem.id` looks almost right and is the wrong object: the live 2026-07
    schema says the field expects a `FulfillmentLineItem.id`. Saying so explicitly beats
    forwarding an id that would fail with a message about someone else's return line.
    """
    if isinstance(value, str) and value.strip().startswith(FULFILLMENT_ORDER_LINE_ITEM_GID_PREFIX):
        raise ReturnError(
            "The return target is a FulfillmentOrderLineItem id, which is not what a Shopify "
            "return references; it needs a FulfillmentLineItem id read from Order.fulfillments. "
            "Nothing was asked of the merchant.",
            status_code=400,
        )
    return _validated_gid(value, FULFILLMENT_LINE_ITEM_GID_PREFIX, "fulfillment line item")


def _error_text(user_errors: object) -> str:
    """Flatten Shopify `userErrors` into one sanitized message. Raw structures are never leaked."""
    items: list[str] = []
    if isinstance(user_errors, list):
        for entry in user_errors:
            if isinstance(entry, dict):
                field = entry.get("field")
                message = str(entry.get("message") or "unspecified error")
                items.append(f"{redact(str(field))}: {redact(message)}" if field else redact(message))
            else:
                items.append(redact(str(entry)))
    if not items:
        items.append("unspecified error")
    return "; ".join(items)


__all__ = [
    "FULFILLMENT_LINE_ITEM_GID_PREFIX",
    "FULFILLMENT_ORDER_LINE_ITEM_GID_PREFIX",
    "ORDER_GID_PREFIX",
    "PRODUCT_GID_PREFIX",
    "RETURN_GID_PREFIX",
    "RETURN_REASON_BY_ISSUE_TYPE",
    "VARIANT_GID_PREFIX",
    "ShopifyReturnProvider",
    "ShopifyReturnRequest",
]
