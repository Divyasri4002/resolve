"""
Task 14C.7I Phase B — the merchant-side channel's own bytes, accepted by RESOLVE's real contract.

14C.7G proved the RESOLVE half: a signed `resolve.order_linkage.v1` event, verified against a fixture
store, links exactly one order, survives a restart, replays idempotently, and is refused on conflict.
What it could not prove was the other half, because the channel did not exist: that the bytes a
*merchant-owned* purchase channel actually produces are the bytes this contract accepts.

This file closes that gap without dissolving the boundary. The channel is imported as the separate
package it is (`merchant_channel`, from MERCHANT_CHANNEL_ROOT) and driven by its own code — its own
correlation, its own outbox, its own signing — against its own scripted merchant store. RESOLVE is
the real app over real HTTP with its real route, its real single writer, and the same fixture store
14C.7G used, because RESOLVE must never be trusted to take the channel's word for an order.

So every test here asks the same question in a different shape: does what the channel emits land on
RESOLVE's side unchanged, and does RESOLVE's refusal still stop the channel from claiming more than
it knows? Nothing in this file can reach the dev store: the merchant side is a fake, and RESOLVE's
credential factory is refused, so a real Shopify call fails the run rather than happening.

Hermetic: in-memory database, mocked Razorpay TEST client, fixture merchant store, disposable
per-process signing secret. No real credential appears anywhere, and no real Shopify order exists
anywhere in this file.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
from datetime import datetime, timezone

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import func, select

import app.tools.shopify as shopify_package
from app.config import get_settings
from app.integrations import order_linkage
from app.integrations.order_linkage import (
    SIGNATURE_HEADER as RESOLVE_SIGNATURE_HEADER,
)
from app.integrations.order_linkage import OrderLinkageEvent
from app.main import app
from app.models.db_models import AuditLogRecord, IntegrationEventRecord, ResolveOrderRecord
from app.tools.shopify.sanitizer import reset_registered_secrets

from tests.test_refund_execution import restart_env  # noqa: F401
from tests.test_shopify_discovery_identity import (  # noqa: F401
    PRODUCT_GID,
    STORE,
    VARIANT_GID,
    _mission_with,
    _pay_and_create_order,
    _shopify_result,
    mock_razorpay,
)
from tests.test_shopify_pairing_channel_e2e import (  # noqa: F401
    FixtureStore,
    _purchased,
    store_factory,
)

# Throwaway values generated for this process only. They are not credentials: they never leave the
# test, are never committed, never printed, and the deployed default stays unset.
SECRET = "disposable-acceptance-secret-for-this-process-only"
TOKEN = "shpua_disposable-acceptance-token-not-a-credential"
CHANNEL_ORDER_GID = "gid://shopify/Order/9000000000000001"


def _channel_root() -> pathlib.Path:
    """
    Where the merchant-side package lives, or a loud failure.

    A silent skip here would be the worst possible outcome: this file's whole job is to prove the
    channel's output satisfies RESOLVE's contract, and a green "skipped" would look like that proof
    had been made. Mount the package (`-v <repo>/merchant-channel:/merchant -e
    MERCHANT_CHANNEL_ROOT=/merchant`) or watch the run fail.
    """
    candidates = []
    configured = os.environ.get("MERCHANT_CHANNEL_ROOT")
    if configured:
        candidates.append(pathlib.Path(configured))
    candidates.append(pathlib.Path(__file__).resolve().parents[2] / "merchant-channel")
    for path in candidates:
        if (path / "merchant_channel" / "channel.py").is_file():
            return path
    raise AssertionError(
        "the merchant-side channel package is not visible to this test run. Set "
        "MERCHANT_CHANNEL_ROOT (and mount the directory read-only) so the acceptance of its exact "
        "signed bytes can be verified. Nothing here guesses at the channel instead."
    )


_CHANNEL_ROOT = _channel_root()
if str(_CHANNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(_CHANNEL_ROOT))

from merchant_channel.channel import PurchaseChannel, PurchaseHandoff  # noqa: E402
from merchant_channel.config import ChannelConfig, Secret  # noqa: E402
from merchant_channel.correlation import correlation_id as correlation_for  # noqa: E402
from merchant_channel.fake_store import FakeAdminStore  # noqa: E402
from merchant_channel.linkage import LINKAGE_ROUTE, SIGNATURE_HEADER  # noqa: E402
from merchant_channel.linkage import build_event, serialize_event  # noqa: E402
from merchant_channel.outbox import LINKAGE_REFUSED, LINKAGE_SENT, Outbox  # noqa: E402

ROUTE = "/integrations/shopify/order-linkage"


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


@pytest.fixture(autouse=True)
def _no_live_shopify(monkeypatch):
    """
    Neither half may touch the dev store: RESOLVE's client factories are refused outright.

    Both names, since 14C.7O — refusing only the read factory would let the return provider's
    write capability be built against the real credential while the test believed it was hermetic.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "No 14C.7I acceptance test may construct the live Shopify client. The merchant store "
            "is a fake and the dev store's only order (#1001) is not being paired by anything."
        )

    monkeypatch.setattr(shopify_package, "get_shopify_client", refuse)
    monkeypatch.setattr(shopify_package, "get_shopify_clients", refuse)


@pytest.fixture
def resolve_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "linkage_integration_secret", SECRET)
    return SECRET


@pytest.fixture
def channel_config(resolve_secret):
    return ChannelConfig(
        store_domain=STORE,
        api_version="2026-07",
        access_token=Secret(TOKEN),
        linkage_secret=Secret(resolve_secret),
        resolve_base_url="http://test",
        expected_currency="INR",
        expected_shop_name="RESOLVE Commerce Lab",
        source="merchant-purchase-channel",
    )


def _handoff(resolve_order_id: str, **overrides) -> PurchaseHandoff:
    values = {
        "resolve_order_id": resolve_order_id,
        "shopify_product_gid": PRODUCT_GID,
        "shopify_variant_gid": VARIANT_GID,
        "quantity": 1,
        "currency": "INR",
    }
    values.update(overrides)
    return PurchaseHandoff(**values)


def _channel(
    config: ChannelConfig, outbox_path: str, client, merchant: FakeAdminStore
) -> PurchaseChannel:
    return PurchaseChannel(config, merchant, Outbox(outbox_path), http_client=client)


async def _stored_text(test_session) -> str:
    """Everything RESOLVE kept about this pairing, as one searchable string."""
    async with test_session() as session:
        events = list((await session.execute(select(IntegrationEventRecord))).scalars().all())
        audits = list((await session.execute(select(AuditLogRecord))).scalars().all())
    return json.dumps(
        [
            [
                str(cell)
                for cell in (
                    row.event_id,
                    row.event_type,
                    row.outcome,
                    row.body_sha256,
                    row.resolve_order_id,
                    row.provider_order_reference,
                )
            ]
            for row in events
        ]
        + [[str(cell) for cell in (row.action, row.reason)] for row in audits]
    )


def _order_columns(row: ResolveOrderRecord) -> list[str]:
    return list(row.__mapper__.columns.keys())


# --------------------------------------------------------------------------- #
# the contract, seen from both ends at once
# --------------------------------------------------------------------------- #
def test_the_two_sides_agree_on_the_route_and_the_header_without_being_told_to():
    """
    The channel was written against RESOLVE's contract, not a copy of it.

    A header spelled differently on one side is a 401 nobody explains; a route spelled differently is
    a 404 the merchant mistakes for an outage. Both are measured here instead of trusted — and the
    route is read off the running app, not from a constant.
    """
    assert SIGNATURE_HEADER == RESOLVE_SIGNATURE_HEADER
    mounted = [route.path for route in app.routes if isinstance(route, APIRoute)]
    assert [ROUTE] == [path for path in mounted if path.endswith("/order-linkage")]
    assert LINKAGE_ROUTE == ROUTE


def test_the_channels_event_validates_against_the_real_model_with_no_extra_field():
    body = serialize_event(
        build_event(
            correlation_id="RESOLVE-order_accept001-000000000000",
            shopify_order_id=CHANNEL_ORDER_GID,
            resolve_order_id="order_accept001",
            issued_at=datetime.now(timezone.utc).isoformat(),
            source="merchant-purchase-channel",
        )
    )
    sent = json.loads(body)
    event = OrderLinkageEvent.model_validate_json(body)

    assert OrderLinkageEvent.model_config["extra"] == "forbid"
    # `mission_id` is the contract's seventh field; the channel always knows the order itself.
    assert set(sent) == set(OrderLinkageEvent.model_fields) - {"mission_id"}
    assert event.shopify_order_id == CHANNEL_ORDER_GID
    assert event.resolve_order_id == "order_accept001"
    assert event.source == "merchant-purchase-channel"


def test_the_channels_event_names_no_money_and_no_customer():
    """Option C's boundary is visible in the payload: the pairing says nothing about payment."""
    body = serialize_event(
        build_event(
            correlation_id="RESOLVE-order_accept002-000000000000",
            shopify_order_id=CHANNEL_ORDER_GID,
            resolve_order_id="order_accept002",
            issued_at=datetime.now(timezone.utc).isoformat(),
            source="merchant-purchase-channel",
        )
    ).decode("utf-8")
    lowered = body.lower()
    for needle in ("amount", "price", "currency", "email", "phone", "name", "address", "customer"):
        assert needle not in lowered, f"the signed pairing carries {needle!r}"


# --------------------------------------------------------------------------- #
# the channel, end to end, against the real route
# --------------------------------------------------------------------------- #
async def test_the_channels_own_signed_bytes_link_a_real_resolve_order(
    restart_env, install_mock_discovery, store_factory, channel_config, tmp_path, mock_razorpay
):
    store = store_factory(known=(CHANNEL_ORDER_GID,))
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, "accept@shop.test")
        order_id = order["order_id"]
        merchant = FakeAdminStore()

        outcome = await _channel(
            channel_config, str(tmp_path / "outbox.sqlite3"), client, merchant
        ).fulfil(_handoff(order_id))

        assert outcome.status == "CONFIRMED_LINKED"
        assert outcome.external_order_id == CHANNEL_ORDER_GID
        # RESOLVE's own authenticated read says the same thing the channel thinks it did.
        after = (await client.get(f"/missions/{mission_id}/order")).json()
        assert after["merchant_order_linked"] is True
        assert after["merchant_order_linkage"] == "linked"

    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order_id)
        assert row.provider_order_reference == CHANNEL_ORDER_GID
        # The channel's correlation is its own business: RESOLVE has no column for it, which is the
        # same fact stated twice — no schema was needed for this architecture, and a merchant-side
        # retry cannot look like a RESOLVE-side state change.
        assert not any("correlation" in name for name in _order_columns(row))
        assert correlation_for(_handoff(order_id)) not in await _order_row_text(session, order_id)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationEventRecord)
                .where(
                    IntegrationEventRecord.event_type == "resolve.order_linkage.v1",
                    IntegrationEventRecord.outcome == "linked",
                )
            )
            == 1
        )
    # RESOLVE stayed passive: it read existence and line identity, and had no write to make.
    assert store.reference_asks == [CHANNEL_ORDER_GID]
    assert not hasattr(store, "execute_graphql")


async def _order_row_text(session, order_id: str) -> str:
    row = await session.get(ResolveOrderRecord, order_id)
    return json.dumps({column: str(getattr(row, column)) for column in _order_columns(row)})


async def test_a_settled_replay_is_answered_from_storage_and_asks_the_merchant_nothing(
    restart_env, install_mock_discovery, store_factory, channel_config, tmp_path, mock_razorpay
):
    """
    The channel's retry and RESOLVE's replay guard meet, and neither side creates a second anything.

    `event_id` is derived from the pairing, so a resend is byte-identical; RESOLVE answers it from
    the row it already settled, which means Shopify is asked nothing at all.
    """
    store = store_factory(known=(CHANNEL_ORDER_GID,))
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "replay@shop.test")
        merchant = FakeAdminStore()
        channel = _channel(
            channel_config, str(tmp_path / "outbox.sqlite3"), client, merchant
        )

        first = await channel.fulfil(_handoff(order["order_id"]))
        assert first.status == "CONFIRMED_LINKED"
        asks_after_pairing = store.asks
        creates_after_pairing = len(merchant.create_calls)

        second = await channel.fulfil(_handoff(order["order_id"]))

        assert second.status == "CONFIRMED_LINKED"
        assert second.event_id == first.event_id
        assert len(merchant.create_calls) == creates_after_pairing == 1
        assert merchant.read_back_calls == [first.correlation_id]
        # RESOLVE answered from storage: not one more Shopify read than the pairing needed.
        assert store.asks == asks_after_pairing

    async with test_session() as session:
        rows = list((await session.execute(select(IntegrationEventRecord))).scalars().all())
        assert len(rows) == 1
        assert await session.scalar(
            select(func.count())
            .select_from(ResolveOrderRecord)
            .where(ResolveOrderRecord.provider_order_reference.is_not(None))
        ) == 1


# --------------------------------------------------------------------------- #
# RESOLVE's refusals stop the channel, not the other way round
# --------------------------------------------------------------------------- #
async def test_the_channel_claims_nothing_when_shopify_does_not_confirm_the_order(
    restart_env, install_mock_discovery, store_factory, channel_config, tmp_path, mock_razorpay
):
    """
    The channel's word is not evidence. An order RESOLVE cannot read back is not a pairing.

    This is the boundary that makes the merchant side safe to connect at all: a channel that invented
    an order id would produce a refusal, not a linkage. And because RESOLVE's answer here is "I could
    not check" rather than "no", the channel treats it as unresolved, not as a verdict — so the
    settled rejection it eventually gets is a decision, arrived at honestly.
    """
    store = store_factory(known=())  # Shopify answers nothing for that id
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, "unconfirmed@shop.test")
        merchant = FakeAdminStore()
        channel = _channel(channel_config, str(tmp_path / "outbox.sqlite3"), client, merchant)

        unresolved = await channel.fulfil(_handoff(order["order_id"]))

        assert unresolved.status == "DELIVERY_UNKNOWN"
        assert "HTTP 503" in unresolved.reason
        assert len(merchant.create_calls) == 1, "a pairing RESOLVE could not check is not a reason to buy again"
        assert store.reference_asks == [CHANNEL_ORDER_GID]
        after = (await client.get(f"/missions/{mission_id}/order")).json()
        assert after["merchant_order_linked"] is False

        # RESOLVE did settle the attempt as a rejection, so the retry is told so — and still buys
        # nothing. The claim ends refused, not quietly repeated until something sticks.
        settled = await channel.fulfil(_handoff(order["order_id"]))
        assert settled.status == "REFUSED_LINKAGE"
        assert settled.event_id == unresolved.event_id
        assert len(merchant.create_calls) == 1

    async with test_session() as session:
        row = await session.get(ResolveOrderRecord, order["order_id"])
        assert row.provider_order_reference is None
        events = list((await session.execute(select(IntegrationEventRecord))).scalars().all())
        assert [event.outcome for event in events] == ["rejected"]


async def test_two_channels_orders_for_one_merchant_order_leave_exactly_one_pairing(
    restart_env, install_mock_discovery, store_factory, channel_config, tmp_path, mock_razorpay
):
    """
    Same merchant order, second RESOLVE order: RESOLVE's unique index decides, and the channel obeys.

    The channel cannot tell "this order is mine" from "someone already claimed it" — only RESOLVE's
    storage can. That is why the second attempt is recorded as a refusal with the first claim still
    standing, and why nothing here responds by creating another Shopify order.
    """
    store = store_factory(known=(CHANNEL_ORDER_GID,))
    make_client, test_session = restart_env
    async with make_client() as client:
        _first_mission, first = await _purchased(client, install_mock_discovery, "first@shop.test")
        _second_mission, second = await _purchased(client, install_mock_discovery, "second@shop.test")
        shared = str(tmp_path / "outbox.sqlite3")

        winner = await _channel(channel_config, shared, client, FakeAdminStore()).fulfil(
            _handoff(first["order_id"])
        )
        losing_merchant = FakeAdminStore()
        loser = await _channel(channel_config, shared, client, losing_merchant).fulfil(
            _handoff(second["order_id"])
        )

        assert winner.status == "CONFIRMED_LINKED"
        assert loser.status == "REFUSED_LINKAGE"
        assert "HTTP 409" in loser.reason
        assert len(losing_merchant.create_calls) == 1

    async with test_session() as session:
        assert (await session.get(ResolveOrderRecord, first["order_id"])).provider_order_reference == (
            CHANNEL_ORDER_GID
        )
        assert (await session.get(ResolveOrderRecord, second["order_id"])).provider_order_reference is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ResolveOrderRecord)
                .where(ResolveOrderRecord.provider_order_reference.is_not(None))
            )
            == 1
        )
        outbox = Outbox(shared)
        record = outbox.get(correlation_for(_handoff(second["order_id"])))
        assert record.linkage == LINKAGE_REFUSED
        assert record.external_order_id == CHANNEL_ORDER_GID


async def test_a_handoff_in_the_wrong_currency_never_reaches_resolve_at_all(
    restart_env, install_mock_discovery, store_factory, channel_config, tmp_path, mock_razorpay
):
    """
    Option C, seen from the merchant side: a USD purchase is refused before anything is claimed.

    The connected store publishes USD (measured live), so this is the exact shape the real
    deployment is in — and the refusal costs RESOLVE nothing, because a pairing that cannot be
    truthful never starts.
    """
    store = store_factory(known=(CHANNEL_ORDER_GID,))
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id, order = await _purchased(client, install_mock_discovery, "usd@shop.test")
        merchant = FakeAdminStore()

        outcome = await _channel(
            channel_config, str(tmp_path / "outbox.sqlite3"), client, merchant
        ).fulfil(_handoff(order["order_id"], currency="USD"))

        assert outcome.status == "REFUSED_HANDOFF"
        assert "does not convert" in outcome.reason
        assert merchant.create_calls == []
        assert merchant.read_back_calls == []
        assert store.asks == 0
        after = (await client.get(f"/missions/{mission_id}/order")).json()
        assert after["merchant_order_linkage"] == "awaiting_linkage"

    async with test_session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(IntegrationEventRecord))
        ) == 0


# --------------------------------------------------------------------------- #
# nothing secret, nothing personal
# --------------------------------------------------------------------------- #
async def test_neither_credential_nor_a_signature_survives_in_what_either_side_stored(
    restart_env, install_mock_discovery, store_factory, channel_config, tmp_path, mock_razorpay
):
    store = store_factory(known=(CHANNEL_ORDER_GID,))
    make_client, test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "leak@shop.test")
        outcome = await _channel(
            channel_config, str(tmp_path / "outbox.sqlite3"), client, FakeAdminStore()
        ).fulfil(_handoff(order["order_id"]))
        assert outcome.status == "CONFIRMED_LINKED"

    stored = await _stored_text(test_session)
    assert SECRET not in stored
    assert TOKEN not in stored
    assert "x-resolve-signature" not in stored
    # RESOLVE keeps a digest of the body, never the body: the signed bytes are not sitting in a
    # table waiting to be replayed by whoever reads it.
    assert re.search(r"[0-9a-f]{64}", stored)
    assert CHANNEL_ORDER_GID in stored


async def test_the_channels_outbox_holds_the_pairing_but_never_a_credential(
    restart_env, install_mock_discovery, store_factory, channel_config, tmp_path, mock_razorpay
):
    store = store_factory(known=(CHANNEL_ORDER_GID,))
    make_client, _test_session = restart_env
    async with make_client() as client:
        _, order = await _purchased(client, install_mock_discovery, "outbox@shop.test")
        path = str(tmp_path / "outbox.sqlite3")
        outcome = await _channel(channel_config, path, client, FakeAdminStore()).fulfil(
            _handoff(order["order_id"])
        )
        assert outcome.status == "CONFIRMED_LINKED"

        record = Outbox(path).get(outcome.correlation_id)
        assert record.linkage == LINKAGE_SENT
        raw = json.dumps(
            [
                [str(cell) for cell in row]
                for row in sqlite3.connect(path).execute("SELECT * FROM purchase_outbox").fetchall()
            ]
        )
    assert SECRET not in raw
    assert TOKEN not in raw
    assert "x-resolve-signature" not in raw


def test_the_channel_is_a_separate_package_that_imports_nothing_from_resolve():
    """
    The boundary 14C.7I exists to keep, checked from the RESOLVE side.

    If a `from app...` ever appears in the channel, the merchant module has acquired RESOLVE's
    Shopify identity — a different app with different scopes — and every claim about the write
    boundary becomes fiction.
    """
    offenders = []
    for file in sorted((_CHANNEL_ROOT / "merchant_channel").rglob("*.py")):
        text = file.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import app", "from app", "from app.", "import app.")):
                offenders.append(f"{file.name}: {stripped}")
    assert offenders == []


# --------------------------------------------------------------------------- #
# the m16 defect, pinned against the real receiver
# --------------------------------------------------------------------------- #
async def test_a_redelivery_resends_the_exact_persisted_bytes_the_receiver_recorded(
    restart_env, install_mock_discovery, store_factory, channel_config, tmp_path, mock_razorpay
):
    """
    The m16 defect, end to end over the real route: the event pair a delivery persisted must
    survive re-verification, and the receiver must have verified exactly those bytes.

    The first attempt cannot be confirmed by the merchant store (HTTP 503), so the real receiver
    records the event as rejected and the channel is left unresolved. The retry runs the m16 path —
    read back, re-verify, mark confirmed, deliver — and must arrive as the same bytes under the
    same event_id: the receiver answers from storage, and the digest it recorded for the first
    delivery still matches the bytes the channel holds afterwards. Under the defect, re-verification
    severed the stored event_id from its body, a fresh body was minted under the same id, and the
    receiver answered 409 — same channel status, different bytes.
    """
    store = store_factory(known=())  # the route cannot confirm that order yet: a 503 outage shape
    make_client, test_session = restart_env
    outbox_path = tmp_path / "outbox.sqlite3"
    async with make_client() as client:
        _mission, order = await _purchased(client, install_mock_discovery, "persist@shop.test")
        order_id = order["order_id"]
        pairing = _handoff(order_id)
        merchant = FakeAdminStore()
        channel = _channel(channel_config, str(outbox_path), client, merchant)

        first = await channel.fulfil(pairing)
        assert first.status == "DELIVERY_UNKNOWN"
        record = Outbox(str(outbox_path)).get(correlation_for(pairing))
        assert record.event_id == first.event_id
        assert record.event_body is not None

        second = await channel.fulfil(pairing)
        assert second.status == "REFUSED_LINKAGE"
        assert second.event_id == first.event_id

        after = Outbox(str(outbox_path)).get(correlation_for(pairing))
        assert (after.event_id, after.event_body) == (
            record.event_id,
            record.event_body,
        ), "re-verification must leave the stored event pair exactly as it was"
        assert len(merchant.create_calls) == 1

    async with test_session() as session:
        events = list((await session.execute(select(IntegrationEventRecord))).scalars().all())
        assert [event.event_id for event in events] == [first.event_id]
        assert [event.outcome for event in events] == ["rejected"]
        # The bytes the receiver verified are, digest for digest, the bytes the channel still holds.
        assert events[0].body_sha256 == hashlib.sha256(after.event_body.encode("utf-8")).hexdigest()
        row = await session.get(ResolveOrderRecord, order_id)
        assert row.provider_order_reference is None
