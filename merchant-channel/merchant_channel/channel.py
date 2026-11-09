"""
The channel's one workflow: handoff -> correlation -> create -> read back -> verify -> pair.

Order of operations is the design, so it is stated once and then followed:

1. validate the handoff — and reject it if the merchant's price is published in a currency RESOLVE
   does not charge in, because pairing an INR payment against a USD order is the same money lie as
   converting it, told from the other side;
2. derive a deterministic correlation id, and record the intent in the channel's own outbox;
3. **ask permission of the outbox before creating anything.** A previous attempt whose outcome is
   unknown is not a green light: it is the exact condition in which a duplicate Shopify order is
   born, and the 2026-07 schema offers no idempotency key to protect against one;
4. create (draft order stamped with the correlation in `poNumber`, then completed) — with no payment
   argument, because nothing was paid to the channel;
5. **read back, always**, even when the mutation answered happily. The response is a claim; the
   read-back is Shopify's answer to "which orders carry this correlation", and zero / one / many are
   three different futures;
6. verify identity against the handoff: correlation, store, currency, product GID, variant GID,
   quantity;
7. only then build, sign and deliver `resolve.order_linkage.v1`, and remember the exact bytes so a
   redelivery is byte-identical under the same `event_id` rather than a fresh claim;
8. and if RESOLVE answers that settled claim "no" — `reclaim_linkage`, Task 14C.7L. The pairing may
   be stated again under a new attempt number, over the same purchase and the same merchant order,
   after reading the store again. It is a separate door, not a looser version of the first one: a
   reclaim cannot create, cannot re-open the outbox for a second order, and cannot carry a different
   handoff under the old correlation.

RESOLVE is not consulted about any of steps 1-6 and gains nothing from them until step 7 arrives
signed. What RESOLVE does with it is already fixed by 14C.7B/7F/7G: verify, corroborate, store. And
what it does with a step-8 re-claim is the same rule, because a new attempt is a new claim to
adjudicate rather than an appeal against the old one.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from merchant_channel import documents as docs
from merchant_channel.config import ChannelConfig, ChannelConfigError
from merchant_channel.correlation import (
    canonical_payload,
    correlation_id as derive_correlation,
    payload_hash as derive_payload_hash,
)
from merchant_channel.gateway import GatewayDefiniteFailure, GatewayOutcomeUnknown
from merchant_channel.linkage import (
    LinkageContractError,
    build_event,
    deliver_event,
    serialize_event,
    sign_event,
)
from merchant_channel.outbox import (
    AMBIGUOUS,
    CONFIRMED,
    MATCHED,
    NO_MATCH,
    RECONCILED,
    LINKAGE_DELIVERY_UNKNOWN,
    LINKAGE_NOT_SENT,
    LINKAGE_REFUSED,
    LINKAGE_SENT,
    OUTCOME_UNKNOWN as OUTCOME_UNKNOWN_STATE,
    PENDING,
    AmbiguousOutcome,
    EventRecordConflict,
    Outbox,
)

# The outcomes a caller can act on. Each one says what the channel knows, and nothing more.
CONFIRMED_LINKED = "CONFIRMED_LINKED"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
AWAITING_RECONCILIATION = "AWAITING_RECONCILIATION"
FAILED = "FAILED"
REFUSED_IDENTITY = "REFUSED_IDENTITY"
REFUSED_LINKAGE = "REFUSED_LINKAGE"
DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
REFUSED_HANDOFF = "REFUSED_HANDOFF"

PRODUCT_GID_PREFIX = "gid://shopify/Product/"
VARIANT_GID_PREFIX = "gid://shopify/ProductVariant/"


class HandoffError(Exception):
    """The purchase handoff is not usable. Nothing was created and nothing was sent."""


@dataclass(frozen=True)
class PurchaseHandoff:
    """
    What the shopping side hands the channel.

    Product and variant GIDs, a quantity, the currency the payment actually moved in, and the RESOLVE
    order id the pairing will name. There is no customer, address, email, phone or name field here —
    and no `provider_order_reference`, because a channel that had been handed the Shopify order id
    would not have needed to create anything.
    """

    resolve_order_id: str
    shopify_product_gid: str
    shopify_variant_gid: str
    quantity: int
    currency: str

    def canonical_payload(self) -> str:
        return canonical_payload(self)


@dataclass
class Outcome:
    status: str
    correlation_id: str | None = None
    external_order_id: str | None = None
    linkage_state: str | None = None
    reason: str | None = None
    event_id: str | None = None
    candidates: int = 0

    def __repr__(self) -> str:
        return (
            f"Outcome(status={self.status!r}, correlation_id={self.correlation_id!r}, "
            f"external_order_id={self.external_order_id!r}, linkage_state={self.linkage_state!r}, "
            f"candidates={self.candidates}, reason={self.reason!r})"
        )


def _is_canonical(value: object, prefix: str) -> bool:
    tail = value[len(prefix) :] if isinstance(value, str) and value.startswith(prefix) else ""
    return bool(tail) and tail.isdigit()


class PurchaseChannel:
    """The merchant-owned half of a Shopify-native RESOLVE purchase."""

    def __init__(
        self,
        config: ChannelConfig,
        gateway,
        outbox: Outbox,
        *,
        clock=None,
        http_client=None,
    ) -> None:
        if not config.expected_currency:
            raise ChannelConfigError(
                "the channel must know which currency RESOLVE charges in, and it was not configured"
            )
        self._config = config
        self._gateway = gateway
        self._outbox = outbox
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._client = http_client

    # ------------------------------------------------------------------ handoff
    def _validate(self, handoff: PurchaseHandoff) -> None:
        if not isinstance(handoff, PurchaseHandoff):
            raise HandoffError("the channel only acts on a PurchaseHandoff")
        if not handoff.resolve_order_id or len(handoff.resolve_order_id) > 64:
            raise HandoffError("a purchase handoff must name one RESOLVE order id")
        if not _is_canonical(handoff.shopify_product_gid, PRODUCT_GID_PREFIX):
            raise HandoffError(
                "the handoff must carry a canonical Shopify Product GID; a catalog handle, a SKU or "
                "a title cannot identify what was bought"
            )
        if not _is_canonical(handoff.shopify_variant_gid, VARIANT_GID_PREFIX):
            raise HandoffError(
                "the handoff must carry a canonical Shopify ProductVariant GID; a Product GID is "
                "not a variant"
            )
        if isinstance(handoff.quantity, bool) or not isinstance(handoff.quantity, int) or handoff.quantity < 1:
            raise HandoffError("a purchase handoff needs a whole quantity of at least 1")
        # Phase A, Option C: the merchant order and RESOLVE's payment must be denominated in the same
        # currency, because nothing here converts and nothing here may invent a rate.
        if handoff.currency != self._config.expected_currency:
            raise HandoffError(
                f"the handoff is priced in {handoff.currency!r} but this deployment charges in "
                f"{self._config.expected_currency!r}. RESOLVE does not convert, and this channel "
                "will not create a merchant order in one currency only to pair it with a payment in "
                "another. Nothing was created."
            )

    def _expected(self, order: docs.MerchantOrder, handoff: PurchaseHandoff) -> str | None:
        """
        Everything the read-back must agree with before a pairing may be asserted.

        Each mismatch says what disagrees, so a human can act on it. Nothing here "best matches" a
        line, and nothing here falls back to a title or an amount.
        """
        if order.po_number != derive_correlation(handoff):
            return "the order Shopify returned is not stamped with this correlation"
        if order.currency != self._config.expected_currency:
            return (
                f"Shopify priced the order in {order.currency!r}, not "
                f"{self._config.expected_currency!r}, so it is not the purchase RESOLVE paid for"
            )
        matching = [line for line in order.lines if line.variant_id == handoff.shopify_variant_gid]
        if len(matching) != 1:
            return (
                f"Shopify names the variant RESOLVE sold on {len(matching)} line(s) of that order, "
                "so which line this purchase is cannot be determined"
            )
        if matching[0].product_id != handoff.shopify_product_gid:
            return "Shopify puts that variant on a different product than the one RESOLVE sold"
        if matching[0].quantity != handoff.quantity:
            return (
                f"Shopify sold {matching[0].quantity} unit(s) of that variant, not the "
                f"{handoff.quantity} the purchase handoff named"
            )
        return None

    # ------------------------------------------------------------------ workflow
    async def fulfil(self, handoff: PurchaseHandoff) -> Outcome:
        try:
            self._validate(handoff)
        except HandoffError as exc:
            return Outcome(status=REFUSED_HANDOFF, reason=str(exc))

        correlation = derive_correlation(handoff)
        self._outbox.reserve(
            correlation_id=correlation,
            resolve_order_id=handoff.resolve_order_id,
            payload_hash=derive_payload_hash(handoff),
        )
        record = self._outbox.get(correlation)
        if record.linkage == LINKAGE_SENT:
            # Already paired: re-deliver the same signed bytes under the same event_id, which RESOLVE
            # answers from storage. Creating again would be the duplicate this whole module is for.
            return await self._deliver(correlation, handoff)

        if record.status == PENDING:
            # A process decided to send and never recorded an answer. The honest reading of that is
            # unknown, not "it did not happen", so it becomes unknown before anything else is decided.
            self._outbox.mark_outcome_unknown(
                correlation, "a previous attempt left PENDING unresolved"
            )
            record = self._outbox.get(correlation)

        if record.status == OUTCOME_UNKNOWN_STATE and not record.may_create:
            # B2's hard rule, enforced at the only place it can be: no second create, and no silent
            # reconciliation either. `reconcile()` is the way out, because only a read of the
            # merchant's store can say whether the first attempt produced an order. An unknown that
            # a reconciliation left closed (the request did leave; absence is not proven) stays
            # here — unless a human authorization is recorded on the row, which `may_create`
            # reports and `begin_create` consumes.
            return Outcome(
                status=OUTCOME_UNKNOWN,
                correlation_id=correlation,
                reason=(
                    "the previous attempt's outcome is still unknown"
                    f" ({record.failure_reason or 'no reason recorded'}), so nothing was created "
                    "and nothing was sent; reconcile by reading Shopify before trying again"
                ),
            )

        if record.may_create:
            try:
                self._outbox.begin_create(correlation)
            except AmbiguousOutcome as exc:
                # A concurrent caller claimed the row between the read above and the claim. The
                # loser sees the winner's row and reaches Shopify for nothing.
                return Outcome(
                    status=OUTCOME_UNKNOWN,
                    correlation_id=correlation,
                    reason=(
                        "another attempt claimed this purchase first, so its outcome is not this "
                        f"caller's to guess: {exc}"
                    ),
                )
            try:
                # Before the request is handed to the transport: from here on, the row records that
                # a request for this correlation left the channel. That single fact is what makes
                # every later empty search non-probative (M20.1's fail-closed recovery rule).
                self._outbox.mark_request_sent(correlation)
                await self._gateway.create_order(
                    correlation_id=correlation,
                    variant_gid=handoff.shopify_variant_gid,
                    quantity=handoff.quantity,
                )
            except GatewayOutcomeUnknown as exc:
                self._outbox.mark_outcome_unknown(correlation, str(exc))
                return Outcome(
                    status=OUTCOME_UNKNOWN,
                    correlation_id=correlation,
                    reason=f"the create answer was lost, so whether an order exists is unknown: {exc}",
                )
            except GatewayDefiniteFailure as exc:
                self._outbox.mark_failed(correlation, str(exc))
                return Outcome(status=FAILED, correlation_id=correlation, reason=str(exc))

        return await self._read_back_and_link(correlation, handoff)

    async def reconcile(self, correlation: str, handoff: PurchaseHandoff | None = None) -> Outcome:
        """
        The only way out of an unknown outcome: ask Shopify what it has, and decide from that.

        Matched  -> confirm, then pair.
        No match -> Shopify reports nothing. Whether that reopens a create is decided by the
                    outbox, not here: a definitive failure or a never-sent attempt makes the
                    create deterministic; an unknown whose request left keeps the row closed,
                    because an eventually-consistent search does not prove absence.
        Many     -> ambiguous; stop. Picking one candidate is a human decision.
        """
        if self._outbox.get(correlation) is None:
            return Outcome(
                status=FAILED, correlation_id=correlation, reason="no outbox row for that correlation"
            )
        orders = await self._gateway.read_back(correlation)
        if len(orders) == 1:
            self._outbox.mark_reconciled(correlation, MATCHED)
            if handoff is None:
                return Outcome(
                    status=AWAITING_RECONCILIATION,
                    correlation_id=correlation,
                    external_order_id=orders[0].order_id,
                    reason="reconciled to exactly one order; a handoff is needed to pair it",
                )
            return await self._verify_and_link(correlation, handoff, orders[0])
        if len(orders) == 0:
            self._outbox.mark_reconciled(correlation, NO_MATCH)
            record = self._outbox.get(correlation)
            if record.status == RECONCILED:
                vouch = (
                    "no request for this correlation ever left this channel"
                    if record.sent_at is None
                    else "the previous attempt ended in a definitive refusal"
                )
                reason = (
                    f"Shopify has no order carrying this correlation and {vouch}, so the absence "
                    "is vouched for and a create is now a deterministic next step rather than a "
                    "second purchase"
                )
            elif record.status == CONFIRMED:
                reason = (
                    "Shopify currently reports no order carrying this correlation, but an order "
                    "for it was already found, verified and confirmed; an eventually-consistent "
                    "search does not un-see a verified order, and no authorization applies to a "
                    "confirmed purchase"
                )
            else:
                reason = (
                    "Shopify currently reports no order carrying this correlation, but the create "
                    "request did leave and an eventually-consistent search does not prove absence; "
                    "the row stays closed. Another create requires a recorded human authorization "
                    "(Outbox.authorize_create) after an out-of-band check of the store."
                )
            return Outcome(
                status=AWAITING_RECONCILIATION,
                correlation_id=correlation,
                reason=reason,
            )
        self._outbox.mark_reconciled(correlation, AMBIGUOUS)
        return Outcome(
            status=AWAITING_RECONCILIATION,
            correlation_id=correlation,
            candidates=len(orders),
            reason=(
                f"Shopify returned {len(orders)} orders carrying this correlation, which is not a "
                "result to pick from. Nothing was created and nothing was sent."
            ),
        )

    async def reclaim_linkage(
        self, correlation: str, handoff: PurchaseHandoff
    ) -> Outcome:
        """
        Re-state a pairing RESOLVE refused, under a new claim identity — and under nothing else.

        The situation this exists for is measured, not hypothetical (14C.7K §12): the merchant order
        is real and stands, the correlation is unchanged, and RESOLVE answered the first claim
        "no". That answer is permanent *for that event id* — a settled replay is answered from
        storage and asks Shopify nothing — so the pairing can only ever arrive again as a new
        event. Deriving ids from the pairing alone meant the channel had no way to make one.

        What is deliberately not reused here is `fulfil`. A reclaim is not a purchase attempt, so it
        never reaches the create path, never consults `may_create`, and cannot reopen a row for a
        second order. The invariant it has to preserve is the stronger one:

            one correlation_id -> at most one Shopify order, however many claims are made about it.

        So the order is re-read from the merchant store first (local memory of an order id is not
        evidence, and a reclaim that trusted its own outbox could pair against an order that is no
        longer there or no longer single), then re-verified against the handoff exactly as the first
        attempt was, and only then is the attempt rotated and the event signed.
        """
        try:
            self._validate(handoff)
        except HandoffError as exc:
            return Outcome(status=REFUSED_HANDOFF, reason=str(exc))

        derived = derive_correlation(handoff)
        if derived != correlation:
            # A reclaim that accepted a different handoff would be a new purchase wearing an old
            # correlation — which is how a second Shopify order gets a legitimate-looking name.
            return Outcome(
                status=REFUSED_IDENTITY,
                correlation_id=correlation,
                reason=(
                    "the handoff supplied does not hash to this correlation, so it is a different "
                    "purchase and not a reclaim of it. Nothing was created and nothing was sent."
                ),
            )

        record = self._outbox.get(correlation)
        if record is None:
            return Outcome(
                status=FAILED, correlation_id=correlation, reason="no outbox row for that correlation"
            )
        if record.status == PENDING:
            self._outbox.mark_outcome_unknown(
                correlation, "a previous attempt left PENDING unresolved"
            )
            record = self._outbox.get(correlation)
        if record.status == OUTCOME_UNKNOWN_STATE:
            return Outcome(
                status=OUTCOME_UNKNOWN,
                correlation_id=correlation,
                reason=(
                    "the create behind this pairing has no known outcome, so there is no single "
                    "merchant order to reclaim a claim about; reconcile it first"
                ),
            )
        if record.linkage == LINKAGE_SENT:
            return Outcome(
                status=CONFIRMED_LINKED,
                correlation_id=correlation,
                external_order_id=record.external_order_id,
                event_id=record.event_id,
                reason=(
                    "this pairing is already linked, so a reclaim would only be a second claim about "
                    "a purchase RESOLVE has already accepted. Nothing was sent."
                ),
            )
        if record.linkage == LINKAGE_DELIVERY_UNKNOWN:
            return Outcome(
                status=DELIVERY_UNKNOWN,
                correlation_id=correlation,
                external_order_id=record.external_order_id,
                event_id=record.event_id,
                reason=(
                    "the previous claim was never answered, which is not a refusal; re-deliver the "
                    "same bytes under the same event id, because a new identity here would put two "
                    "live claims on one purchase"
                ),
            )
        if record.linkage != LINKAGE_REFUSED:
            return Outcome(
                status=FAILED,
                correlation_id=correlation,
                reason=(
                    f"linkage state {record.linkage} has no spent claim to recover; a reclaim needs "
                    "a refusal"
                ),
            )

        orders = await self._gateway.read_back(correlation)
        if len(orders) == 0:
            # Nothing to pair. The refusal is left exactly as it was: reopening the row for a create
            # from here would turn "RESOLVE said no" into "the channel will now go buy it again".
            return Outcome(
                status=AWAITING_RECONCILIATION,
                correlation_id=correlation,
                reason=(
                    "the merchant store holds no order carrying this correlation, so there is "
                    "nothing to reclaim a claim about. Nothing was created, nothing was sent, and "
                    "the refusal stands."
                ),
            )
        if len(orders) > 1:
            self._outbox.mark_reconciled(correlation, AMBIGUOUS)
            return Outcome(
                status=AWAITING_RECONCILIATION,
                correlation_id=correlation,
                candidates=len(orders),
                reason=(
                    f"{len(orders)} orders carry this correlation, so a reclaim could not say which "
                    "purchase it was re-claiming. Nothing was sent."
                ),
            )

        order = orders[0]
        shop_name, _shop_currency = await self._gateway.shop_identity()
        if self._config.expected_shop_name and shop_name != self._config.expected_shop_name:
            reason = (
                f"the store answering is {shop_name!r}, not the configured "
                f"{self._config.expected_shop_name!r}, so this order was not read from the merchant "
                "the handoff came from. Nothing was sent."
            )
            self._outbox.set_linkage(correlation, LINKAGE_REFUSED, failure_reason=reason)
            return Outcome(
                status=REFUSED_IDENTITY,
                correlation_id=correlation,
                external_order_id=order.order_id,
                reason=reason,
            )
        problem = self._expected(order, handoff)
        if problem is not None:
            self._outbox.set_linkage(correlation, LINKAGE_REFUSED, failure_reason=problem)
            return Outcome(
                status=REFUSED_IDENTITY,
                correlation_id=correlation,
                external_order_id=order.order_id,
                reason=problem,
            )

        self._outbox.begin_linkage_attempt(correlation)
        self._outbox.mark_confirmed(correlation, order.order_id)
        return await self._deliver(correlation, handoff, order=order)

    async def _read_back_and_link(self, correlation: str, handoff: PurchaseHandoff) -> Outcome:
        """B6: the mutation's own answer never closes this. The order is what the read says it is."""
        try:
            orders = await self._gateway.read_back(correlation)
        except GatewayOutcomeUnknown as exc:
            reason = f"the read-back answer was lost, so whether an order exists is unknown: {exc}"
            self._outbox.mark_outcome_unknown(correlation, reason)
            return Outcome(status=OUTCOME_UNKNOWN, correlation_id=correlation, reason=reason)
        except GatewayDefiniteFailure as exc:
            # The read was refused, not the create. The create returned without error, so an order
            # may well exist; a definite failure here is about the read, and saying FAILED would
            # reopen the row for the second order.
            reason = f"the read-back was refused ({exc}), so whether an order exists is unknown"
            self._outbox.mark_outcome_unknown(correlation, reason)
            return Outcome(status=OUTCOME_UNKNOWN, correlation_id=correlation, reason=reason)
        if len(orders) == 0:
            self._outbox.mark_outcome_unknown(
                correlation, "read-back found no order carrying the correlation"
            )
            return Outcome(
                status=OUTCOME_UNKNOWN,
                correlation_id=correlation,
                reason=(
                    "the create did not error but Shopify reports no order carrying this "
                    "correlation, so the outcome is unknown and will not be retried blind"
                ),
            )
        if len(orders) > 1:
            self._outbox.mark_reconciled(correlation, AMBIGUOUS)
            return Outcome(
                status=AWAITING_RECONCILIATION,
                correlation_id=correlation,
                candidates=len(orders),
                reason=(
                    f"{len(orders)} orders carry this correlation, so which one this purchase is "
                    "cannot be determined. Nothing was sent."
                ),
            )
        return await self._verify_and_link(correlation, handoff, orders[0])

    async def _verify_and_link(
        self, correlation: str, handoff: PurchaseHandoff, order: docs.MerchantOrder
    ) -> Outcome:
        try:
            shop_name, _shop_currency = await self._gateway.shop_identity()
        except (GatewayOutcomeUnknown, GatewayDefiniteFailure) as exc:
            # The order exists and matches, but the store answering cannot be identified, so
            # pairing it would be trusting a read whose provenance is unverified. Unknown, not
            # refused: a refusal would read as "wrong store" and never recover from an outage.
            reason = (
                f"the store identity could not be read ({exc}), so whether this order may be "
                "paired is unknown; nothing was sent"
            )
            self._outbox.mark_outcome_unknown(correlation, reason)
            return Outcome(status=OUTCOME_UNKNOWN, correlation_id=correlation, reason=reason)
        if self._config.expected_shop_name and shop_name != self._config.expected_shop_name:
            reason = (
                f"the store answering is {shop_name!r}, not the configured "
                f"{self._config.expected_shop_name!r}, so this order was not read from the merchant "
                "the handoff came from. Nothing was sent."
            )
            self._outbox.set_linkage(correlation, LINKAGE_REFUSED, failure_reason=reason)
            return Outcome(
                status=REFUSED_IDENTITY,
                correlation_id=correlation,
                external_order_id=order.order_id,
                reason=reason,
            )
        problem = self._expected(order, handoff)
        if problem is not None:
            self._outbox.set_linkage(correlation, LINKAGE_REFUSED, failure_reason=problem)
            return Outcome(
                status=REFUSED_IDENTITY,
                correlation_id=correlation,
                external_order_id=order.order_id,
                reason=problem,
            )
        self._outbox.mark_confirmed(correlation, order.order_id)
        return await self._deliver(correlation, handoff, order=order)

    async def _deliver(
        self, correlation: str, handoff: PurchaseHandoff, *, order: docs.MerchantOrder | None = None
    ) -> Outcome:
        record = self._outbox.get(correlation)
        if order is None:
            if record is None or not record.external_order_id:
                return Outcome(
                    status=AWAITING_RECONCILIATION,
                    correlation_id=correlation,
                    reason="nothing has been confirmed for this correlation yet",
                )
            order = docs.MerchantOrder(
                order_id=record.external_order_id,
                name=None,
                po_number=correlation,
                currency=self._config.expected_currency,
                source_name=None,
                financial_status=None,
                created_at=None,
            )

        try:
            body = self._body_for(correlation, handoff, order)
        except LinkageContractError as exc:
            # Fail closed on a half-persisted event: no honest bytes exist for it, so nothing is
            # sent and the row is left untouched for a human, rather than a fresh body being minted
            # under a stored id.
            return Outcome(
                status=REFUSED_LINKAGE,
                correlation_id=correlation,
                external_order_id=order.order_id,
                reason=f"{exc}. Nothing was sent and the row was left as it was.",
            )
        try:
            signature = sign_event(self._config.linkage_secret.reveal(), body)
        except LinkageContractError as exc:
            self._outbox.set_linkage(correlation, LINKAGE_NOT_SENT, failure_reason=str(exc))
            return Outcome(
                status=REFUSED_LINKAGE,
                correlation_id=correlation,
                external_order_id=order.order_id,
                reason=str(exc),
            )
        event = json.loads(body.decode("utf-8"))
        # Persisted before the attempt, never after it: the bytes that go out are the bytes a retry
        # must resend, and an answer that never arrived cannot be allowed to invent a new claim.
        try:
            self._outbox.record_event(
                correlation, event_id=event["event_id"], event_body=body.decode("utf-8")
            )
        except EventRecordConflict as exc:
            return Outcome(
                status=REFUSED_LINKAGE,
                correlation_id=correlation,
                external_order_id=order.order_id,
                reason=f"{exc}. Nothing was sent.",
            )
        try:
            status_code, payload = await deliver_event(
                self._client,
                base_url=self._config.resolve_base_url,
                body=body,
                signature=signature,
            )
        except Exception as exc:  # noqa: BLE001 - a lost answer is not a refusal
            self._outbox.set_linkage(
                correlation, LINKAGE_DELIVERY_UNKNOWN, event_id=event["event_id"]
            )
            return Outcome(
                status=DELIVERY_UNKNOWN,
                correlation_id=correlation,
                external_order_id=order.order_id,
                event_id=event["event_id"],
                reason=(
                    f"RESOLVE's answer was lost ({type(exc).__name__}); the same signed bytes will "
                    "be re-sent under the same event_id, and no new Shopify order will be created"
                ),
            )

        if status_code == 200 and payload.get("outcome") in ("linked", "already_linked"):
            self._outbox.set_linkage(
                correlation, LINKAGE_SENT, event_id=event["event_id"], event_body=body.decode()
            )
            return Outcome(
                status=CONFIRMED_LINKED,
                correlation_id=correlation,
                external_order_id=order.order_id,
                event_id=event["event_id"],
                linkage_state=payload["outcome"],
            )
        if 500 <= status_code < 600:
            # A 5xx is RESOLVE saying it could not answer the question — most often because it could
            # not confirm this order with Shopify right now. That is not a verdict on the pairing, so
            # it is recorded as the lost answer it is and re-sent with the same bytes under the same
            # id. If RESOLVE has in fact settled the event as rejected, the retry will be told so.
            self._outbox.set_linkage(
                correlation, LINKAGE_DELIVERY_UNKNOWN, event_id=event["event_id"]
            )
            return Outcome(
                status=DELIVERY_UNKNOWN,
                correlation_id=correlation,
                external_order_id=order.order_id,
                event_id=event["event_id"],
                reason=(
                    f"RESOLVE could not answer (HTTP {status_code}: "
                    f"{str(payload.get('detail') or '')[:200]}), so the pairing is unresolved rather "
                    "than refused; the same signed bytes will be re-sent under the same event_id and "
                    "no new Shopify order will be created"
                ),
            )
        self._outbox.set_linkage(
            correlation,
            LINKAGE_REFUSED,
            event_id=event["event_id"],
            event_body=body.decode(),
            failure_reason=f"RESOLVE answered HTTP {status_code}",
        )
        return Outcome(
            status=REFUSED_LINKAGE,
            correlation_id=correlation,
            external_order_id=order.order_id,
            event_id=event["event_id"],
            reason=(
                f"RESOLVE refused the pairing with HTTP {status_code}: "
                f"{str(payload.get('detail') or payload.get('outcome') or '')[:200]}. The merchant "
                "order stays as it is; nothing here decides to create another one."
            ),
        )

    def _body_for(self, correlation: str, handoff: PurchaseHandoff, order: docs.MerchantOrder) -> bytes:
        """
        The exact bytes to sign — reused verbatim on every redelivery of the same attempt.

        A resend that re-stamped `issued_at` would carry the same `event_id` over different bytes, and
        RESOLVE would correctly answer 409 "already recorded for a different payload".

        The attempt number comes out of the row, never out of an argument: the only way to send fresh
        bytes for one purchase is to have rotated the row deliberately through `reclaim_linkage`.
        """
        record = self._outbox.get(correlation)
        if record is not None and (record.event_body or record.event_id):
            if not (record.event_body and record.event_id):
                # The m16 defect's exact shape: an id whose body is gone, or a body whose id is
                # gone. Resending one without the other, or minting a replacement body under a
                # stored id, is the same-id/different-payload mismatch the receiver refuses — so
                # no bytes are produced at all and the caller sees a refusal.
                raise LinkageContractError(
                    f"correlation {correlation} holds a half-persisted event "
                    f"(event_id={'present' if record.event_id else 'missing'}, "
                    f"event_body={'present' if record.event_body else 'missing'}), so no honest "
                    "bytes exist for this attempt"
                )
            return record.event_body.encode("utf-8")
        event = build_event(
            correlation_id=correlation,
            shopify_order_id=order.order_id,
            resolve_order_id=handoff.resolve_order_id,
            issued_at=self._clock().isoformat(),
            source=self._config.source,
            attempt=(record.attempt if record is not None else 1),
        )
        return serialize_event(event)
