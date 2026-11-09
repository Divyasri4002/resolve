"""
Tests for Task 14C.7F — authoritative Shopify order pairing.

One question runs through this file: what makes a RESOLVE order and a Shopify order the SAME
purchase, and what must refuse when that cannot be established?

  * a signed event is the only thing that can name a Shopify order, and a signature is still not
    proof of identity — when RESOLVE's own row carries canonical Shopify identity from a purchase
    made out of that store's catalog, Shopify's own order lines must sell the same Product GID and
    Variant GID, at the same store domain, before the pairing is stored;
  * every case where that corroboration is not clean refuses and stores NOTHING: the variant is
    absent, the product is absent, the variant appears on two lines, the line's own product
    disagrees with it, RESOLVE's identity is half-populated, the order was read from another store,
    or Shopify says the line list was not fully returned;
  * similarity is never evidence, and this is checked structurally: the pairing code never even
    reads a title, price, currency, merchant, SKU or timestamp;
  * the browser is not an authority. It receives a derived state, sends no merchant identifier, and
    every attempt to inject one through a request body changes nothing;
  * an unpaired order cannot be returned through a merchant provider; a pairing that arrives later
    — after a restart — is what the signed route exists for.

Hermetic throughout: an in-memory database and instrumented stand-ins for the Shopify client. A
mutation document anywhere in this path is an assertion failure, not a risk.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import pathlib
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.integrations import order_linkage
from app.integrations.order_linkage import handle_linkage_event, sign_body
from app.models.db_models import AuditLogRecord, IntegrationEventRecord, ResolveOrderRecord
from app.models.mission import MissionStatus
from app.routers import missions as missions_module
from app.tools.returns.base import ReturnProviderUnavailable, ReturnRequest
from app.tools.shopify import client as client_module
from app.tools.shopify import linkage as linkage_module
from app.tools.shopify.client import _ORDER_LINE_IDENTITY_QUERY, ShopifyReadOnlyClient
from app.tools.shopify.errors import ShopifyError, ShopifyMalformedResponseError
from app.tools.shopify.linkage import (
    ShopifyLinkageError,
    link_order_to_shopify_order,
)
from app.tools.shopify.models import (
    ShopifyOrderLineIdentity,
    ShopifyShippedFulfillmentLine,
    ShopifyOrderLineRef,
    ShopifyOrderReference,
)
from app.tools.shopify.return_provider import ShopifyReturnProvider
from app.tools.shopify.sanitizer import reset_registered_secrets

from tests.test_refund_execution import (  # noqa: F401
    _register,
    _verified_mission,
    mock_razorpay,
    restart_env,
)

SECRET = "test-pairing-signing-secret"
OWNER = "user_pairing_owner"
STORE_DOMAIN = "resolve-commerce-lab.myshopify.com"
ORDER_GID = "gid://shopify/Order/7990366601503"
OTHER_GID = "gid://shopify/Order/7990366601599"
PRODUCT_GID = "gid://shopify/Product/10622982422815"
VARIANT_GID = "gid://shopify/ProductVariant/52923834990879"
OTHER_PRODUCT_GID = "gid://shopify/Product/10622982717727"
OTHER_VARIANT_GID = "gid://shopify/ProductVariant/52923835973919"
LINE_GID = "gid://shopify/LineItem/18801909727519"

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


@pytest.fixture
def configured(monkeypatch):
    """Enable the signed integration the way a deployment would: one environment value."""
    monkeypatch.setattr(get_settings(), "linkage_integration_secret", SECRET)
    return SECRET


# --------------------------------------------------------------------------- #
# stand-ins
# --------------------------------------------------------------------------- #
def _line(order_line_id: str, product_id, variant_id) -> ShopifyOrderLineRef:
    return ShopifyOrderLineRef(
        order_line_item_id=order_line_id, product_id=product_id, variant_id=variant_id
    )


class PairingReader:
    """
    Stands where the read-only Shopify client stands, and records every question asked of it.

    Deliberately has NO mutation surface: a test that tried to submit something through it would
    fail with AttributeError rather than silently reach a store.
    """

    def __init__(
        self,
        *,
        known=(ORDER_GID, OTHER_GID),
        lines: list[ShopifyOrderLineRef] | None = None,
        all_lines_read: bool = True,
        store_domain: str = STORE_DOMAIN,
        line_error: Exception | None = None,
    ) -> None:
        self.known = set(known)
        self.store_domain = store_domain
        self.all_lines_read = all_lines_read
        self.line_error = line_error
        self.lines = [_line(LINE_GID, PRODUCT_GID, VARIANT_GID)] if lines is None else list(lines)
        self.reference_asks: list[str] = []
        self.line_asks: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.reference_asks) + len(self.line_asks)

    async def read_order_reference(self, order_id: str) -> ShopifyOrderReference:
        self.reference_asks.append(order_id)
        if order_id not in self.known:
            raise ShopifyMalformedResponseError(
                "Shopify knows nothing about that order id, so it cannot be used as a canonical "
                "reference."
            )
        return ShopifyOrderReference(order_id=order_id, name="#1001")

    async def read_order_line_identity(self, order_id: str) -> ShopifyOrderLineIdentity:
        self.line_asks.append(order_id)
        if self.line_error is not None:
            raise self.line_error
        return ShopifyOrderLineIdentity(
            order_id=order_id, lines=list(self.lines), all_lines_read=self.all_lines_read
        )


class TargetProbe:
    """The return-target read a paired order would need. Reaching it here is the failure."""

    def __init__(self, *, lines=None, shipped=None) -> None:
        self.reads: list[str] = []
        self.lines = lines if lines is not None else [_line(LINE_GID, PRODUCT_GID, VARIANT_GID)]
        self.shipped = shipped if shipped is not None else []

    async def read_return_targets(self, order_id: str):
        from app.tools.shopify.models import ShopifyOrderReturnTargets

        self.reads.append(order_id)
        return ShopifyOrderReturnTargets(
            order_id=order_id,
            return_status="NO_RETURN",
            order_lines=list(self.lines),
            shipped_lines=list(self.shipped),
        )


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #
async def _insert_order(
    test_session,
    *,
    mission_id="mission_pair",
    user_id=OWNER,
    shopify_identity: bool = True,
    reference: str | None = None,
    half_identity: bool = False,
) -> str:
    """Persist a confirmed RESOLVE order, optionally carrying 14C.7D's canonical Shopify identity."""
    async with test_session() as session:
        row = ResolveOrderRecord(
            mission_id=mission_id,
            user_id=user_id,
            product_id=PRODUCT_GID if shopify_identity else "1462412133584222299",
            title="Head & Shoulders Anti-Hairfall Shampoo 360ml",
            merchant="RESOLVE Commerce Lab" if shopify_identity else "TabletBox",
            product={"product_id": "x", "title": "Head & Shoulders Anti-Hairfall Shampoo 360ml"},
            amount=Decimal("410.00"),
            currency="USD" if shopify_identity else "INR",
            razorpay_order_id="order_TEST_pair",
            razorpay_payment_id="pay_TEST_pair",
            provider_order_reference=reference,
            shopify_product_gid=PRODUCT_GID if shopify_identity else None,
            shopify_variant_gid=None if half_identity else (VARIANT_GID if shopify_identity else None),
            shopify_store_domain=STORE_DOMAIN if shopify_identity else None,
            status="order_confirmed",
        )
        session.add(row)
        await session.commit()
        return row.order_id


async def _row(test_session, order_id: str) -> ResolveOrderRecord:
    async with test_session() as session:
        return await session.get(ResolveOrderRecord, order_id)


async def _link(
    test_session, reader, *, mission_id="mission_pair", reference=ORDER_GID, user_id=OWNER
):
    """
    Link the way the signed handler does — with the owner read off the persisted row. Passing the
    caller's own subject is not available here, because nothing in this flow has one.
    """
    async with test_session() as session:
        return await link_order_to_shopify_order(
            session,
            mission_id=mission_id,
            actor_user_id=user_id,
            shopify_order_id=reference,
            client=reader,
        )


def _event_body(*, event_id="evt-pair-0001", order_id=None, mission_id=None, **overrides) -> bytes:
    payload = {
        "event_id": event_id,
        "event_type": "resolve.order_linkage.v1",
        "shopify_order_id": ORDER_GID,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "source": "pairing-test-harness",
    }
    if mission_id is not None:
        payload["mission_id"] = mission_id
    if order_id is not None:
        payload["resolve_order_id"] = order_id
    payload.update(overrides)
    return json.dumps(payload, sort_keys=True).encode()


async def _deliver(test_session, reader, body, *, signature=None):
    async with test_session() as session:
        return await handle_linkage_event(
            session,
            body=body,
            signature=sign_body(SECRET, body) if signature is None else signature,
            client=reader,
        )


async def _outcome_rows(test_session):
    async with test_session() as session:
        events = (await session.scalars(select(IntegrationEventRecord))).all()
        audits = (await session.scalars(select(AuditLogRecord))).all()
    return events, audits


# --------------------------------------------------------------------------- #
# 1 + 20 — a legitimate pairing is stored, and only after Shopify's own lines corroborate it
# --------------------------------------------------------------------------- #
async def test_a_shopify_native_order_pairs_after_shopifys_own_lines_agree(restart_env):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader()

    row = await _link(test_session, reader)

    assert row.provider_order_reference == ORDER_GID
    # Two reads, both Shopify's, both about the one order named by the event — never a search for a
    # likely-looking order.
    assert reader.reference_asks == [ORDER_GID]
    assert reader.line_asks == [ORDER_GID]
    persisted = await _row(test_session, order_id)
    assert (persisted.shopify_product_gid, persisted.shopify_variant_gid) == (
        PRODUCT_GID,
        VARIANT_GID,
    )


async def test_return_grounding_reads_the_paired_reference_and_still_sends_nothing(
    restart_env,
):
    """
    STEP 9 #20: the pairing is what the 14C.7E chain was waiting for. Grounding aims at the order
    Shopify confirmed, and this provider is the only place a mutation document exists — so the
    probe is used for the read and `request_return` is never called.
    """
    _make_client, _test_session = restart_env
    probe = TargetProbe(
        shipped=[
            ShopifyShippedFulfillmentLine(
                fulfillment_line_item_id="gid://shopify/FulfillmentLineItem/16492059328799",
                quantity=1,
                order_line_item_id=LINE_GID,
                fulfillment_id="gid://shopify/Fulfillment/7166055481631",
                fulfillment_status="SUCCESS",
            )
        ]
    )
    provider = ShopifyReturnProvider(client=probe)

    target = await provider.resolve_return_target(
        ReturnRequest(
            mission_id="m",
            issue_id="i",
            order_id="o",
            return_id="r",
            product_id=PRODUCT_GID,
            title="Head & Shoulders Anti-Hairfall Shampoo 360ml",
            merchant="RESOLVE Commerce Lab",
            issue_type="damaged",
            provider_order_reference=ORDER_GID,
            provider_product_reference=PRODUCT_GID,
            provider_variant_reference=VARIANT_GID,
        )
    )

    assert target.provider_line_reference == "gid://shopify/FulfillmentLineItem/16492059328799"
    assert target.line_quantity == 1
    assert probe.reads == [ORDER_GID]


# --------------------------------------------------------------------------- #
# 13/14/15/16 — every way the corroboration can fail refuses and stores nothing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "lines, fragment",
    [
        ([_line(LINE_GID, PRODUCT_GID, OTHER_VARIANT_GID)], "not the variant RESOLVE sold"),
        ([_line(LINE_GID, OTHER_PRODUCT_GID, OTHER_VARIANT_GID)], "does not sell the product"),
        ([_line(LINE_GID, OTHER_PRODUCT_GID, VARIANT_GID)], "different product than the one"),
        (
            [
                _line(LINE_GID, PRODUCT_GID, VARIANT_GID),
                _line("gid://shopify/LineItem/18801909727520", PRODUCT_GID, VARIANT_GID),
            ],
            "2 lines of that order",
        ),
        ([], "does not sell the product"),
    ],
    ids=["other-variant", "other-product", "variant-under-another-product", "two-matching-lines", "no-lines"],
)
async def test_a_merchant_order_that_does_not_sell_this_purchase_is_refused(
    restart_env, lines, fragment
):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader(lines=lines)

    with pytest.raises(ShopifyLinkageError) as excinfo:
        await _link(test_session, reader)

    assert fragment in excinfo.value.message
    assert excinfo.value.status_code == 409
    assert (await _row(test_session, order_id)).provider_order_reference is None


async def test_a_multi_line_merchant_order_pairs_on_the_one_line_that_matches(restart_env):
    """
    A second, different line does not make a pairing ambiguous: identity is per line, and exactly
    one line sells what RESOLVE's own row says it sold.
    """
    _make_client, test_session = restart_env
    await _insert_order(test_session)
    reader = PairingReader(
        lines=[
            _line(LINE_GID, OTHER_PRODUCT_GID, OTHER_VARIANT_GID),
            _line("gid://shopify/LineItem/18801909727521", PRODUCT_GID, VARIANT_GID),
        ]
    )

    row = await _link(test_session, reader)
    assert row.provider_order_reference == ORDER_GID


async def test_an_unread_remainder_of_the_lines_proves_nothing(restart_env):
    """
    `hasNextPage` is Shopify saying "there is more". RESOLVE can neither confirm the item is on the
    order nor deny it, so it claims neither and stores nothing.
    """
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader(all_lines_read=False)

    with pytest.raises(ShopifyLinkageError) as excinfo:
        await _link(test_session, reader)

    assert "more lines than RESOLVE read" in excinfo.value.message
    assert (await _row(test_session, order_id)).provider_order_reference is None


async def test_a_half_populated_canonical_identity_is_refused_before_shopify_is_asked(
    restart_env,
):
    """
    STEP 9 #15. The identity mirror columns are written together from one server-side selection, so
    a row carrying a product and no variant is RESOLVE contradicting itself — and that is settled on
    RESOLVE's own data, before a single question is asked about the order's lines.
    """
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session, half_identity=True)
    reader = PairingReader()

    with pytest.raises(ShopifyLinkageError) as excinfo:
        await _link(test_session, reader)

    assert "only part of a Shopify product identity" in excinfo.value.message
    # RESOLVE's own data is enough to refuse, so the merchant order's lines are never read.
    assert reader.line_asks == []
    assert (await _row(test_session, order_id)).provider_order_reference is None


async def test_an_identity_from_another_store_is_not_the_same_purchase(restart_env):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader(store_domain="some-other-store.myshopify.com")

    with pytest.raises(ShopifyLinkageError) as excinfo:
        await _link(test_session, reader)

    assert "different store" in excinfo.value.message
    assert reader.line_asks == []
    assert (await _row(test_session, order_id)).provider_order_reference is None


@pytest.mark.parametrize(
    "error",
    [
        ShopifyMalformedResponseError("Shopify's answer had no lines."),
        ShopifyError("The store did not answer."),
        RuntimeError("the transport blew up"),
    ],
    ids=["malformed", "shopify-error", "unexpected-error"],
)
async def test_a_failure_to_read_the_lines_is_never_a_licence_to_link(restart_env, error):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader(line_error=error)

    with pytest.raises(ShopifyLinkageError) as excinfo:
        await _link(test_session, reader)

    assert excinfo.value.status_code == 503
    assert "nothing was linked" in excinfo.value.message
    assert (await _row(test_session, order_id)).provider_order_reference is None


# --------------------------------------------------------------------------- #
# 3 + 7 — no similarity matching, and nothing to corroborate when there is no catalog identity
# --------------------------------------------------------------------------- #
async def test_an_order_with_no_shopify_identity_pairs_on_existence_and_claims_no_more(
    restart_env,
):
    """
    The dev RESOLVE order came from Google Shopping. Shopify's lines are never consulted, because
    RESOLVE holds no canonical identity to compare — and its Shopify columns stay NULL, which is
    what keeps a return from ever being aimed at a merchant line for a purchase RESOLVE cannot name.
    """
    _make_client, test_session = restart_env
    order_id = await _insert_order(
        test_session, shopify_identity=False, mission_id="mission_google"
    )
    reader = PairingReader()

    row = await _link(test_session, reader, mission_id="mission_google")

    assert row.provider_order_reference == ORDER_GID
    assert reader.line_asks == []
    persisted = await _row(test_session, order_id)
    assert persisted.shopify_product_gid is None
    assert persisted.shopify_variant_gid is None
    assert persisted.shopify_store_domain is None


def test_the_pairing_code_never_reads_a_field_two_purchases_could_share():
    """
    STEP 9's lookalike defence, structural rather than prose: the single writer touches no title,
    amount, currency, merchant, timestamp or SKU at all, so a lookalike order cannot be matched
    into a linkage even in principle.
    """
    tree = ast.parse(inspect.getsource(linkage_module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant
            ) and isinstance(body[0].value.value, str):
                body.pop(0)  # the prose that explains the refusal is not the refusal
    code = ast.unparse(tree)
    for similarity in (".title", "amount", ".currency", ".merchant", "created_at", ".sku"):
        assert similarity not in code, similarity
    # The documents the corroboration is allowed to send are equally blind.
    lowered = _ORDER_LINE_IDENTITY_QUERY.lower()
    for forbidden in (
        "title",
        "price",
        "currency",
        "sku",
        "merchant",
        "createdAt",
        "note",
        "tags",
        "email",
        "phone",
        "address",
    ):
        assert forbidden not in lowered, forbidden
    assert "mutation" not in lowered
    assert "product { id }" in _ORDER_LINE_IDENTITY_QUERY
    assert "variant { id }" in _ORDER_LINE_IDENTITY_QUERY
    assert "hasNextPage" in _ORDER_LINE_IDENTITY_QUERY


# --------------------------------------------------------------------------- #
# 4 + 5 — conflicting and duplicate pairings
# --------------------------------------------------------------------------- #
async def test_one_merchant_order_never_answers_for_two_resolve_orders(restart_env):
    _make_client, test_session = restart_env
    first = await _insert_order(test_session, mission_id="mission_a")
    await _insert_order(test_session, mission_id="mission_b")
    reader = PairingReader()

    await _link(test_session, reader, mission_id="mission_a")
    with pytest.raises(ShopifyLinkageError) as excinfo:
        await _link(test_session, reader, mission_id="mission_b")

    assert "already linked to a different RESOLVE order" in excinfo.value.message
    assert excinfo.value.status_code == 409
    assert (await _row(test_session, first)).provider_order_reference == ORDER_GID


async def test_a_later_event_never_moves_an_established_pairing(restart_env):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    await _link(test_session, PairingReader())

    with pytest.raises(ShopifyLinkageError) as excinfo:
        await _link(test_session, PairingReader(), reference=OTHER_GID)

    assert "not moved by a later write" in excinfo.value.message
    assert (await _row(test_session, order_id)).provider_order_reference == ORDER_GID


async def test_repeating_a_valid_pairing_stores_nothing_and_asks_shopify_nothing(restart_env):
    _make_client, test_session = restart_env
    await _insert_order(test_session)
    reader = PairingReader()
    await _link(test_session, reader)
    calls = reader.calls

    again = await _link(test_session, reader)

    assert again.provider_order_reference == ORDER_GID
    assert reader.calls == calls


# --------------------------------------------------------------------------- #
# 6 + 8 + 9 + 10 + 11 + 12 — the signed route
# --------------------------------------------------------------------------- #
async def test_a_signed_event_pairs_a_shopify_native_order(restart_env, configured):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader()

    status, payload = await _deliver(test_session, reader, _event_body(order_id=order_id))

    assert status == 200
    assert payload["outcome"] == "linked"
    assert (await _row(test_session, order_id)).provider_order_reference == ORDER_GID
    # The handler never writes the column itself: the single writer did, after both reads.
    assert reader.reference_asks == [ORDER_GID]
    assert reader.line_asks == [ORDER_GID]


async def test_a_signed_event_is_refused_when_the_lines_disagree(restart_env, configured):
    """
    A valid signature over an order Shopify really does confirm is still not a pairing when that
    order demonstrably does not sell this purchase.
    """
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader(lines=[_line(LINE_GID, PRODUCT_GID, OTHER_VARIANT_GID)])

    status, payload = await _deliver(test_session, reader, _event_body(order_id=order_id))

    assert status == 409
    assert payload["outcome"] == "rejected"
    assert (await _row(test_session, order_id)).provider_order_reference is None
    events, audits = await _outcome_rows(test_session)
    assert [e.outcome for e in events] == ["rejected"]
    assert any(a.decision == "refused" for a in audits)


async def test_the_same_event_delivered_twice_is_idempotent(restart_env, configured):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader()
    body = _event_body(order_id=order_id)

    first = await _deliver(test_session, reader, body)
    second = await _deliver(test_session, reader, body)

    assert first[1]["outcome"] == "linked"
    assert second[1]["already_processed"] is True
    async with test_session() as session:
        assert await session.scalar(
            select(func.count()).select_from(IntegrationEventRecord)
        ) == 1


async def test_a_reused_event_id_with_a_different_body_is_refused(restart_env, configured):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader()
    await _deliver(test_session, reader, _event_body(order_id=order_id))

    other = await _insert_order(test_session, mission_id="mission_second")
    body = _event_body(order_id=other)
    async with test_session() as session:
        with pytest.raises(order_linkage.LinkageIntegrationError) as excinfo:
            await handle_linkage_event(
                session, body=body, signature=sign_body(SECRET, body), client=reader
            )

    assert excinfo.value.status_code == 409
    assert "different payload" in excinfo.value.message
    assert (await _row(test_session, other)).provider_order_reference is None


@pytest.mark.parametrize(
    "label, signature",
    [("missing", None), ("malformed", "not-a-signature"), ("wrong", "a" * 64)],
    ids=["missing", "malformed", "wrong"],
)
async def test_an_event_that_is_not_properly_signed_pairs_nothing(
    restart_env, configured, label, signature
):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader()

    async with test_session() as session:
        with pytest.raises(order_linkage.LinkageIntegrationError) as excinfo:
            await handle_linkage_event(
                session, body=_event_body(order_id=order_id), signature=signature, client=reader
            )

    assert excinfo.value.status_code == 401, label
    assert (await _row(test_session, order_id)).provider_order_reference is None
    assert reader.calls == 0


@pytest.mark.parametrize("label", ["stale", "future"])
async def test_an_event_outside_the_freshness_window_pairs_nothing(restart_env, configured, label):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    moment = datetime.now(timezone.utc) + (
        timedelta(hours=-2) if label == "stale" else timedelta(hours=2)
    )
    reader = PairingReader()

    body = _event_body(
        event_id=f"evt-pair-{label}", order_id=order_id, issued_at=moment.isoformat()
    )
    async with test_session() as session:
        with pytest.raises(order_linkage.LinkageIntegrationError) as excinfo:
            await handle_linkage_event(
                session, body=body, signature=sign_body(SECRET, body), client=reader
            )

    assert excinfo.value.status_code == 400, label
    assert (await _row(test_session, order_id)).provider_order_reference is None


@pytest.mark.parametrize(
    "extra",
    [
        {"provider_order_reference": ORDER_GID},
        {"shopify_order_gid": ORDER_GID},
        {"amount": "410.00"},
        {"currency": "USD"},
        {"title": "Head & Shoulders Anti-Hairfall Shampoo 360ml"},
        {"merchant": "RESOLVE Commerce Lab"},
        {"razorpay_payment_id": "pay_TEST_pair"},
        {"fulfillment_line_item_gid": "gid://shopify/FulfillmentLineItem/16492059328799"},
        {"return_reason": "DAMAGED"},
        {"user_id": OWNER},
    ],
    ids=[
        "provider_order_reference",
        "shopify_order_gid",
        "amount",
        "currency",
        "title",
        "merchant",
        "razorpay_payment_id",
        "fulfillment_line",
        "return_reason",
        "user_id",
    ],
)
async def test_an_event_carrying_any_authority_field_beyond_the_contract_is_refused(
    restart_env, configured, extra
):
    """
    STEP 2's contract, enforced: two references, an event id, a type, a timestamp and a source — and
    nothing else. A field that could match an order or override ownership is not extra information.
    """
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader()
    body = _event_body(order_id=order_id, **extra)

    async with test_session() as session:
        with pytest.raises(order_linkage.LinkageIntegrationError) as excinfo:
            await handle_linkage_event(
                session, body=body, signature=sign_body(SECRET, body), client=reader
            )

    assert excinfo.value.status_code == 400
    assert (await _row(test_session, order_id)).provider_order_reference is None
    assert reader.calls == 0


async def test_an_event_naming_an_unknown_resolve_order_pairs_nothing(restart_env, configured):
    _make_client, test_session = restart_env
    await _insert_order(test_session)
    reader = PairingReader()

    status, payload = await _deliver(
        test_session, reader, _event_body(order_id="order_does_not_exist")
    )

    assert status == 404
    assert payload["outcome"] == "rejected"


async def test_an_event_naming_an_order_shopify_does_not_confirm_pairs_nothing(
    restart_env, configured
):
    """STEP 9 #6 — an unknown Shopify order is refused, and the event is spent, not retried silently."""
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader(known=())

    status, payload = await _deliver(test_session, reader, _event_body(order_id=order_id))

    assert status == 503
    assert (await _row(test_session, order_id)).provider_order_reference is None


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "   ",
        "1001",
        "#1001",
        "gid://shopify/LineItem/18801909727519",
        "gid://shopify/Order/abc",
        "gid://shopify/Order/7990366601503/line_items",
        "order_44ec95eb8e58",
    ],
    ids=[
        "empty",
        "spaces",
        "bare-number",
        "order-name",
        "line-item-gid",
        "non-numeric",
        "path-suffix",
        "resolve-order-id",
    ],
)
async def test_a_reference_that_is_not_one_canonical_shopify_order_id_is_refused_early(
    restart_env, reference
):
    """STEP 9 #8, widened: nothing that is not exactly a canonical order id reaches the store."""
    _make_client, test_session = restart_env
    await _insert_order(test_session)
    reader = PairingReader()

    with pytest.raises(ShopifyLinkageError) as excinfo:
        await _link(test_session, reader, reference=reference)

    assert excinfo.value.status_code in (400, 409)
    assert reader.calls == 0


async def test_an_event_naming_a_mission_pairs_the_order_that_belongs_to_it(restart_env, configured):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader()

    status, payload = await _deliver(test_session, reader, _event_body(mission_id="mission_pair"))

    assert status == 200
    assert payload["resolve_order_id"] == order_id
    assert (await _row(test_session, order_id)).provider_order_reference == ORDER_GID


# --------------------------------------------------------------------------- #
# 17 + 18 + 19 — delayed arrival, restart safety, and the unpaired order
# --------------------------------------------------------------------------- #
async def test_a_pairing_that_arrives_after_a_restart_is_stored_once_and_read_back(
    restart_env, configured
):
    """
    STEP 6 and STEP 9 #17/#18: order first, app restarts, event later. The linkage is a fact in
    storage, so nothing about the delay needs to be reconstructed.
    """
    make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader()

    async with make_client() as client:
        before = await _deliver(test_session, reader, _event_body(order_id=order_id))
    assert before[1]["outcome"] == "linked"

    after_restart = await _row(test_session, order_id)
    assert after_restart.provider_order_reference == ORDER_GID

    repeat = await _deliver(
        test_session, reader, _event_body(event_id="evt-pair-later", order_id=order_id)
    )
    assert repeat[1]["outcome"] == "already_linked"
    assert (await _row(test_session, order_id)).provider_order_reference == ORDER_GID


async def test_an_unpaired_order_still_refuses_a_return_before_asking_shopify(restart_env):
    """
    STEP 9 #19 and STEP 5: with no canonical pairing the executor cannot aim a return, and it says
    so without touching the network.
    """
    _make_client, _test_session = restart_env
    probe = TargetProbe()

    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await ShopifyReturnProvider(client=probe).resolve_return_target(
            ReturnRequest(
                mission_id="m",
                issue_id="i",
                order_id="o",
                return_id="r",
                product_id=PRODUCT_GID,
                title="Head & Shoulders Anti-Hairfall Shampoo 360ml",
                merchant="RESOLVE Commerce Lab",
                issue_type="damaged",
                provider_order_reference=None,
                provider_product_reference=PRODUCT_GID,
                provider_variant_reference=VARIANT_GID,
            )
        )

    assert "No canonical Shopify order is linked" in excinfo.value.message
    assert probe.reads == []


# --------------------------------------------------------------------------- #
# 4 (STEP 4/5 surface) + 22 (STEP 8/12 trust boundary) — the client sees a derived state only
# --------------------------------------------------------------------------- #
async def test_the_client_sees_a_derived_state_and_never_a_merchant_identifier(restart_env):
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)

    async with test_session() as session:
        awaiting = missions_module._order_response(
            await session.get(ResolveOrderRecord, order_id)
        )
    assert awaiting["merchant_order_linked"] is False
    assert awaiting["merchant_order_linkage"] == "awaiting_linkage"
    assert "no merchant order, shipping, or delivery exists" in awaiting["fulfillment_note"]

    await _link(test_session, PairingReader())
    async with test_session() as session:
        linked = missions_module._order_response(
            await session.get(ResolveOrderRecord, order_id)
        )
    assert linked["merchant_order_linked"] is True
    assert linked["merchant_order_linkage"] == "linked"
    assert "RESOLVE has not asserted shipping, delivery, or a return" in linked["fulfillment_note"]
    # The merchant-side id itself never crosses to the client: a value the browser holds is a value
    # the browser can try to hand back.
    assert ORDER_GID not in json.dumps(linked)
    assert "provider_order_reference" not in linked
    assert "shopify" not in json.dumps(
        {k: v for k, v in linked.items() if k in ("merchant_order_linked", "merchant_order_linkage")}
    )


def test_the_derivation_is_a_function_of_the_linkage_column_and_nothing_else():
    """The state is computed, never stored: `provider_order_reference` stays the only answer."""
    source = inspect.getsource(missions_module._merchant_order_linkage)
    assert "provider_order_reference" in source
    for forbidden in ("execute_graphql", "get_shopify_client", "db.commit", "await ", "broadcast"):
        assert forbidden not in source, forbidden

    cases = [
        (None, True, "awaiting_linkage"),
        (ORDER_GID, True, "linked"),
        (None, False, "unlinked"),
        (ORDER_GID, False, "linked"),
    ]
    for reference, shopify_native, expected in cases:
        row = _bare_row(
            provider_order_reference=reference,
            **(
                {
                    "shopify_product_gid": PRODUCT_GID,
                    "shopify_variant_gid": VARIANT_GID,
                    "shopify_store_domain": STORE_DOMAIN,
                }
                if shopify_native
                else {}
            ),
        )
        assert missions_module._merchant_order_linkage(row) == expected, (
            reference,
            shopify_native,
        )


def _bare_row(**overrides) -> ResolveOrderRecord:
    """An unsaved but real `ResolveOrderRecord`, so the derivation reads the mapped attributes."""
    fields = dict(
        mission_id="mission_bare",
        user_id=OWNER,
        product_id="1462412133584222299",
        title="TabletBox tablet",
        merchant="TabletBox",
        product={},
        amount=Decimal("410.00"),
        currency="INR",
        razorpay_order_id="order_T",
        razorpay_payment_id="pay_T",
        status="order_confirmed",
        provider_order_reference=None,
        shopify_product_gid=None,
        shopify_variant_gid=None,
        shopify_store_domain=None,
    )
    fields.update(overrides)
    return ResolveOrderRecord(**fields)


async def test_no_request_body_can_pair_an_order_or_restate_its_state(
    restart_env, mock_razorpay, install_mock_discovery, sample_products
):
    """
    STEP 9 #22 and STEP 12: the order lifecycle accepts nothing authoritative. A forged linkage, a
    forged merchant id and a forged derived state are all ignored, and the row keeps saying what it
    actually is.
    """
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        for forged in (
            {"provider_order_reference": ORDER_GID},
            {"shopify_order_gid": ORDER_GID},
            {"merchant_order_linkage": "linked"},
            {"merchant_order_linked": True},
            {"status": "order_confirmed"},
        ):
            created = await client.post(f"/missions/{mission_id}/order", json=forged)
            assert created.status_code == 200, created.text
            payload = created.json()
            assert payload["merchant_order_linked"] is False
            # Mock discovery is Google Shopping: RESOLVE holds no catalog identity for it, so it is
            # honestly "unlinked" rather than "awaiting" — and never "linked".
            assert payload["merchant_order_linkage"] == "unlinked"

            confirmed = await client.post(f"/missions/{mission_id}/order/confirm", json=forged)
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["merchant_order_linked"] is False

            fetched = (await client.get(f"/missions/{mission_id}/order")).json()
            assert fetched["merchant_order_linked"] is False
            assert fetched["merchant_order_linkage"] == "unlinked"

        async with test_session() as session:
            row = await session.scalar(
                select(ResolveOrderRecord).where(ResolveOrderRecord.mission_id == mission_id)
            )
            assert row.provider_order_reference is None


# --------------------------------------------------------------------------- #
# structural: no schema, no new status, no new mutation, no secrets, single writer intact
# --------------------------------------------------------------------------- #
def test_the_paired_state_needed_no_schema_and_adds_no_status_value():
    from app.database import _SCHEMA_UPGRADES

    assert {name for name, _ddl in _SCHEMA_UPGRADES["resolve_orders"]} == {
        "provider_order_reference",
        "shopify_product_gid",
        "shopify_variant_gid",
        "shopify_store_domain",
    }
    values = {member.value for member in MissionStatus}
    # The lifecycle this task needed is derived, so the stored vocabulary is untouched: no status
    # here says anything about a merchant order or a pairing.
    assert not [value for value in values if "merchant" in value or "link" in value], values


def test_the_new_read_is_a_query_only_and_is_covered_by_the_existing_safety_scans():
    """
    A new Shopify read must be added to the guards, not slipped past them: no mutation, no PII, and
    the privacy scan that enumerates every document RESOLVE can send.
    """
    assert _ORDER_LINE_IDENTITY_QUERY.strip().startswith("query ResolveReadOrderLineIdentity")
    module_source = inspect.getsource(client_module)
    literals = [
        node.value
        for node in ast.walk(ast.parse(module_source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not [text for text in literals if text.strip().startswith("mutation ")], "a new mutation"

    privacy = (BACKEND_ROOT / "tests" / "test_privacy_foundation.py").read_text(encoding="utf-8")
    assert "_ORDER_LINE_IDENTITY_QUERY" in privacy, "the PII scan does not cover the new read"
    returns = (BACKEND_ROOT / "tests" / "test_shopify_return_provider.py").read_text(
        encoding="utf-8"
    )
    assert "_ORDER_LINE_IDENTITY_QUERY" in returns, "the mutation scan does not cover the new read"

    reads = {
        name
        for name, member in inspect.getmembers(ShopifyReadOnlyClient, inspect.isfunction)
        if name.startswith("read_")
    }
    assert {"read_order_reference", "read_order_line_identity"} <= reads
    for forbidden in ("execute_graphql_mutation", "submit_return", "create_order", "update_order"):
        assert not hasattr(ShopifyReadOnlyClient, forbidden), forbidden


def test_the_pairing_added_no_second_writer_and_no_second_caller():
    """
    14C.7B pinned this and 14C.7F must not quietly widen it: `linkage.py` is the only module that
    assigns a RESOLVE order's merchant reference, and the signed handler is the only production
    caller of it. Checked from the source, so a new site fails here and in the existing pin.
    """
    writers: set[str] = set()
    callers: set[str] = set()
    for path in BACKEND_ROOT.rglob("*.py"):
        if "tests" in path.parts or path.name.startswith("scratch"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        relative = path.relative_to(BACKEND_ROOT).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Attribute)
                and node.targets[0].attr == "provider_order_reference"
                and getattr(node.targets[0].value, "id", "") == "order"
            ):
                writers.add(relative)
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", ""))
                == "link_order_to_shopify_order"
            ):
                callers.add(relative)

    assert writers == {"app/tools/shopify/linkage.py"}, writers
    assert callers == {"app/integrations/order_linkage.py"}, callers
    # The merchant store is asked about a pairing only from inside the shopify package.
    mission_source = inspect.getsource(missions_module)
    integration_source = (BACKEND_ROOT / "app" / "routers" / "integrations.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "read_order_line_identity",
        "link_order_to_shopify_order",
        "ShopifyReadOnlyClient",
        "execute_graphql",
    ):
        assert forbidden not in mission_source, forbidden
        assert forbidden not in integration_source, forbidden


async def test_an_audit_of_a_pairing_names_no_credential_and_no_raw_body(restart_env, configured):
    """STEP 13: attempts are recorded, and nothing recorded is a secret or the raw payload."""
    _make_client, test_session = restart_env
    order_id = await _insert_order(test_session)
    reader = PairingReader(lines=[_line(LINE_GID, PRODUCT_GID, OTHER_VARIANT_GID)])
    body = _event_body(order_id=order_id)

    status, _payload = await _deliver(test_session, reader, body)
    assert status == 409

    events, audits = await _outcome_rows(test_session)
    assert audits, "a refused pairing must still be auditable"
    blob = json.dumps(
        [
            [a.actor, a.action, a.reason, a.decision] for a in audits
        ]
        + [[e.outcome, e.detail, e.body_sha256] for e in events],
        default=str,
    )
    assert SECRET not in blob
    assert hashlib_body_fragment(body) not in blob
    assert any("evt-pair-0001" in (a.reason or "") for a in audits)
    assert any("refused" in (a.reason or "") for a in audits)


def hashlib_body_fragment(body: bytes) -> str:
    """A distinctive slice of the raw payload, to prove the body itself is not stored verbatim."""
    return body.decode()[:40]


def test_no_merchant_identifier_or_authority_value_reaches_the_browser_code():
    """
    STEP 12's boundary, read off the frontend source: the client may display the server's derived
    state and nothing else. Skipped when the frontend is not mounted, exactly as the existing
    frontend scans do.
    """
    root = os.environ.get("FRONTEND_ROOT")
    if not root:
        pytest.skip("frontend not mounted; this scan runs with -e FRONTEND_ROOT=/frontend")
    frontend = pathlib.Path(root)
    sources = [
        path
        for pattern in ("components/**/*.tsx", "app/**/*.tsx", "lib/**/*.ts")
        for path in frontend.glob(pattern)
    ]
    assert sources, "the frontend mount is empty"
    for path in sources:
        source = path.read_text(encoding="utf-8", errors="replace")
        code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        code = re.sub(r"//.*", "", code)
        for forbidden in ("provider_order_reference", "shopify_order_id", "link_order_to_shopify"):
            assert forbidden not in code, f"{path.name}: {forbidden}"
        # Nothing the client sends may carry a merchant-side identifier.
        for posted in re.findall(r"JSON\.stringify\(\{[^}]*\}", code):
            assert "shopify" not in posted, f"{path.name}: {posted}"
    panel = (frontend / "components" / "workspace" / "OrderPanel.tsx").read_text(
        encoding="utf-8", errors="replace"
    )
    # The panel displays the server's derived state and never computes or submits one: the value is
    # read off the response, and no POST body in this file carries a merchant identifier.
    assert "MERCHANT_ORDER_COPY[order.merchant_order_linkage]" in panel
    assert "awaiting merchant order linkage" in panel.lower()
    assert "onCreate" in panel and "shopify" not in panel.lower().replace("merchant_order", "")
