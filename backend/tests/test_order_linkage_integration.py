"""
Tests for Task 14C.7B — the authenticated order-linkage integration contract.

One question runs through this whole file: can a signed event ever become an authority it should
not have? The answers are structural, and each one is measured rather than asserted in prose:

  * nothing is read, stored, or acted on before the HMAC over the raw bytes passes;
  * the payload may name two references and nothing else — an event carrying an amount, a
    product title, a fulfillment line, a return reason, a payment id, or a `user_id` to override
    ownership is refused outright, and refused BEFORE it is recorded as having arrived;
  * replay is harmless because `event_id` is unique in storage, not because the handler hopes to
    notice;
  * a valid signature buys no linkage on its own: the RESOLVE order must exist, and Shopify must
    confirm that exact order, before the single writer stores anything;
  * the handler never touches the linkage column itself — `link_order_to_shopify_order` does, and
    the owner it links for is read off the persisted order, never supplied by the event;
  * a browser session, the frontend, and the reasoning model have no route to it at all.

Hermetic: an in-memory database and an instrumented stand-in for the Shopify client. Every
document the provider could send is recorded, and a mutation is an assertion failure.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import logging
import os
import pathlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.integrations import order_linkage
from app.integrations.order_linkage import (
    RECEIVED,
    SIGNATURE_HEADER,
    LinkageIntegrationError,
    OrderLinkageEvent,
    handle_linkage_event,
    parse_event,
    sign_body,
    verify_signature,
)
from app.main import app
from app.models.db_models import (
    AuditLogRecord,
    IntegrationEventRecord,
    ResolveOrderRecord,
)
from app.tools.shopify.errors import ShopifyMalformedResponseError
from app.tools.shopify.models import ShopifyOrderReference
from app.tools.shopify.sanitizer import reset_registered_secrets

from tests.test_refund_execution import restart_env  # noqa: F401

SECRET = "test-integration-signing-secret"
OWNER = "user_linkage_owner"
EVENT_ID = "evt-linkage-0001"
ORDER_GID = "gid://shopify/Order/7990366601503"
OTHER_GID = "gid://shopify/Order/7990366601599"
UNKNOWN_GID = "gid://shopify/Order/99999999999999"
ROUTE = "/integrations/shopify/order-linkage"

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


@pytest.fixture
def configured(monkeypatch):
    """Enable the integration the way a deployment would: one environment value."""
    monkeypatch.setattr(get_settings(), "linkage_integration_secret", SECRET)
    return SECRET


class StubShopifyReader:
    """Stands where the real read-only client stands, and records every question asked of it."""

    def __init__(self, *, known=(ORDER_GID, OTHER_GID)):
        self.known = set(known)
        self.asks: list[str] = []

    async def read_order_reference(self, order_id: str) -> ShopifyOrderReference:
        self.asks.append(order_id)
        if order_id not in self.known:
            raise ShopifyMalformedResponseError(
                "Shopify knows nothing about that order id, so it cannot be used as a canonical "
                "reference."
            )
        return ShopifyOrderReference(order_id=order_id, name="#1001")


@pytest.fixture
def stub_reader(monkeypatch):
    """
    The client the route would build, replaced by a recorder.

    Patching `order_linkage._client` also proves the route has no second credential path: every
    Shopify read this handler makes goes through the one factory the rest of the app uses.
    """
    reader = StubShopifyReader()
    monkeypatch.setattr(order_linkage, "_client", lambda: reader)
    return reader


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #
async def _order(test_session, *, mission_id, order_id=None, user_id=OWNER, reference=None) -> str:
    async with test_session() as session:
        row = ResolveOrderRecord(
            mission_id=mission_id,
            user_id=user_id,
            product_id="item_dove",
            title="Dove Hair Fall Rescue Shampoo",
            merchant="Nykaa",
            product={"product_id": "item_dove", "source": "google_shopping"},
            amount=Decimal("399.00"),
            currency="INR",
            razorpay_order_id=f"order_{mission_id}",
            razorpay_payment_id=f"pay_{mission_id}",
            provider_order_reference=reference,
            status="order_confirmed",
        )
        if order_id:
            row.order_id = order_id
        session.add(row)
        await session.commit()
        return row.order_id


async def _reference(test_session, mission_id) -> str | None:
    async with test_session() as session:
        row = await session.scalar(
            select(ResolveOrderRecord).where(ResolveOrderRecord.mission_id == mission_id)
        )
        return row.provider_order_reference if row else None


def _payload(**overrides) -> dict:
    base = {
        "event_id": EVENT_ID,
        "event_type": "resolve.order_linkage.v1",
        "mission_id": "mission_lk_1",
        "shopify_order_id": ORDER_GID,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "source": "merchant-channel",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _signed(payload: dict, secret: str = SECRET) -> tuple[bytes, str]:
    body = json.dumps(payload).encode("utf-8")
    return body, sign_body(secret, body)


async def _post(client: AsyncClient, body: bytes, signature: str | None = None, **headers):
    hdrs = dict(headers)
    if signature is not None:
        hdrs[SIGNATURE_HEADER] = signature
    hdrs.setdefault("content-type", "application/json")
    return await client.post(ROUTE, content=body, headers=hdrs)


async def _events(test_session) -> list[IntegrationEventRecord]:
    async with test_session() as session:
        return list(
            (
                await session.scalars(
                    select(IntegrationEventRecord).order_by(IntegrationEventRecord.event_id)
                )
            ).all()
        )


async def _stored(test_session) -> list[dict]:
    """Every column of every inbox row, as plain values, for leak checks."""
    return [
        {
            column.name: getattr(row, column.name)
            for column in IntegrationEventRecord.__table__.columns
        }
        for row in await _events(test_session)
    ]


async def _audit_rows(test_session, mission_id) -> list[AuditLogRecord]:
    async with test_session() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
                )
            ).all()
        )


async def _audit_notes(test_session, mission_id) -> list[str]:
    return [
        f"{r.actor}|{r.action}|{r.decision}|{r.reason}" for r in await _audit_rows(test_session, mission_id)
    ]


@pytest.fixture
async def env(restart_env, configured, stub_reader):  # noqa: ARG001
    """A configured integration over a fresh in-memory DB with two unlinked RESOLVE orders."""
    make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    await _order(test_session, mission_id="mission_lk_2")
    return make_client, test_session, stub_reader


async def _handle(test_session, *, body, signature, client):
    async with test_session() as session:
        return await handle_linkage_event(session, body=body, signature=signature, client=client)


def _production_sources(prefixes=None):
    """(relative path, source) for every production module under app/."""
    root = BACKEND_ROOT / "app"
    for file in sorted(root.rglob("*.py")):
        rel = file.relative_to(BACKEND_ROOT).as_posix()
        if "__pycache__" in file.parts:
            continue
        if prefixes and not rel.startswith(tuple(prefixes)):
            continue
        yield rel, file.read_text(encoding="utf-8")


def _production_callers(name: str, *, defined_in: str) -> list[str]:
    """Production files that CALL `name`, excluding the module that defines it."""
    callers = []
    for rel, source in _production_sources():
        if rel == defined_in:
            continue
        for node in ast.walk(ast.parse(source)):
            func = getattr(node, "func", None)
            if getattr(func, "id", None) == name or getattr(func, "attr", None) == name:
                callers.append(rel)
                break
    return callers


# --------------------------------------------------------------------------- #
# A: a valid signed event establishes the linkage — and only because Shopify confirmed the order
# --------------------------------------------------------------------------- #
async def test_valid_signed_event_establishes_linkage_over_http(env):
    make_client, test_session, reader = env
    body, signature = _signed(_payload())
    async with make_client() as client:
        resp = await _post(client, body, signature)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["outcome"] == "linked"
        assert data["provider_order_reference"] == ORDER_GID
        assert data["mission_id"] == "mission_lk_1"
        assert data["already_processed"] is False
    assert await _reference(test_session, "mission_lk_1") == ORDER_GID
    # Exactly one question to Shopify: "is this order real?" — nothing else is ever read.
    assert reader.asks == [ORDER_GID]
    rows = await _events(test_session)
    assert [(r.event_id, r.outcome, r.provider_order_reference) for r in rows] == [
        (EVENT_ID, "linked", ORDER_GID)
    ]
    assert rows[0].processed_at is not None
    assert rows[0].detail is None


async def test_service_level_linkage_verifies_with_shopify_before_writing(restart_env, configured):
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    reader = StubShopifyReader()
    body, signature = _signed(_payload())
    status, payload = await _handle(test_session, body=body, signature=signature, client=reader)
    assert (status, payload["outcome"]) == (200, "linked")
    assert reader.asks == [ORDER_GID]
    assert await _reference(test_session, "mission_lk_1") == ORDER_GID


async def test_an_event_naming_the_resolve_order_id_instead_of_the_mission_also_works(
    restart_env, configured
):
    """Either RESOLVE identifier names the target; the second is never a second authority."""
    _make, test_session = restart_env
    order_id = await _order(test_session, mission_id="mission_lk_1")
    reader = StubShopifyReader()
    body, signature = _signed(_payload(mission_id=None, resolve_order_id=order_id))
    status, payload = await _handle(test_session, body=body, signature=signature, client=reader)
    assert status == 200
    assert payload["resolve_order_id"] == order_id
    assert payload["mission_id"] == "mission_lk_1"
    assert await _reference(test_session, "mission_lk_1") == ORDER_GID
    # The inbox row learns the canonical mission even when the event named only the order.
    rows = await _events(test_session)
    assert rows[0].mission_id == "mission_lk_1"


async def test_success_is_audited_through_the_existing_insert_only_log(restart_env, configured):
    _make, test_session = restart_env
    order_id = await _order(test_session, mission_id="mission_lk_1")
    body, signature = _signed(_payload())
    await _handle(test_session, body=body, signature=signature, client=StubShopifyReader())
    notes = await _audit_notes(test_session, "mission_lk_1")
    assert len(notes) == 1
    actor, action, decision, reason = notes[0].split("|")
    assert actor == "integration" and action == "shopify_order_linkage"
    assert decision == "observed"  # not 'approved': no Authority Engine was consulted here
    assert ORDER_GID in reason and EVENT_ID in reason and order_id in reason
    assert SECRET not in reason and "signature" not in reason.lower()


async def test_a_refused_linkage_is_audited_as_a_refusal(restart_env, configured):
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    body, signature = _signed(_payload(shopify_order_id=UNKNOWN_GID))
    await _handle(
        test_session,
        body=body,
        signature=signature,
        client=StubShopifyReader(known=()),
    )
    actor, action, decision, reason = (await _audit_notes(test_session, "mission_lk_1"))[0].split("|")
    assert (actor, action, decision) == ("integration", "shopify_order_linkage", "refused")
    assert EVENT_ID in reason and "merchant-channel" in reason
    assert SECRET not in reason


# --------------------------------------------------------------------------- #
# B, C, D: authentication failures — one answer, no detail, nothing recorded
# --------------------------------------------------------------------------- #
async def test_missing_signature_is_refused_and_nothing_is_stored(env):
    make_client, test_session, reader = env
    body, _ = _signed(_payload())
    async with make_client() as client:
        resp = await _post(client, body, None)
        assert resp.status_code == 401
    assert resp.json()["detail"] == "The event signature is not valid."
    assert await _reference(test_session, "mission_lk_1") is None
    assert await _events(test_session) == []
    assert reader.asks == []


async def test_wrong_or_malformed_signatures_are_refused_with_the_same_answer(env):
    make_client, test_session, reader = env
    body, _ = _signed(_payload())
    async with make_client() as client:
        empty = await _post(client, body, "")
        wrong = await _post(client, body, "0" * 64)
        truncated = await _post(client, body, sign_body(SECRET, body)[:32])
        oversized = await _post(client, body, "ff" * 5000)
        non_hex = await _post(client, body, "z" * 64)
        signed_by_other = await _post(client, body, _signed(_payload(), "other-secret")[1])
    responses = (empty, wrong, truncated, oversized, non_hex, signed_by_other)
    assert [r.status_code for r in responses] == [401] * len(responses)
    for response in responses:
        # Identical text for missing, malformed and wrong: nothing here says which step failed.
        assert response.json()["detail"] == "The event signature is not valid."
    assert await _events(test_session) == []
    assert reader.asks == []
    assert await _reference(test_session, "mission_lk_1") is None


async def test_body_modified_after_signing_is_refused(env):
    """The signature covers the bytes, not an idea of them: one changed field voids it."""
    make_client, test_session, reader = env
    payload = _payload()
    body, signature = _signed(payload)
    tampered = json.dumps({**payload, "shopify_order_id": OTHER_GID}).encode("utf-8")
    async with make_client() as client:
        resp = await _post(client, tampered, signature)
        assert resp.status_code == 401
    assert await _reference(test_session, "mission_lk_1") is None
    assert await _reference(test_session, "mission_lk_2") is None
    assert reader.asks == []
    assert await _events(test_session) == []


async def test_a_valid_signature_for_another_secret_shape_is_not_forged_by_casing(env):
    """Hex casing is accepted; nothing else about the signature is negotiable."""
    make_client, test_session = env[0], env[1]
    body, signature = _signed(_payload())
    async with make_client() as client:
        resp = await _post(client, body, signature.upper())
    assert resp.status_code == 200
    assert await _reference(test_session, "mission_lk_1") == ORDER_GID


async def test_an_unconfigured_deployment_refuses_rather_than_trusting(env, monkeypatch):
    """No signing secret means the endpoint is off — stated as 503, never as an open door."""
    make_client, test_session, reader = env
    monkeypatch.setattr(get_settings(), "linkage_integration_secret", "")
    body, signature = _signed(_payload())
    async with make_client() as client:
        signed = await _post(client, body, signature)
        assert signed.status_code == 503
        unsigned = await _post(client, body, None)
    assert "not configured" in signed.json()["detail"]
    # Disabled is the whole answer, and it is the same answer with or without a signature.
    assert unsigned.status_code == 503
    assert await _events(test_session) == []
    assert reader.asks == []
    assert await _reference(test_session, "mission_lk_1") is None


async def test_nothing_is_read_from_the_payload_before_the_signature_passes(env):
    """
    An unauthenticated caller cannot even probe the schema: the body that would fail validation and
    the body that would pass it produce the identical 401, and neither is parsed or stored.
    """
    make_client, test_session, reader = env
    garbage, _ = _signed({"nonsense": True})
    valid, _ = _signed(_payload())
    async with make_client() as client:
        first = await _post(client, garbage, "a" * 64)
        second = await _post(client, valid, "a" * 64)
    assert first.status_code == second.status_code == 401
    assert first.json() == second.json()
    assert await _events(test_session) == []
    assert reader.asks == []


# --------------------------------------------------------------------------- #
# E, F: validation after authentication — and never before the event is recorded
# --------------------------------------------------------------------------- #
async def test_stale_event_is_refused_and_nothing_is_linked(env):
    make_client, test_session, reader = env
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    body, signature = _signed(_payload(issued_at=old.isoformat()))
    async with make_client() as client:
        resp = await _post(client, body, signature)
        assert resp.status_code == 400
    assert "outside the accepted window" in resp.json()["detail"]
    assert await _reference(test_session, "mission_lk_1") is None
    assert await _events(test_session) == []  # never even recorded as having arrived
    assert reader.asks == []


async def test_event_from_the_future_is_refused(env):
    make_client, test_session = env[0], env[1]
    ahead = datetime.now(timezone.utc) + timedelta(hours=1)
    body, signature = _signed(_payload(issued_at=ahead.isoformat()))
    async with make_client() as client:
        resp = await _post(client, body, signature)
    assert resp.status_code == 400
    assert await _events(test_session) == []


async def test_naive_timestamp_is_refused(env):
    make_client, test_session = env[0], env[1]
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    body, signature = _signed(_payload(issued_at=naive))
    async with make_client() as client:
        resp = await _post(client, body, signature)
    assert resp.status_code == 400
    assert "timezone-aware" in resp.json()["detail"]
    assert await _events(test_session) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", "399.00"),
        ("currency", "INR"),
        ("product_title", "Dove Hair Fall Rescue Shampoo"),
        ("product_price", "410.00"),
        ("fulfillment_line_item_id", "gid://shopify/FulfillmentLineItem/1"),
        ("return_reason", "DEFECTIVE"),
        ("razorpay_payment_id", "pay_TEST1"),
        ("user_id", "someone_else"),
        ("provider_order_reference", ORDER_GID),
        ("shopify_order_ids", [ORDER_GID]),
        ("status", "linked"),
    ],
    ids=[
        "amount", "currency", "product-title", "product-price", "fulfillment-line",
        "return-reason", "payment-id", "user-id", "pre-written-linkage", "multiple-gids",
        "outcome-override",
    ],
)
async def test_authority_shaped_fields_are_refused_outright(env, field, value):
    """
    The schema is closed, so an extra field is not 'ignored' — it is invalid. This is the one
    property that makes the payload incapable of carrying authority: money, product, reason,
    fulfillment target, payment id and identity all fail here.
    """
    make_client, test_session, reader = env
    body, signature = _signed(_payload(**{field: value}))
    async with make_client() as client:
        resp = await _post(client, body, signature)
        assert resp.status_code == 400, field
    assert resp.json()["detail"].startswith("The event does not match")
    assert await _reference(test_session, "mission_lk_1") is None
    assert await _events(test_session) == []  # refused before it was ever recorded
    assert reader.asks == []


@pytest.mark.parametrize(
    "payload",
    [
        None,  # not JSON at all
        b"",  # empty body
        b"[1,2,3]",  # JSON, but not an object
        {},  # no fields
        _payload(event_type="resolve.order_linkage.v2"),  # unknown type
        _payload(mission_id=None),  # no RESOLVE order named
        _payload(mission_id="mission_lk_1", resolve_order_id="order_lk_1"),  # two named
        _payload(event_id="x"),  # too short to be a real id
        _payload(event_id="evt with spaces"),  # not an id-shaped string
        _payload(shopify_order_id="   "),  # blank reference
        _payload(issued_at="yesterday"),  # unusable timestamp
        _payload(source="x" * 200),  # oversized free text
    ],
    ids=[
        "not-json", "empty", "json-array", "no-fields", "wrong-type", "no-target",
        "two-targets", "short-id", "spaced-id", "blank-reference", "bad-timestamp",
        "oversized-source",
    ],
)
async def test_malformed_events_are_refused(env, payload):
    make_client, test_session, reader = env
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    async with make_client() as client:
        resp = await _post(client, body, sign_body(SECRET, body))
        assert resp.status_code == 400, body[:60]
    assert await _reference(test_session, "mission_lk_1") is None
    assert await _events(test_session) == []
    assert reader.asks == []


async def test_an_oversized_body_is_refused_without_being_parsed(env):
    make_client, test_session = env[0], env[1]
    body = b'{"event_id": "' + b"x" * 20_000 + b'"}'
    async with make_client() as client:
        resp = await _post(client, body, sign_body(SECRET, body))
    assert resp.status_code == 400
    assert "small JSON object" in resp.json()["detail"]


async def test_a_refusal_never_echoes_the_payload(restart_env, configured):
    """The 400 text is a fixed sentence: a caller learns the contract, not their own data back."""
    _make, test_session = restart_env
    marker = "customer-9f3a-not-a-real-secret"
    body, signature = _signed(_payload(source=marker, amount=12))
    with pytest.raises(LinkageIntegrationError) as direct:
        parse_event(body)
    assert direct.value.status_code == 400
    assert marker not in direct.value.message
    with pytest.raises(LinkageIntegrationError) as through_handler:
        await _handle(test_session, body=body, signature=signature, client=StubShopifyReader())
    assert through_handler.value.status_code == 400
    assert through_handler.value.message == direct.value.message
    assert await _events(test_session) == []


# --------------------------------------------------------------------------- #
# G, H, I: the linkage refusals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "reference",
    ["ord_canonical_1", "gid://shopify/Order/", "gid://shopify/Order/abc", ORDER_GID + "/x",
     "gid://shopify/Refund/7990366601503", ORDER_GID.replace("gid://", "GID://")],
    ids=["opaque-id", "empty-tail", "non-numeric-tail", "extra-path", "wrong-gid-type",
         "wrong-scheme-case"],
)
async def test_a_malformed_shopify_reference_is_refused_before_any_network_call(
    restart_env, configured, reference
):
    """Shape first, network second: a malformed id never costs a Shopify read or a write."""
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    reader = StubShopifyReader()
    body, signature = _signed(_payload(shopify_order_id=reference))
    status, payload = await _handle(test_session, body=body, signature=signature, client=reader)
    assert status == 400
    assert "linkage was made" in payload["detail"].lower()
    assert reader.asks == []
    assert await _reference(test_session, "mission_lk_1") is None
    assert [(r.event_id, r.outcome) for r in await _events(test_session)] == [
        (EVENT_ID, "rejected")
    ]


async def test_an_order_shopify_does_not_confirm_is_never_stored(restart_env, configured):
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    reader = StubShopifyReader(known=(OTHER_GID,))
    body, signature = _signed(_payload(shopify_order_id=UNKNOWN_GID))
    status, payload = await _handle(test_session, body=body, signature=signature, client=reader)
    assert status == 503
    assert reader.asks == [UNKNOWN_GID]  # it asked; Shopify said no
    assert payload["detail"] == (
        "The merchant store could not confirm that order, so no linkage was stored. This "
        "event id is now spent; a further attempt must arrive as a new event."
    )
    assert await _reference(test_session, "mission_lk_1") is None
    rows = await _events(test_session)
    assert [(r.event_id, r.outcome) for r in rows] == [(EVENT_ID, "rejected")]
    # The auditor keeps the reason even though the caller gets the fixed sentence.
    assert "could not confirm" in rows[0].detail
    assert rows[0].provider_order_reference is None
    assert rows[0].processed_at is not None


async def test_an_unreachable_shopify_is_a_refusal_not_a_licence(restart_env, configured):
    """A store that cannot answer must not leave a pointer behind."""
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")

    class Broken(StubShopifyReader):
        async def read_order_reference(self, order_id):
            self.asks.append(order_id)
            raise RuntimeError("connection reset to resolve-commerce-lab.myshopify.com")

    reader = Broken()
    body, signature = _signed(_payload())
    status, payload = await _handle(test_session, body=body, signature=signature, client=reader)
    assert status == 503
    assert reader.asks == [ORDER_GID]  # it tried, and the try is the whole story
    assert "no linkage was stored" in payload["detail"].lower()
    assert await _reference(test_session, "mission_lk_1") is None


async def test_an_unknown_resolve_order_is_refused_without_revealing_anything(restart_env, configured):
    _make, test_session = restart_env
    reader = StubShopifyReader()
    body, signature = _signed(_payload(mission_id="mission_does_not_exist"))
    status, payload = await _handle(test_session, body=body, signature=signature, client=reader)
    assert status == 404
    assert payload == {
        "event_id": EVENT_ID,
        "outcome": "rejected",
        "detail": "No RESOLVE order matches this event, so nothing was linked.",
    }
    # Nothing worth verifying was found, so Shopify was never consulted and nothing was leaked.
    assert reader.asks == []
    assert "does_not_exist" not in json.dumps(payload)
    assert [(r.outcome, r.detail) for r in await _events(test_session)] == [
        ("rejected", "No RESOLVE order matches this event, so nothing was linked.")
    ]


async def test_a_mission_owned_by_someone_else_still_links_for_its_real_owner(
    restart_env, configured
):
    """
    The event cannot name an actor, so there is nothing for it to spoof: the handler reads the
    persisted order's own user_id and passes THAT to the writer.
    """
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1", user_id="user_real_owner")
    seen: dict = {}
    real_link = order_linkage.link_order_to_shopify_order

    async def spy(db, **kwargs):
        seen.update(kwargs)
        return await real_link(db, **kwargs)

    order_linkage.link_order_to_shopify_order = spy
    try:
        body, signature = _signed(_payload())
        status, _ = await _handle(
            test_session, body=body, signature=signature, client=StubShopifyReader()
        )
    finally:
        order_linkage.link_order_to_shopify_order = real_link
    assert status == 200
    assert seen["actor_user_id"] == "user_real_owner"  # never from the event
    assert seen["mission_id"] == "mission_lk_1"
    assert seen["shopify_order_id"] == ORDER_GID
    assert "user_id" not in _payload()


# --------------------------------------------------------------------------- #
# the single-writer boundary
# --------------------------------------------------------------------------- #
async def test_the_handler_delegates_and_never_writes_the_linkage_column_itself(restart_env, configured, monkeypatch):
    """
    The integration may only ASK the canonical writer to do the work. Proved behaviourally: with
    `link_order_to_shopify_order` replaced by a recorder, a valid signed event stores nothing.
    """
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    calls: list[dict] = []

    async def recorder(db, **kwargs):
        calls.append(kwargs)
        raise AssertionError("stop here: the writer is the only thing that may persist")

    monkeypatch.setattr(order_linkage, "link_order_to_shopify_order", recorder)
    body, signature = _signed(_payload())
    with pytest.raises(AssertionError):
        await _handle(test_session, body=body, signature=signature, client=StubShopifyReader())
    assert [c["shopify_order_id"] for c in calls] == [ORDER_GID]
    assert await _reference(test_session, "mission_lk_1") is None


def test_provider_order_reference_has_exactly_one_production_writer():
    """
    Every production assignment to `provider_order_reference` is listed here as a snapshot, and
    only ONE of them can reach the RESOLVE order table:

      * `order.provider_order_reference`  — app/tools/shopify/linkage.py, the canonical writer;
      * `return_record.provider_order_reference` — the ReturnRecord copy of an already-persisted
        linkage (Task 14C.4), a different table that only ever reads;
      * `row.provider_order_reference` — the integration inbox row's own bookkeeping column.

    A new site appears as a diff in this list, which is the point.
    """
    sites = []
    for rel, source in _production_sources():
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if ".provider_order_reference =" in stripped.replace("==", "="):
                if "==" in stripped:
                    continue
                sites.append((rel, stripped))
    assert sites == [
        ("app/integrations/order_linkage.py",
         "row.provider_order_reference = linked.provider_order_reference"),
        ("app/routers/missions.py",
         "return_record.provider_order_reference = target.provider_order_reference"),
        ("app/tools/shopify/linkage.py", "order.provider_order_reference = reference"),
    ]
    resolve_order_writers = [
        rel for rel, line in sites if line.startswith("order.provider_order_reference")
    ]
    assert resolve_order_writers == ["app/tools/shopify/linkage.py"]
    # And in that file the assigned object is provably the RESOLVE order row.
    linkage_source = (BACKEND_ROOT / "app/tools/shopify/linkage.py").read_text(encoding="utf-8")
    assert "order = await db.scalar(select(ResolveOrderRecord)" in linkage_source


def test_no_production_module_constructs_a_resolve_order_with_a_reference():
    """The column is not writable at INSERT time either: no row is born linked."""
    for rel, source in _production_sources():
        if rel.endswith("models/db_models.py"):
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "ResolveOrderRecord":
                continue
            keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
            assert "provider_order_reference" not in keywords, rel
    assert "provider_order_reference=reference" not in (
        BACKEND_ROOT / "app/routers/missions.py"
    ).read_text(encoding="utf-8")


def test_the_linkage_writer_has_exactly_one_production_caller():
    """
    `link_order_to_shopify_order` may be invoked by the authenticated integration and by nothing
    else in production code. Tests and the definition itself are excluded on purpose: what is being
    held shut is the set of entry points, not the existence of assertions.
    """
    callers = _production_callers(
        "link_order_to_shopify_order", defined_in="app/tools/shopify/linkage.py"
    )
    assert callers == ["app/integrations/order_linkage.py"], callers


# --------------------------------------------------------------------------- #
# J, K, L, M, U, V: idempotency, replay, conflicts, restart, concurrency
# --------------------------------------------------------------------------- #
async def test_the_same_event_twice_links_once(restart_env, configured):
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    reader = StubShopifyReader()
    body, signature = _signed(_payload())
    first = await _handle(test_session, body=body, signature=signature, client=reader)
    second = await _handle(test_session, body=body, signature=signature, client=reader)
    assert first[0] == 200 and first[1]["outcome"] == "linked"
    assert second[0] == 200
    assert second[1]["already_processed"] is True
    assert second[1]["outcome"] == "linked"
    assert second[1]["provider_order_reference"] == ORDER_GID
    assert reader.asks == [ORDER_GID]  # verified once, not twice
    assert len(await _events(test_session)) == 1


async def test_a_replayed_event_answers_from_the_record_even_over_http(env):
    make_client, test_session = env[0], env[1]
    body, signature = _signed(_payload())
    async with make_client() as client:
        assert (await _post(client, body, signature)).status_code == 200
        replay = await _post(client, body, signature)
        assert replay.status_code == 200
        assert replay.json()["already_processed"] is True
    assert len(await _events(test_session)) == 1


async def test_a_rejected_event_replayed_identically_stays_rejected(restart_env, configured):
    """Replaying a failure must not turn it into a success, and must not retry Shopify."""
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    first_reader = StubShopifyReader(known=())
    body, signature = _signed(_payload(shopify_order_id=UNKNOWN_GID))
    first = await _handle(test_session, body=body, signature=signature, client=first_reader)
    second_reader = StubShopifyReader()  # Shopify would now 'know' it — irrelevant
    second = await _handle(test_session, body=body, signature=signature, client=second_reader)
    assert first[0] == 503
    assert second[0] == 200
    assert second[1]["outcome"] == "rejected"
    assert second[1]["already_processed"] is True
    assert second_reader.asks == []
    assert await _reference(test_session, "mission_lk_1") is None


async def test_the_same_event_id_with_a_different_payload_is_refused(restart_env, configured):
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    reader = StubShopifyReader()
    body, signature = _signed(_payload())
    assert (await _handle(test_session, body=body, signature=signature, client=reader))[0] == 200
    impostor, impostor_sig = _signed(_payload(shopify_order_id=OTHER_GID))
    with pytest.raises(LinkageIntegrationError) as excinfo:
        await _handle(test_session, body=impostor, signature=impostor_sig, client=reader)
    assert excinfo.value.status_code == 409
    assert "different payload" in excinfo.value.message
    assert await _reference(test_session, "mission_lk_1") == ORDER_GID
    assert reader.asks == [ORDER_GID]  # the impostor never reached Shopify
    assert len(await _events(test_session)) == 1


async def test_a_second_event_naming_a_different_shopify_order_is_refused(restart_env, configured):
    """Same RESOLVE order, second Shopify GID: an established linkage is not moved."""
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    reader = StubShopifyReader()
    body, signature = _signed(_payload())
    await _handle(test_session, body=body, signature=signature, client=reader)
    body2, signature2 = _signed(_payload(event_id="evt-linkage-0002", shopify_order_id=OTHER_GID))
    status, payload = await _handle(test_session, body=body2, signature=signature2, client=reader)
    assert status == 409
    assert "already linked" in payload["detail"]
    assert await _reference(test_session, "mission_lk_1") == ORDER_GID
    rows = await _events(test_session)
    assert sorted(r.outcome for r in rows) == ["linked", "rejected"]


async def test_an_event_naming_the_same_order_and_gid_again_is_a_no_op_success(
    restart_env, configured
):
    """Re-establishing the identical pairing is not a conflict — an integration may retry freely."""
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1", reference=ORDER_GID)
    reader = StubShopifyReader()
    body, signature = _signed(_payload(event_id="evt-linkage-0009"))
    status, payload = await _handle(test_session, body=body, signature=signature, client=reader)
    assert status == 200
    assert payload["outcome"] == "already_linked"
    assert await _reference(test_session, "mission_lk_1") == ORDER_GID
    # An already-stored linkage is not re-verified: it was confirmed against Shopify when it was
    # written, and the writer never re-opens one.
    assert reader.asks == []


async def test_one_shopify_order_cannot_back_two_resolve_orders(restart_env, configured):
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    await _order(test_session, mission_id="mission_lk_2")
    reader = StubShopifyReader()
    first = _signed(_payload())
    await _handle(test_session, body=first[0], signature=first[1], client=reader)
    second = _signed(_payload(event_id="evt-linkage-0003", mission_id="mission_lk_2"))
    status, _ = await _handle(test_session, body=second[0], signature=second[1], client=reader)
    assert status == 409
    assert await _reference(test_session, "mission_lk_1") == ORDER_GID
    assert await _reference(test_session, "mission_lk_2") is None


async def test_idempotency_survives_a_restart(restart_env, configured):
    """
    The replay guard is storage, not memory: a fresh process (new client, new session) over the
    same database cannot be talked into a second linkage.
    """
    make_client, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    body, signature = _signed(_payload())
    await _handle(test_session, body=body, signature=signature, client=StubShopifyReader())
    fresh = StubShopifyReader()
    async with make_client() as client:
        resp = await _post(client, body, signature)
        assert resp.status_code == 200
        assert resp.json()["already_processed"] is True
    assert fresh.asks == []  # the route's client never had to ask Shopify again
    assert len(await _events(test_session)) == 1
    assert await _reference(test_session, "mission_lk_1") == ORDER_GID


async def test_the_event_id_is_unique_at_the_storage_level(restart_env, configured):
    """The arbiter is a constraint, not a read that could race."""
    _make, test_session = restart_env
    async with test_session() as session:
        session.add(
            IntegrationEventRecord(
                event_id=EVENT_ID,
                event_type="resolve.order_linkage.v1",
                body_sha256="0" * 64,
                outcome=RECEIVED,
            )
        )
        await session.commit()
    async with test_session() as session:
        session.add(
            IntegrationEventRecord(
                event_id=EVENT_ID,
                event_type="resolve.order_linkage.v1",
                body_sha256="1" * 64,
                outcome=RECEIVED,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_duplicate_arriving_while_the_first_is_in_flight_is_not_processed_twice(
    restart_env, configured
):
    """
    The winner's claim is committed before it acts, so a concurrent duplicate sees a row whose
    outcome is still 'received'. It is refused with 'retry' — never answered as done, and never
    allowed to run the linkage a second time.
    """
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    body, signature = _signed(_payload())
    async with test_session() as session:  # the winner, mid-flight
        session.add(
            IntegrationEventRecord(
                event_id=EVENT_ID,
                event_type="resolve.order_linkage.v1",
                body_sha256=hashlib.sha256(body).hexdigest(),
                mission_id="mission_lk_1",
                outcome=RECEIVED,
            )
        )
        await session.commit()
    reader = StubShopifyReader()
    with pytest.raises(LinkageIntegrationError) as excinfo:
        await _handle(test_session, body=body, signature=signature, client=reader)
    assert excinfo.value.status_code == 503
    assert "already being processed" in excinfo.value.message
    assert reader.asks == []
    assert await _reference(test_session, "mission_lk_1") is None
    assert len(await _events(test_session)) == 1


async def test_linkage_is_written_once_even_under_repeated_delivery(restart_env, configured):
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    body, signature = _signed(_payload())
    for _ in range(5):
        reader = StubShopifyReader()
        status, _payload_out = await _handle(
            test_session, body=body, signature=signature, client=reader
        )
        assert status == 200
    async with test_session() as session:
        orders = (
            await session.scalars(
                select(ResolveOrderRecord).where(ResolveOrderRecord.mission_id == "mission_lk_1")
            )
        ).all()
        events = await session.scalar(
            select(IntegrationEventRecord).where(IntegrationEventRecord.event_id == EVENT_ID)
        )
    assert len(orders) == 1
    assert orders[0].provider_order_reference == ORDER_GID
    assert events.outcome == "linked"


# --------------------------------------------------------------------------- #
# N, O, P: the trust boundary — who cannot reach this
# --------------------------------------------------------------------------- #
async def test_an_authenticated_browser_session_cannot_establish_linkage(env):
    """
    A logged-in owner is not an integration. Their session carries no authority here, and no
    arrangement of their own credentials — cookie, Authorization header, query parameter — is
    read as a signature.
    """
    make_client, test_session, reader = env
    async with make_client() as client:
        await client.post(
            "/auth/register",
            json={"name": "Owner", "email": "owner@shop.test", "password": "pw12345678"},
        )
        login = await client.post(
            "/auth/login", json={"email": "owner@shop.test", "password": "pw12345678"}
        )
        assert login.status_code == 200
        assert client.cookies, "the session cookie must exist for this test to mean anything"
        body, signature = _signed(_payload())
        unsigned = await _post(client, body, None)
        assert unsigned.status_code == 401
        forged = await _post(client, body, "1" * 64)
        assert forged.status_code == 401
        # The real signature, smuggled into an Authorization header instead: not the trust root.
        moved = await _post(client, body, None, authorization=f"Bearer {signature}")
        assert moved.status_code == 401
        # And as a query parameter.
        query = await client.post(f"{ROUTE}?signature={signature}", content=body)
        assert query.status_code == 401
    assert await _reference(test_session, "mission_lk_1") is None
    assert reader.asks == []
    assert await _events(test_session) == []


def test_the_route_takes_no_session_and_no_identity_dependency():
    from app.routers import integrations as integrations_module
    from app.routers.integrations import submit_order_linkage

    assert set(inspect.signature(submit_order_linkage).parameters) == {
        "request",
        "x_resolve_signature",
        "db",
    }
    source = inspect.getsource(integrations_module)
    for forbidden in (
        "get_current_user", "current_user", "Depends(auth", "auth_secret", "cookies",
        "set_cookie", "link_order_to_shopify_order", "provider_order_reference =",
        "hmac", "verify_signature",
    ):
        assert forbidden not in source, forbidden
    # The router's only collaborator is the handler; it owns no verification of its own.
    assert "handle_linkage_event" in source


def test_the_integration_package_imports_no_session_or_user_auth():
    for rel, source in _production_sources(("app/integrations",)):
        assert "app.auth" not in source, rel
        assert "from app.routers" not in source, rel
        assert "Request" not in source, rel  # auth lives at the HTTP edge only


async def test_no_user_route_accepts_a_shopify_order_identifier(env):
    """The frontend has no door: not one user-facing endpoint carries a merchant order id."""
    make_client, test_session = env[0], env[1]
    async with make_client() as client:
        await client.post(
            "/auth/register",
            json={"name": "Owner", "email": "owner@shop.test", "password": "pw12345678"},
        )
        await client.post(
            "/auth/login", json={"email": "owner@shop.test", "password": "pw12345678"}
        )
        for path in (
            "/missions/mission_lk_1/order",
            "/missions/mission_lk_1/order/confirm",
            ROUTE.replace("/order-linkage", "/order-linkage/confirm"),
        ):
            resp = await client.post(path, json={"provider_order_reference": ORDER_GID})
            assert resp.status_code in (200, 400, 401, 404), path
    assert await _reference(test_session, "mission_lk_1") is None


def test_the_integration_route_is_the_only_new_surface():
    from fastapi.routing import APIRoute

    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    linkage_routes = [route for route in routes if "linkage" in route.path]
    assert [route.path for route in linkage_routes] == [ROUTE]
    assert [sorted(route.methods) for route in linkage_routes] == [["POST"]]
    assert not [route for route in routes if "shopify" in route.path and route.path != ROUTE]


def _frontend_root() -> pathlib.Path | None:
    """
    Where the client lives, if this checkout exposes it.

    The backend image ships only `backend/`, so the scan runs when the frontend is mounted
    (FRONTEND_ROOT, or a sibling directory in a host run) and states plainly that it cannot see the
    client otherwise — it never passes by having nothing to look at.
    """
    candidates = []
    configured_root = os.environ.get("FRONTEND_ROOT")
    if configured_root:
        candidates.append(pathlib.Path(configured_root))
    candidates.append(BACKEND_ROOT.parent / "frontend")
    return next((path for path in candidates if path.is_dir()), None)


def test_the_frontend_contains_no_path_to_the_integration():
    """Nothing client-side can call this: no reference to the route, and no signing material."""
    root = _frontend_root()
    if root is None:
        # The backend image ships only backend/, so an unmounted run genuinely cannot see the
        # client. Skipping says so out loud instead of passing on an empty scan.
        pytest.skip(
            "the frontend tree is not visible in this container — run with "
            "-v <repo>/frontend:/frontend:ro -e FRONTEND_ROOT=/frontend to execute this scan"
        )
    files = [
        file
        for file in sorted(root.rglob("*"))
        if file.is_file()
        and file.suffix in {".ts", ".tsx", ".js", ".jsx"}
        and "node_modules" not in file.parts
        and ".next" not in file.parts
    ]
    assert files, f"{root} has no client sources to scan"
    hits: list[str] = []
    for file in files:
        text = file.read_text(encoding="utf-8", errors="ignore")
        for needle in ("order-linkage", "x-resolve-signature", "linkage_integration_secret",
                       "/integrations", "shopify_order_id"):
            if needle in text:
                hits.append(f"{file.relative_to(root)}: {needle}")
    assert hits == []


def test_the_reasoning_model_can_never_reach_linkage():
    """
    The agent layer — the Gemini-facing code and the mission/resolution machinery it feeds —
    imports no linkage, no Shopify client and no integration module. It proposes; these modules
    are not in its vocabulary.
    """
    for rel, source in _production_sources(
        ("app/agents", "app/protocol", "app/mission_engine", "app/resolution", "app/authority")
    ):
        for forbidden in (
            "link_order_to_shopify_order",
            "order_linkage",
            "app.integrations",
            "shopify",
            "provider_order_reference",
        ):
            assert forbidden not in source, (rel, forbidden)


def test_no_mission_or_checkout_payload_reaches_the_writer():
    """The only production caller of the writer is the integration handler (proved by scan)."""
    assert _production_callers(
        "link_order_to_shopify_order", defined_in="app/tools/shopify/linkage.py"
    ) == ["app/integrations/order_linkage.py"]
    missions_source = (BACKEND_ROOT / "app/routers/missions.py").read_text(encoding="utf-8")
    assert "link_order_to_shopify_order" not in missions_source
    assert "shopify_order_id" not in missions_source


# --------------------------------------------------------------------------- #
# R, S: no mutation, ever, through this door
# --------------------------------------------------------------------------- #
async def test_the_whole_contract_sends_no_shopify_mutation(restart_env, configured):
    """The one document this handler can cause is the read the writer already needed."""
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    reader = StubShopifyReader()
    body, signature = _signed(_payload())
    status, _ = await _handle(test_session, body=body, signature=signature, client=reader)
    assert status == 200
    assert reader.asks == [ORDER_GID]
    # read_order_reference is the only method the handler could even name.
    assert sorted(name for name in dir(reader) if name.startswith("read_")) == [
        "read_order_reference"
    ]


def test_the_integration_modules_ship_no_mutation_document():
    """
    No GraphQL text at all: the integration layer never speaks to Shopify directly, so there is
    no document here that could become a mutation. It reaches the store only through the one
    read-only client the linkage service already uses.
    """
    for rel, source in _production_sources(("app/integrations", "app/routers/integrations.py")):
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                assert not text.lstrip().startswith("mutation"), (rel, text[:40])
                assert "query " not in text.lower(), (rel, text[:40])
                assert not text.startswith("gid://shopify"), rel
        for forbidden in ("returnCreate", "orderCreate", "draftOrderCreate", "fulfillmentCreate",
                          "metafieldsSet", "graphql", "httpx", "ShopifyReadOnlyClient",
                          "ShopifyGraphQL"):
            assert forbidden not in source, (rel, forbidden)


def test_the_integration_adds_no_shopify_scope_or_credential():
    """
    14C.7B is an inbound contract. It handles no Shopify credential itself — it calls the one
    client factory the whole app already shares — and it declares no new permission.
    """
    from app.tools.shopify.data_use import SHOPIFY_DATA_USE

    for rel, source in _production_sources(("app/integrations",)):
        for forbidden in (
            "access_token", "client_secret", "shpat_", "shppa_", "X-Shopify-Access-Token",
            "api_key", "requested_scopes", "access_scopes", "write_returns", "read_orders",
        ):
            assert forbidden not in source, (rel, forbidden)
        # The only Shopify seams allowed are the shared client factory and the single writer.
        seams = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and "shopify" in (node.module or ""):
                seams.add(node.module)
            elif isinstance(node, ast.Import):
                seams.update(a.name for a in node.names if "shopify" in a.name)
        assert seams <= {"app.tools.shopify", "app.tools.shopify.linkage"}, (rel, seams)
    # Declared scopes are exactly what Task 14C.6B recorded — this task widens nothing.
    assert SHOPIFY_DATA_USE.requested_scopes == (
        "read_products",
        "read_orders",
        "read_returns",
        "read_merchant_managed_fulfillment_orders",
        "write_returns",
    )
    assert SHOPIFY_DATA_USE.intended_write_mutations == ("returnCreate",)


# --------------------------------------------------------------------------- #
# T: nothing credential-shaped leaks anywhere
# --------------------------------------------------------------------------- #
async def test_no_secret_or_signature_appears_in_logs_responses_or_storage(env, caplog):
    make_client, test_session = env[0], env[1]
    body, signature = _signed(_payload())
    with caplog.at_level(logging.DEBUG):
        async with make_client() as client:
            ok = await _post(client, body, signature)
            bad = await _post(client, body, "0" * 64)
    blob = " | ".join(
        [ok.text, bad.text, *[r.getMessage() for r in caplog.records]]
        + [json.dumps(await _stored(test_session), default=str)]
        + await _audit_notes(test_session, "mission_lk_1")
    )
    for forbidden in (SECRET, signature, "traceback", "Traceback"):
        assert forbidden not in blob, forbidden[:12]
    assert "shpat_" not in blob and "shppa_" not in blob
    assert "Authorization" not in blob and "Bearer" not in blob
    assert "shopify_client_secret" not in blob
    stored = await _stored(test_session)
    assert [sorted(row) for row in stored] == [
        sorted(
            [
                "body_sha256", "detail", "event_id", "event_type", "mission_id",
                "outcome", "processed_at", "provider_order_reference", "received_at",
                "resolve_order_id",
            ]
        )
    ]
    for row in stored:
        assert not any(
            forbidden in " ".join(row).lower()
            for forbidden in ("signature", "secret", "token", "authorization", "password")
        )
        assert SECRET not in json.dumps(row, default=str)


async def test_the_stored_body_digest_is_not_the_body(restart_env, configured):
    """`body_sha256` detects a reused event_id without keeping the signed bytes around."""
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    body, signature = _signed(_payload())
    await _handle(test_session, body=body, signature=signature, client=StubShopifyReader())
    row = (await _events(test_session))[0]
    assert row.body_sha256 == hashlib.sha256(body).hexdigest()
    assert len(row.body_sha256) == 64
    assert EVENT_ID not in row.body_sha256


async def test_a_refusal_message_never_carries_payload_contents_or_internals(restart_env, configured):
    _make, test_session = restart_env
    await _order(test_session, mission_id="mission_lk_1")
    marker = "customer-9f3a-marker"
    body, signature = _signed(_payload(source=marker, shopify_order_id="ord_not_canonical"))
    status, payload = await _handle(
        test_session, body=body, signature=signature, client=_SilentReader()
    )
    assert status == 400
    assert marker not in payload["detail"]
    assert "Traceback" not in payload["detail"]
    assert SECRET not in payload["detail"]


class _SilentReader(StubShopifyReader):
    def __init__(self):
        super().__init__(known=())


# --------------------------------------------------------------------------- #
# signature / schema primitives
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body,signature,secret,expected",
    [
        (b"x", sign_body("s", b"x"), "s", True),
        (b"y", sign_body("s", b"x"), "s", False),
        (b"x", "", "s", False),
        (b"x", None, "s", False),
        (b"x", 12345, "s", False),
        (b"x", "z" * 64, "s", False),
        (b"x", "0" * 63, "s", False),
        (b"x", sign_body("s", b"x").upper(), "s", True),  # hex is case-insensitive
        (b"x", sign_body("s", b"x") + "ab", "s", False),
        (b"", sign_body("s", b""), "s", True),
    ],
)
def test_signature_verification_is_exact(body, signature, secret, expected):
    assert verify_signature(body, signature, secret) is expected


def test_an_empty_secret_verifies_nothing():
    body = b"x"
    assert verify_signature(body, sign_body("", body), "") is False


def test_the_signature_covers_the_exact_bytes_not_a_canonicalisation():
    a = b'{"b":1,"a":2}'
    b = b'{"a":2,"b":1}'
    assert verify_signature(a, sign_body(SECRET, a), SECRET) is True
    assert verify_signature(b, sign_body(SECRET, a), SECRET) is False


def test_the_event_model_itself_names_no_authority_field():
    """The contract is a closed set. Anything else is not 'ignored' — it is invalid."""
    fields = set(OrderLinkageEvent.model_fields)
    assert fields == {
        "event_id", "event_type", "shopify_order_id", "issued_at",
        "resolve_order_id", "mission_id", "source",
    }
    assert OrderLinkageEvent.model_config["extra"] == "forbid"
    for forbidden in (
        "amount", "currency", "price", "title", "product_id", "product_title",
        "fulfillment_line_item_id", "return_reason", "user_id", "actor_user_id",
        "provider_order_reference", "shopify_order_ids", "status", "authorization",
    ):
        assert forbidden not in fields


def test_the_handler_signature_takes_no_identity():
    """The handler cannot be handed a user or an actor from outside: only bytes and a signature."""
    parameters = inspect.signature(handle_linkage_event).parameters
    assert set(parameters) == {"db", "body", "signature", "client"}
    assert parameters["body"].kind is parameters["signature"].kind == inspect.Parameter.KEYWORD_ONLY
    assert "user" not in str(parameters) and "actor" not in str(parameters)


def test_parse_event_refuses_a_body_that_is_not_an_object():
    for body in (b"", b"[1,2,3]", b'"a string"', b"not json", b"null"):
        with pytest.raises(LinkageIntegrationError) as excinfo:
            parse_event(body)
        assert excinfo.value.status_code == 400


# --------------------------------------------------------------------------- #
# configuration and the untouched rest of the app
# --------------------------------------------------------------------------- #
def test_the_integration_is_off_until_a_deployment_configures_it():
    """The default is no secret, which means the endpoint refuses everything. Nothing is open."""
    settings = get_settings()
    assert settings.linkage_integration_secret == ""
    assert settings.linkage_event_max_age_seconds > 0
    # The secret is a plain str, never a default that could act like a real key.
    field = type(settings).model_fields["linkage_integration_secret"]
    assert field.default == ""


def test_the_inbox_table_is_narrow_and_has_no_credential_column():
    columns = {column.name for column in IntegrationEventRecord.__table__.columns}
    assert columns == {
        "event_id", "event_type", "body_sha256", "mission_id", "resolve_order_id",
        "provider_order_reference", "outcome", "detail", "received_at", "processed_at",
    }
    assert IntegrationEventRecord.__table__.primary_key.columns.keys() == ["event_id"]
    for forbidden in ("signature", "secret", "token", "payload", "raw_body", "customer", "email"):
        assert not any(forbidden in column for column in columns), forbidden
    # The one body-derived column holds a fixed-width digest, not text that could be replayed.
    assert IntegrationEventRecord.__table__.c.body_sha256.type.length == 64


def test_the_linkage_unique_index_is_untouched():
    index = next(
        idx
        for idx in ResolveOrderRecord.__table__.indexes
        if idx.name == "uq_resolve_orders_provider_order_reference"
    )
    assert index.unique is True
    assert [column.name for column in index.columns] == ["provider_order_reference"]


def test_return_provider_configuration_is_untouched_by_this_task():
    """14C.7B registers an inbound route, not an outbound provider."""
    from app.tools.returns import get_return_provider

    assert get_settings().return_provider == "none"
    assert get_return_provider().name == "none"


def test_the_audit_module_still_has_one_writer_and_two_entry_points():
    from app.audit import logger as audit_logger

    assert sorted(
        name for name in dir(audit_logger) if name.startswith("append")
    ) == ["append", "append_event"]
    public = [name for name in dir(audit_logger) if not name.startswith("_")]
    assert not [name for name in public if name.startswith(("update", "delete", "remove"))]


async def test_audit_rows_for_a_non_matching_mission_stay_empty(restart_env, configured):
    """An event that resolves to no order writes no mission-scoped audit row."""
    _make, test_session = restart_env
    body, signature = _signed(_payload(mission_id="mission_absent"))
    await _handle(test_session, body=body, signature=signature, client=StubShopifyReader())
    assert await _audit_rows(test_session, "mission_absent") == []
    assert await _audit_rows(test_session, "mission_lk_1") == []
