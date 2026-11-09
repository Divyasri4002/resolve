"""
The purchase gates, shared by every caller that may reach ``PurchaseChannel.fulfil``.

There is deliberately ONE implementation of the trust boundary. The gated scratch purchase
runner was its first consumer; the merchant-side trigger service will be the second. A gate
copied into a second process is not a gate — it is a divergent copy of a safety rule waiting
for the two copies to disagree, so the rules live here and callers supply only the
deployment-specific values they are responsible for pinning (the expected store, the
expected currency, the callback base url).

What may never be parameterized away — the invariant every consumer inherits unchanged:

    product and variant identity is read from RESOLVE's own API, never accepted from a
    caller, and every gate fails closed.

``fetch_order_facts`` is the only order source: it machine-authenticates to RESOLVE with the
service identity and reads the persisted rows the application itself wrote. ``order_gates``
turns those served facts into a licence to purchase, and ``outbox_state_gates`` asks the
outbox — with the actual correlation and payload hash — whether any row that already exists
plainly licenses a create. A row that does not stops the run: recovery states (reconcile,
reclaim, human ``authorize_create``) are deliberate separate steps no gate caller performs.

Nothing here creates, sends, retries, reconciles, or reclaims. A passed gate is permission to
ask the channel; the channel's own outbox discipline remains the only thing standing between
a lost answer and a second order.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import httpx

from merchant_channel.channel import PRODUCT_GID_PREFIX, VARIANT_GID_PREFIX
from merchant_channel.outbox import (
    AMBIGUOUS,
    LINKAGE_DELIVERY_UNKNOWN,
    LINKAGE_REFUSED,
    LINKAGE_SENT,
    OUTCOME_UNKNOWN as OUTBOX_UNKNOWN,
    PENDING,
    Outbox,
)

# The machine credential for the two read-only preflight reads: RESOLVE serves them only to
# the configured service identity, carried in a dedicated header that is not Authorization.
SERVICE_TOKEN_ENV = "MERCHANT_RESOLVE_SERVICE_TOKEN"
SERVICE_TOKEN_HEADER = "X-Resolve-Service-Token"

# The production shape of ResolveOrderRecord.order_id ("order_" + 12 hex chars, missions.py):
# a stricter check than the event contract's 64-char limit, because a purchase acts only on
# an order the application's own checkout flow created.
ORDER_ID_SHAPE = re.compile(r"^order_[0-9a-f]{12}$")
# States a resolve_orders row can only reach AFTER payment verification succeeded.
POST_PAYMENT_STATUSES = ("order_created", "order_confirmed")

# Why an outbox gate refused, stamped on the report so the caller can classify the refusal
# without re-deriving it from problem strings. Two honest categories exist:
#   - ambiguous_prior_execution: something about a previous attempt is unresolved, so whether
#     a merchant order exists is unknown. This is NOT a pre-execution refusal and NOT proof
#     of an order; the only truthful report is "unknown, reconcile deliberately".
#   - merchant_order_exists: the row itself is the record of a verified create (linkage
#     states are only ever set after a confirmed create; a confirmed or reconciled-matched
#     row records one). The refusal is because the order already exists.
# Anything else that fails here carries no category and is reported unknown, fail-closed.
OUTBOX_REFUSAL_AMBIGUOUS = "ambiguous_prior_execution"
OUTBOX_REFUSAL_ORDER_EXISTS = "merchant_order_exists"


# --------------------------------------------------------------------- gates

@dataclass
class GateReport:
    checks: list[tuple[str, bool]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # Set only by outbox refusals that fit a known honest category (see the constants above);
    # None means the refusal carries no classification and callers must fail closed.
    refusal_category: str | None = None

    @property
    def passed(self) -> bool:
        return not self.problems

    def ok(self, name: str) -> None:
        self.checks.append((name, True))

    def fail(self, problem: str, name: str = "gate refused") -> None:
        self.checks.append((name, False))
        self.problems.append(problem)

    def absorb(self, other: "GateReport") -> bool:
        self.checks.extend(other.checks)
        self.problems.extend(other.problems)
        return other.passed


@dataclass(frozen=True)
class OrderFacts:
    """One RESOLVE order, as the application's own API served it. Never caller-supplied."""

    resolve_order_id: str
    mission_id: str
    status: str | None
    currency: str | None
    razorpay_order_id: str | None
    razorpay_payment_id: str | None
    merchant_order_linked: bool
    product_gid: str | None
    variant_gid: str | None
    store_domain: str | None
    order_product_id: str | None
    selected_product_id: str | None
    title: str | None


async def fetch_order_facts(
    base_url: str,
    *,
    mission_id: str,
    service_token: str | None = None,
) -> tuple[OrderFacts | None, GateReport]:
    """
    The trusted read-only order source: RESOLVE's own two endpoints, nothing else.

    Both answers are the application serving its own persisted rows. A purchase accepts GIDs
    from nowhere else; if the served selection lacks its Shopify identity, the run fails here.

    The read is machine-authenticated: RESOLVE serves these two reads only to the configured
    service identity. The token comes from the environment (``MERCHANT_RESOLVE_SERVICE_TOKEN``)
    unless the caller supplies it; without it the run fails closed BEFORE any request — asking
    without the credential could only produce a 404 whose cause the caller would have to guess
    at, and guessing is how mistakes get retried. The token value itself is never printed.
    """
    report = GateReport()
    token = service_token if service_token is not None else os.environ.get(SERVICE_TOKEN_ENV, "")
    if not token:
        report.fail(
            f"{SERVICE_TOKEN_ENV} is not set, so RESOLVE cannot authenticate this machine read; "
            "the runner fails closed instead of asking without credentials"
        )
        return None, report
    base = base_url.rstrip("/")
    headers = {SERVICE_TOKEN_HEADER: token}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            order_response = await client.get(
                f"{base}/missions/{mission_id}/order", headers=headers
            )
            checkout_response = await client.get(
                f"{base}/missions/{mission_id}/shopping/checkout", headers=headers
            )
    except httpx.HTTPError as exc:
        report.fail(
            f"RESOLVE's API could not be asked about this mission ({type(exc).__name__}); "
            "nothing was created and nothing was sent"
        )
        return None, report
    if order_response.status_code != 200:
        report.fail(
            f"GET /missions/{mission_id}/order answered HTTP {order_response.status_code}; the "
            "runner acts only on an order the application itself serves"
        )
        return None, report
    if checkout_response.status_code != 200:
        report.fail(
            f"GET /missions/{mission_id}/shopping/checkout answered HTTP "
            f"{checkout_response.status_code}"
        )
        return None, report
    report.ok("RESOLVE served the order and the checkout review it owns")

    order_payload = order_response.json() if isinstance(order_response.json(), dict) else {}
    checkout_payload = checkout_response.json() if isinstance(checkout_response.json(), dict) else {}
    intent = checkout_payload.get("shopping_intent") or {}
    selected = intent.get("selected_product") if isinstance(intent.get("selected_product"), dict) else {}
    product = order_payload.get("product") if isinstance(order_payload.get("product"), dict) else {}
    if not selected:
        report.fail(
            "the mission has no persisted selection, so no trustworthy product/variant identity "
            "exists; caller-supplied GIDs are never accepted, so nothing can proceed"
        )
        return None, report
    facts = OrderFacts(
        resolve_order_id=str(order_payload.get("order_id") or ""),
        mission_id=str(order_payload.get("mission_id") or ""),
        status=order_payload.get("status"),
        currency=order_payload.get("currency"),
        razorpay_order_id=order_payload.get("razorpay_order_id"),
        razorpay_payment_id=order_payload.get("razorpay_payment_id"),
        merchant_order_linked=bool(order_payload.get("merchant_order_linked")),
        product_gid=selected.get("shopify_product_gid"),
        variant_gid=selected.get("shopify_variant_gid"),
        store_domain=selected.get("shopify_store_domain"),
        order_product_id=product.get("product_id"),
        selected_product_id=selected.get("product_id"),
        title=selected.get("title") or product.get("title"),
    )
    return facts, report


def order_gates(
    facts: OrderFacts,
    *,
    mission_id: str,
    expected_currency: str,
    expected_store_domain: str,
    resolve_order_id: str | None = None,
    quantity: int = 1,
) -> GateReport:
    """
    Everything the handoff needs to be true, checked before any mutation is possible.

    ``expected_currency`` and ``expected_store_domain`` are the caller's own deployment pins:
    the values this deployment is willing to purchase against. The scratch runner pins them to
    its scratch store; the shared module hardcodes nothing, and a facts row that disagrees with
    either pin refuses.
    """
    report = GateReport()
    if ORDER_ID_SHAPE.match(facts.resolve_order_id or ""):
        report.ok(f"canonical RESOLVE order id ({facts.resolve_order_id})")
    else:
        report.fail(
            f"the order id {facts.resolve_order_id!r} is not a canonical RESOLVE order id "
            "(order_<12 hex>); only an order created by the application's own checkout and "
            "Razorpay TEST verification flow is acceptable, and no arbitrary or pre-existing "
            "merchant order can be substituted"
        )
    if resolve_order_id is not None:
        if resolve_order_id == facts.resolve_order_id:
            report.ok("the served order matches the requested order id")
        else:
            report.fail("the order RESOLVE served is not the one that was requested")
    if facts.mission_id == mission_id:
        report.ok("the served order belongs to the requested mission")
    else:
        report.fail("the served order does not belong to the requested mission")
    if facts.razorpay_order_id and facts.razorpay_payment_id:
        report.ok("payment evidence present (server-copied Razorpay TEST references)")
    else:
        report.fail(
            "no payment-verification evidence: razorpay_order_id and razorpay_payment_id are "
            "copied onto the row only by the payment-verify endpoint after server-side HMAC "
            "verification, and at least one is missing. A boolean is not proof."
        )
    if facts.status in POST_PAYMENT_STATUSES:
        report.ok(f"order status is a post-payment state ({facts.status})")
    else:
        report.fail(
            f"order status {facts.status!r} is not a post-payment state; a purchase is only "
            "created for a payment-verified order"
        )
    if facts.merchant_order_linked:
        report.fail(
            "this order already carries a merchant linkage; one RESOLVE order pairs with at "
            "most one merchant order and the receiver would refuse a second"
        )
    else:
        report.ok("no existing merchant linkage on the order")
    if facts.currency == expected_currency:
        report.ok(f"the order is priced in {expected_currency}")
    else:
        report.fail(f"the order is priced in {facts.currency!r}, not {expected_currency!r}; nothing converts")
    for name, value, prefix in (
        ("product", facts.product_gid, PRODUCT_GID_PREFIX),
        ("variant", facts.variant_gid, VARIANT_GID_PREFIX),
    ):
        if value and value.startswith(prefix) and value[len(prefix):].isdigit():
            report.ok(f"canonical Shopify {name} GID from the served selection")
        else:
            report.fail(f"the {name} GID is missing or not a canonical {prefix}<digits> value")
    if facts.product_gid and facts.variant_gid and facts.store_domain:
        report.ok("the served selection carries its complete Shopify identity")
    else:
        report.fail(
            "the served selection carries only part of a Shopify product identity; a half-"
            "populated identity cannot be corroborated and nothing may be created for it"
        )
    if facts.store_domain == expected_store_domain:
        report.ok(f"the selection was read from the expected store ({expected_store_domain})")
    else:
        report.fail(
            f"the selection's store is {facts.store_domain!r}, not {expected_store_domain!r}; a "
            "merchant order created elsewhere could never be corroborated against this row"
        )
    if facts.selected_product_id and facts.order_product_id:
        if facts.selected_product_id == facts.order_product_id:
            report.ok("the order's product identity agrees with the selected catalog item")
        else:
            report.fail(
                "the order's product identity disagrees with the selected catalog item, so the "
                "two views do not describe one purchase"
            )
    else:
        report.fail("either the order or the selection is missing its product identity")
    if isinstance(quantity, bool) or quantity != 1:
        report.fail(
            "the scratch run buys exactly one unit: quantity is pinned to 1 and no caller can "
            "raise it"
        )
    else:
        report.ok("quantity is pinned to exactly 1")
    return report


def outbox_state_gates(
    outbox: Outbox, correlation_id: str, resolve_order_id: str, payload_hash_value: str
) -> GateReport:
    """
    The outbox, asked with the actual correlation and payload hash.

    Any row that does not plainly license a create stops the run. Recovery states (reconcile,
    reclaim, redelivery) are deliberate separate steps this gate's callers never perform.

    Every failure stamps ``refusal_category`` so the caller can report what is actually known:
    ``OUTBOX_REFUSAL_AMBIGUOUS`` (whether a merchant order exists is unknown — a PENDING or
    unknown-outcome row, or a row for a different purchase under the same correlation) or
    ``OUTBOX_REFUSAL_ORDER_EXISTS`` (the row records a verified create — any linkage state,
    a confirmed row, or a reconciled-matched row). A missing category on a failed report
    means unknown, fail-closed.
    """

    def refuse(category: str, problem: str) -> None:
        report.fail(problem)
        report.refusal_category = category

    report = GateReport()
    record = outbox.get(correlation_id)
    if record is None:
        report.ok("no prior outbox row for this correlation")
        return report
    if record.resolve_order_id != resolve_order_id or record.payload_hash != payload_hash_value:
        # The correlation collision is itself unexplained — this run cannot establish anything
        # about its own purchase, so the honest classification is ambiguous, not "exists".
        refuse(
            OUTBOX_REFUSAL_AMBIGUOUS,
            "an outbox row already exists under this correlation for a different purchase; "
            "this run stops rather than act on someone else's purchase",
        )
        return report
    if record.status == PENDING:
        refuse(
            OUTBOX_REFUSAL_AMBIGUOUS,
            "a previous attempt left the outbox row PENDING, so whether its create happened is "
            "unknown; reconcile (a separate deliberate step) is the way out, never a retry",
        )
    elif record.status == OUTBOX_UNKNOWN or record.reconciliation_state == AMBIGUOUS:
        reason = record.failure_reason or "no reason recorded"
        refuse(
            OUTBOX_REFUSAL_AMBIGUOUS,
            f"the previous attempt's outcome is unknown ({reason}); reconcile by reading "
            "Shopify is a separate deliberate step, and this tool does not perform it",
        )
    elif record.linkage == LINKAGE_SENT:
        refuse(
            OUTBOX_REFUSAL_ORDER_EXISTS,
            "this purchase is already linked; a second run would only re-deliver the same "
            "claim, so the scratch tool stops here instead",
        )
    elif record.linkage == LINKAGE_DELIVERY_UNKNOWN:
        # The linkage state is only ever set after a verified create, so the order exists;
        # what is unknown is whether RESOLVE received the claim, not whether the order does.
        refuse(
            OUTBOX_REFUSAL_ORDER_EXISTS,
            "the previous claim was never answered; re-delivering the same bytes is a "
            "deliberate separate step this tool does not perform",
        )
    elif record.linkage == LINKAGE_REFUSED:
        refuse(
            OUTBOX_REFUSAL_ORDER_EXISTS,
            "RESOLVE settled a refusal for this pairing; reclaim is a separate deliberate "
            "decision and never an automatic follow-up to a refusal",
        )
    elif not record.may_create:
        # may_create is False for exactly the states that record an existing order
        # (CONFIRMED, RECONCILED with MATCHED) — the create happened and is on record.
        refuse(
            OUTBOX_REFUSAL_ORDER_EXISTS,
            f"outbox state {record.status} does not license a create",
        )
    else:
        report.ok(f"outbox row is retryable ({record.status}, linkage={record.linkage})")
    return report


__all__ = [
    "GateReport",
    "OrderFacts",
    "ORDER_ID_SHAPE",
    "OUTBOX_REFUSAL_AMBIGUOUS",
    "OUTBOX_REFUSAL_ORDER_EXISTS",
    "POST_PAYMENT_STATUSES",
    "SERVICE_TOKEN_ENV",
    "SERVICE_TOKEN_HEADER",
    "fetch_order_facts",
    "order_gates",
    "outbox_state_gates",
]
