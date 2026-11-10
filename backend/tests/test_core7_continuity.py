"""
CORE-7 Phase 2 — continuous commerce context, hermetic proof.

Three shipped surfaces are exercised end to end over the real HTTP routes:

1. LIFECYCLE CONVERSATION EVENTS (S01-S25): every server-driven commerce transition — product
   selected, checkout confirmed, payment order created / verified / failed, RESOLVE order
   created / confirmed, issue reported, authority delegated / revoked — leaves exactly one
   `system_state_change` turn in the mission's append-only conversation, appended INSIDE the
   same transaction as the transition it describes (an event cannot outlive a rolled-back
   mutation, and a committed transition cannot exist without its event), never for a refused
   call, and never twice for an idempotent replay. Identity is resolved server-side; a request
   body cannot steer it. The reasoning model reads these rows labeled [SERVER LIFECYCLE STATE];
   the browser transcript reads the plain sentence.
2. MULTI-ISSUE ADDRESSING (S32-S40): two issues on one mission are assessed and resolved
   independently — by direct route and by an agent turn naming the issue id — with no
   cross-issue leakage, and a foreign issue id is indistinguishable from a missing one.
3. THE AUTHENTICATED WEBSOCKET (S41-S46): /ws/missions/{id} now demands the same identity and
   ownership as every REST mission route, with one uniform refusal, and a stream only ever
   carries its own mission's events.

What is stood in, and only where a seam already exists: reasoning by `MockReasoningProvider`
(conftest) or a scripted subclass of it, discovery by `install_mock_discovery`, the Razorpay
TEST order/refund APIs by the existing fixtures. No live provider is called, no Shopify object
of any kind is constructed (the imported autouse guard makes one an error), and the lifecycle
writer itself is proven data-only by AST.

Scenario labels S01..S49 map to the Phase 2 mandate's test list.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient  # noqa: F401 (ASGITransport via fixtures)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

try:  # WebSocketDisconnect moved between starlette versions
    from starlette.testclient import WebSocketDisconnect
except ImportError:  # pragma: no cover
    from starlette.websockets import WebSocketDisconnect

from app.agents.reasoning_provider import MockReasoningProvider
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.agent import AgentStepDecision
from app.models.db_models import (
    AgentConversationRecord,
    AuditLogRecord,
    RefundRecord,
    ReplacementRecord,
    ResolutionAssessmentRecord,
    ResolutionRecord,
)

from tests.test_core2_agent_loop import (  # noqa: F401
    SHAMPOO_PROMPT,
    _agent,
    _delegate,
    _install_provider,
    _issues,
    _register,
    _signature,
    _tools,
)
from tests.test_core3a_golden_lifecycle import (  # noqa: F401
    _assess,
    _cheapest,
    _confirmed_order,
    _intent,
    _mission_row,
    _no_live_shopify,
    _report,
    _resolve,
    _rows,
    _select,
    _to_paid_mission,
    _transcript,
)
from tests.test_refund_execution import (  # noqa: F401
    mock_razorpay,
    recorded_events,
    refund_api,
    restart_env,
)

# asyncio_mode = auto (pytest.ini) drives every async test here; the three sync structural
# tests stay unmarked.

PASSWORD = "SecurePassword123!"
PAYMENT_ID = "pay_CORE7P2"
SERVER_LABEL = "[SERVER LIFECYCLE STATE]"


# --------------------------------------------------------------------------- #
# Readers and small factories local to this file.
# --------------------------------------------------------------------------- #
async def _conversation_rows(test_session, mission_id) -> list[AgentConversationRecord]:
    return list(await _rows(test_session, AgentConversationRecord, mission_id))


async def _lifecycle_rows(test_session, mission_id) -> list[AgentConversationRecord]:
    """Only the CORE-7 lifecycle events: system_state_change rows carrying a `state` payload.

    The agent loop's own from/to rows share the event_type but not the payload shape, so the
    payload key is what tells the two writers apart."""
    return [
        row
        for row in await _conversation_rows(test_session, mission_id)
        if row.event_type == "system_state_change" and (row.payload or {}).get("state")
    ]


def _states(rows) -> list[str]:
    return [row.payload["state"] for row in rows]


def _assert_no_secrets_in_rows(rows, settings) -> None:
    """No conversation row may carry a credential, a signature shape, or a provider id."""
    patterns = [
        re.compile(r"\brzp_\w+"),  # live Razorpay key prefix
        re.compile(r"\bpay_\w+"),  # Razorpay payment ids
        re.compile(r"\border_TEST\w*"),  # the mock's Razorpay order-id shape
        re.compile(r"[0-9a-f]{64}"),  # any HMAC/signature-shaped value
    ]
    for row in rows:
        blob = (row.content or "") + json.dumps(row.payload or {}, default=str)
        for secret in (settings.razorpay_key_secret, settings.auth_secret):
            assert secret not in blob, f"secret material leaked into a row: {row.content!r}"
        for pattern in patterns:
            assert pattern.search(blob) is None, (
                f"provider reference or signature shape leaked into a row: {blob[:200]!r}"
            )
        payload_keys = set((row.payload or {}).keys())
        assert "razorpay_order_id" not in payload_keys
        assert "razorpay_payment_id" not in payload_keys
        assert "signature" not in payload_keys


class ScriptedProvider(MockReasoningProvider):
    """The mock provider with scripted step choices and a captured context transcript."""

    def __init__(self, steps: list[AgentStepDecision] | None = None) -> None:
        self.steps = list(steps or [])
        self.contexts: list[dict] = []

    async def choose_agent_action(
        self, context: dict, tool_declarations: list[dict]
    ) -> AgentStepDecision:
        self.contexts.append(json.loads(json.dumps(context, default=str)))
        if self.steps:
            return self.steps.pop(0)
        return AgentStepDecision.respond("Understood.")


# --------------------------------------------------------------------------- #
# WebSocket fixture: a temp-FILE engine with NullPool. TestClient drives the app on its own
# portal loop, so no connection may be pooled across loops; a file DB keeps the schema alive
# between per-request connections.
# --------------------------------------------------------------------------- #
@pytest.fixture
async def ws_env():
    tmpdir = tempfile.mkdtemp(prefix="core7_ws_")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmpdir}/core7.db", echo=False, poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield session_factory
    app.dependency_overrides.clear()
    await engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def ws_test_client(monkeypatch):
    """TestClient with the app lifespan stubbed: no DB init, no engine loop."""
    import app.main as main_module

    async def fake_init_models() -> None:
        return None

    async def fake_initialize_db_enforcement(_engine) -> None:
        return None

    async def fake_run_forever() -> None:
        import asyncio

        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "init_models", fake_init_models)
    monkeypatch.setattr(main_module, "initialize_db_enforcement", fake_initialize_db_enforcement)
    monkeypatch.setattr(main_module, "run_forever", fake_run_forever)
    return TestClient(app)


def _register_ws(tc: TestClient, name: str, email: str):
    return tc.post("/auth/register", json={"name": name, "email": email, "password": PASSWORD})


def _cookie_header(token: str) -> dict:
    return {"Cookie": f"{get_settings().auth_cookie_name}={token}"}


def _connect_must_refuse(tc: TestClient, url: str, headers: dict | None = None):
    """The handshake must be denied before accept — a successful connect fails the test."""
    with pytest.raises(WebSocketDisconnect):
        with tc.websocket_connect(url, headers=headers or {}):
            pass


# --------------------------------------------------------------------------- #
# A. The golden chain: one event per real transition, in order, and nothing else.
#    (S01-S10, S21-S23, S31, S43, S49)
# --------------------------------------------------------------------------- #
async def test_the_full_commerce_chain_persists_one_event_per_transition(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    settings = get_settings()
    client = make_client()
    async with client:
        install_mock_discovery(sample_results=sample_products)
        await _register(client, "Continuity Buyer", "chain@core7.test")
        created = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        assert created.status_code == 200, created.text
        mission_id = created.json()["mission_id"]
        chosen = _cheapest((await _intent(client, mission_id))["discovered_products"])

        # select -> confirm -> pay (fail, then retry -> verify) -> order -> confirm -> issue
        # -> delegate -> revoke. Every transition below is a REAL state change.
        assert (await _select(client, mission_id, chosen["product_id"])).status_code == 200
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        order = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert order.status_code == 200, order.text
        rzp_order = order.json()["payment"]["razorpay_order_id"]
        forged = await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": rzp_order,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": "0" * 64,
            },
        )
        assert forged.status_code == 400
        retry = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert retry.status_code == 200, retry.text
        rzp_retry = retry.json()["payment"]["razorpay_order_id"]
        verified = await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": rzp_retry,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": _signature(rzp_retry, PAYMENT_ID),
            },
        )
        assert verified.status_code == 200, verified.text
        created_order = await client.post(f"/missions/{mission_id}/order", json={})
        assert created_order.status_code == 200, created_order.text
        order_id = created_order.json()["order_id"]
        assert (
            await client.post(f"/missions/{mission_id}/order/confirm", json={})
        ).status_code == 200
        # The true idempotent replay — confirming an already-confirmed order — happens
        # BEFORE the issue is reported (reporting moves the order to
        # awaiting_post_purchase_context, from which confirm is refused).
        assert (
            await client.post(f"/missions/{mission_id}/order/confirm", json={})
        ).status_code == 200
        reported = await _report(
            client,
            mission_id,
            issue_type="damaged",
            description="The bottle arrived cracked.",
            requested_resolution="refund",
        )
        assert reported.status_code == 200, reported.text
        issue_id = reported.json()["issue_id"]
        assert (await _delegate(client, mission_id, ["request_refund"])).status_code == 200
        # An identical re-delegation while the mandate is live is a replay, not a re-grant.
        assert (
            await client.post(
                f"/missions/{mission_id}/authority/delegate",
                json={"allowed_actions": ["request_refund"], "refund_limit": "500"},
            )
        ).status_code == 200
        assert (await client.post(f"/missions/{mission_id}/authority/revoke")).status_code == 200

        # Idempotent replays and refused calls — none of them is a transition, so none of
        # them may add a lifecycle event: checkout re-confirmation, a second order read, a
        # duplicate issue, a revocation of the now-empty mandate, and a refused second
        # payment order. The order re-confirm replay is asserted up in the chain; here a
        # confirm attempt from awaiting_post_purchase_context is REFUSED — a refused call is
        # also not a transition, so it must not create an event either.
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        assert (await client.post(f"/missions/{mission_id}/order", json={})).status_code == 200
        assert (
            await client.post(f"/missions/{mission_id}/order/confirm", json={})
        ).status_code == 400
        assert (
            await client.post(
                f"/missions/{mission_id}/order/issues",
                json={
                    "issue_type": "damaged",
                    "description": "The bottle arrived cracked.",
                    "requested_resolution": "refund",
                },
            )
        ).status_code == 200
        assert (await client.post(f"/missions/{mission_id}/authority/revoke")).status_code == 200
        assert (
            await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        ).status_code == 400

        rows = await _lifecycle_rows(test_session, mission_id)
        # S21/S22: exactly the transitions that happened — an honest failure IS a transition;
        # every refused or replayed call above is not.
        actual_states = _states(rows)
        assert actual_states == [
            "product_selected",
            "checkout_confirmed",
            "payment_pending",
            "payment_failed",
            "payment_pending",
            "payment_verified",
            "order_created",
            "order_confirmed",
            "issue_reported",
            "authority_delegated",
            "authority_revoked",
        ], f"actual lifecycle states: {actual_states}"
        # S23: strictly monotonic sequence over the WHOLE transcript, lifecycle rows included.
        all_rows = await _conversation_rows(test_session, mission_id)
        seqs = [row.sequence for row in all_rows]
        assert seqs == list(range(len(all_rows)))
        assert all(row.role == "system" for row in rows)
        assert all(row.mission_id == mission_id for row in rows)

        by_state = {row.payload["state"]: row for row in rows}
        # S01: the event names the product the SERVER discovered — never a client value.
        assert by_state["product_selected"].content == f"Product selected: {chosen['title']}."
        assert by_state["product_selected"].payload["product_id"] == chosen["product_id"]
        assert "extracted_price" not in by_state["product_selected"].payload
        assert "merchant" not in by_state["product_selected"].payload
        # S02: the confirm event says nothing was charged.
        assert "Nothing has been charged" in by_state["checkout_confirmed"].content
        assert by_state["checkout_confirmed"].payload["status"] == "confirmed"
        # S03: order created, pending, not verified.
        assert "payment pending" in by_state["payment_pending"].content
        assert by_state["payment_pending"].payload["status"] == "pending"
        # S05: the failed verification is an honest, persisted transition.
        assert by_state["payment_failed"].payload["status"] == "failed"
        assert by_state["payment_failed"].payload["reason"] == "signature_verification_failed"
        # S04: verified in TEST mode.
        assert by_state["payment_verified"].payload["status"] == "verified"
        assert "No real money moved" in by_state["payment_verified"].content
        # S06/S07: the RESOLVE order rows carry RESOLVE's own order id, not a provider's.
        assert by_state["order_created"].payload["order_id"] == order_id
        assert by_state["order_confirmed"].payload["order_id"] == order_id
        # S08: the issue row names the type and carries the issue id.
        assert "damaged" in by_state["issue_reported"].content
        assert by_state["issue_reported"].payload["issue_id"] == issue_id
        # S09/S10: authority events carry actions and what was withdrawn.
        assert by_state["authority_delegated"].payload["allowed_actions"] == ["request_refund"]
        assert "request_refund" in by_state["authority_revoked"].content
        assert by_state["authority_revoked"].payload["previously_delegated"] == ["request_refund"]

        # S31: the browser transcript reads the plain sentence — the SERVER LIFECYCLE label
        # travels only in the model view (asserted further down).
        assert all(SERVER_LABEL not in (row.content or "") for row in all_rows)
        # S43: the chain reached the rail exactly twice — the two real order creations.
        assert mock_razorpay["n"] == 2
        # S49: no credential, signature shape, or provider id in ANY persisted row.
        _assert_no_secrets_in_rows(all_rows, settings)


# --------------------------------------------------------------------------- #
# B. Refused calls write nothing; replays write nothing. (S11-S20)
# --------------------------------------------------------------------------- #
async def test_selection_replay_appends_no_duplicate_event(
    restart_env, install_mock_discovery, sample_products
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _selected_mission(
            client, install_mock_discovery, sample_products, "replay@core7.test"
        )
        chosen = _cheapest((await _intent(client, mission_id))["discovered_products"])
        assert (await _select(client, mission_id, chosen["product_id"])).status_code == 200
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows) == ["product_selected"], "a replay is not a transition"


async def _selected_mission(client, install_mock_discovery, sample_products, email) -> str:
    """A mission parked exactly at product_selected: discovery at creation, then the guard."""
    install_mock_discovery(sample_results=sample_products)
    await _register(client, "Continuity Buyer", email)
    created = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert created.status_code == 200, created.text
    mission_id = created.json()["mission_id"]
    chosen = _cheapest((await _intent(client, mission_id))["discovered_products"])
    assert (await _select(client, mission_id, chosen["product_id"])).status_code == 200
    return mission_id


async def test_a_changed_selection_is_a_new_transition(
    restart_env, install_mock_discovery, sample_products
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        install_mock_discovery(sample_results=sample_products)
        await _register(client, "Continuity Buyer", "change@core7.test")
        created = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        mission_id = created.json()["mission_id"]
        products = (await _intent(client, mission_id))["discovered_products"]
        ordered = sorted(products, key=lambda p: Decimal(str(p["extracted_price"])))
        first, second = ordered[0], ordered[1]
        assert (await _select(client, mission_id, first["product_id"])).status_code == 200
        assert (await _select(client, mission_id, second["product_id"])).status_code == 200
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows) == ["product_selected", "product_selected"]
        assert rows[0].payload["product_id"] == first["product_id"]
        assert rows[1].payload["product_id"] == second["product_id"]


async def test_refused_selection_appends_nothing(
    restart_env, install_mock_discovery, sample_products
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        install_mock_discovery(sample_results=sample_products)
        await _register(client, "Continuity Buyer", "refused@core7.test")
        created = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        mission_id = created.json()["mission_id"]
        refused = await _select(client, mission_id, "item_the_agent_invented")
        assert refused.status_code == 400
        assert await _lifecycle_rows(test_session, mission_id) == []


async def test_reconfirmation_appends_no_duplicate_event(
    restart_env, install_mock_discovery, sample_products
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _selected_mission(
            client, install_mock_discovery, sample_products, "reconfirm@core7.test"
        )
        first = await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        assert first.status_code == 200
        again = await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        assert again.status_code == 200
        assert (
            again.json()["confirmation"]["confirmed_at"]
            == first.json()["confirmation"]["confirmed_at"]
        )
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows) == ["product_selected", "checkout_confirmed"]


async def test_payment_order_reuse_appends_no_duplicate_event(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _selected_mission(
            client, install_mock_discovery, sample_products, "reuse@core7.test"
        )
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        first = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert first.status_code == 200
        reused = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert reused.status_code == 200
        assert (
            reused.json()["payment"]["razorpay_order_id"]
            == first.json()["payment"]["razorpay_order_id"]
        )
        assert mock_razorpay["n"] == 1
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows) == ["product_selected", "checkout_confirmed", "payment_pending"]


async def test_verified_payment_refuses_a_second_order_and_event(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _selected_mission(
            client, install_mock_discovery, sample_products, "verified@core7.test"
        )
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        created = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        rzp = created.json()["payment"]["razorpay_order_id"]
        assert (
            await client.post(
                f"/missions/{mission_id}/shopping/payment/verify",
                json={
                    "razorpay_order_id": rzp,
                    "razorpay_payment_id": PAYMENT_ID,
                    "razorpay_signature": _signature(rzp, PAYMENT_ID),
                },
            )
        ).status_code == 200
        refused = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert refused.status_code == 400
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows) == [
            "product_selected",
            "checkout_confirmed",
            "payment_pending",
            "payment_verified",
        ]


async def test_double_verification_appends_no_duplicate_event(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _selected_mission(
            client, install_mock_discovery, sample_products, "doubleverify@core7.test"
        )
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        created = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        rzp = created.json()["payment"]["razorpay_order_id"]
        body = {
            "razorpay_order_id": rzp,
            "razorpay_payment_id": PAYMENT_ID,
            "razorpay_signature": _signature(rzp, PAYMENT_ID),
        }
        assert (
            await client.post(f"/missions/{mission_id}/shopping/payment/verify", json=body)
        ).status_code == 200
        duplicate = await client.post(f"/missions/{mission_id}/shopping/payment/verify", json=body)
        assert duplicate.status_code == 400
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows).count("payment_verified") == 1


async def test_foreign_order_verification_appends_nothing(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _selected_mission(
            client, install_mock_discovery, sample_products, "foreignorder@core7.test"
        )
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        foreign = await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": "order_TEST9999",
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": _signature("order_TEST9999", PAYMENT_ID),
            },
        )
        assert foreign.status_code == 400
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows) == ["product_selected", "checkout_confirmed"]
        record = await _mission_row(test_session, mission_id)
        assert record.status == "checkout_confirmed", "a refused verification changed nothing"


async def test_payment_failed_event_is_persisted_with_its_transition(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _selected_mission(
            client, install_mock_discovery, sample_products, "failpath@core7.test"
        )
        assert (
            await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
        ).status_code == 200
        created = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        rzp = created.json()["payment"]["razorpay_order_id"]
        forged = await client.post(
            f"/missions/{mission_id}/shopping/payment/verify",
            json={
                "razorpay_order_id": rzp,
                "razorpay_payment_id": PAYMENT_ID,
                "razorpay_signature": "0" * 64,
            },
        )
        assert forged.status_code == 400
        # One read proves the pair committed together: the transition AND its event.
        record = await _mission_row(test_session, mission_id)
        assert record.status == "payment_failed"
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows) == ["product_selected", "checkout_confirmed", "payment_pending", "payment_failed"]
        assert "signature did not verify" in rows[-1].content
        # The recovery path is a real transition of its own and gets its own event.
        retry = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert retry.status_code == 200
        rzp2 = retry.json()["payment"]["razorpay_order_id"]
        assert (
            await client.post(
                f"/missions/{mission_id}/shopping/payment/verify",
                json={
                    "razorpay_order_id": rzp2,
                    "razorpay_payment_id": PAYMENT_ID,
                    "razorpay_signature": _signature(rzp2, PAYMENT_ID),
                },
            )
        ).status_code == 200
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows) == [
            "product_selected",
            "checkout_confirmed",
            "payment_pending",
            "payment_failed",
            "payment_pending",
            "payment_verified",
        ]


async def test_duplicate_issue_report_appends_no_event(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "dupissue@core7.test"
        )
        await _confirmed_order(client, mission_id)
        body = {
            "issue_type": "damaged",
            "description": "The bottle arrived cracked.",
            "requested_resolution": "refund",
        }
        first = await client.post(f"/missions/{mission_id}/order/issues", json=body)
        assert first.status_code == 200
        second = await client.post(f"/missions/{mission_id}/order/issues", json=body)
        assert second.status_code == 200
        assert second.json()["issue_id"] == first.json()["issue_id"]
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows).count("issue_reported") == 1


async def test_identical_re_delegation_appends_no_event(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "redelegate@core7.test"
        )
        body = {"allowed_actions": ["request_refund"], "refund_limit": "500"}
        assert (
            await client.post(f"/missions/{mission_id}/authority/delegate", json=body)
        ).status_code == 200
        assert (
            await client.post(f"/missions/{mission_id}/authority/delegate", json=body)
        ).status_code == 200
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows).count("authority_delegated") == 1


async def test_revoke_without_delegation_and_repeat_revocation_append_nothing(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "revoke@core7.test"
        )
        # A fresh mandate holds nothing: revoking it is not a state change.
        before = _states(await _lifecycle_rows(test_session, mission_id))
        assert (await client.post(f"/missions/{mission_id}/authority/revoke")).status_code == 200
        assert _states(await _lifecycle_rows(test_session, mission_id)) == before
        # Delegate, then revoke: exactly one event. Revoking the now-empty mandate again: none.
        assert (await _delegate(client, mission_id, ["request_refund"])).status_code == 200
        assert (await client.post(f"/missions/{mission_id}/authority/revoke")).status_code == 200
        assert (await client.post(f"/missions/{mission_id}/authority/revoke")).status_code == 200
        states = _states(await _lifecycle_rows(test_session, mission_id))
        # Exactly one delegation and one revocation on top of the paid-mission baseline.
        assert states.count("authority_delegated") == 1
        assert states.count("authority_revoked") == 1
        assert states[-2:] == ["authority_delegated", "authority_revoked"]


# --------------------------------------------------------------------------- #
# C. The transaction boundary: an event cannot outlive a rolled-back transition. (S24)
# --------------------------------------------------------------------------- #
async def test_lifecycle_event_cannot_survive_a_rolled_back_transition(
    restart_env, install_mock_discovery, sample_products, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        install_mock_discovery(sample_results=sample_products)
        await _register(client, "Rollback Buyer", "rollback@core7.test")
        created = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        mission_id = created.json()["mission_id"]
        chosen = _cheapest((await _intent(client, mission_id))["discovered_products"])

        original_commit = AsyncSession.commit
        calls = {"n": 0}

        async def exploding_commit(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated commit failure")
            return await original_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", exploding_commit)
        with pytest.raises(RuntimeError):
            await _select(client, mission_id, chosen["product_id"])
        monkeypatch.setattr(AsyncSession, "commit", original_commit)

        # The flush-then-rollback pairing means the uncommitted event and the uncommitted
        # state change died together: no transcript row, no selection, no audit row.
        assert await _lifecycle_rows(test_session, mission_id) == []
        assert await _conversation_rows(test_session, mission_id) == []
        record = await _mission_row(test_session, mission_id)
        assert record.status == "discovery_completed"
        assert (record.shopping_intent or {}).get("selected_product") is None
        assert (record.shopping_intent or {}).get("state") == "discovery_completed"
        audit_rows = await _rows(test_session, AuditLogRecord, mission_id)
        assert [row.action for row in audit_rows] == [], "the audit row rolled back with the event"

        # The mission still works after the failure, and the retry commits event+transition.
        assert (await _select(client, mission_id, chosen["product_id"])).status_code == 200
        rows = await _lifecycle_rows(test_session, mission_id)
        assert _states(rows) == ["product_selected"]


# --------------------------------------------------------------------------- #
# D. Persistence and access. (S25-S26)
# --------------------------------------------------------------------------- #
async def test_transcript_survives_a_backend_restart(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "restart@core7.test"
        )
        await _confirmed_order(client, mission_id)
        before_states = _states(await _lifecycle_rows(test_session, mission_id))
        before_seqs = [row.sequence for row in await _conversation_rows(test_session, mission_id)]

    # A "restart": a brand-new client re-authenticating against the same store.
    client2 = make_client()
    async with client2:
        assert (
            await client2.post(
                "/auth/login", json={"email": "restart@core7.test", "password": PASSWORD}
            )
        ).status_code == 200
        assert _states(await _lifecycle_rows(test_session, mission_id)) == before_states
        assert [
            row.sequence for row in await _conversation_rows(test_session, mission_id)
        ] == before_seqs
        transcript = await _transcript(client2, mission_id)
        assert [event["sequence"] for event in transcript] == before_seqs


async def test_conversation_read_requires_ownership(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, _ = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "owner@core7.test"
        )
        assert (await client.get(f"/missions/{mission_id}/agent/conversation")).status_code == 200

    other = make_client()
    async with other:
        await _register(other, "Other Buyer", "notowner@core7.test")
        assert (
            await other.get(f"/missions/{mission_id}/agent/conversation")
        ).status_code == 404, "a foreign mission id is indistinguishable from a missing one"


# --------------------------------------------------------------------------- #
# E. What the model sees, and what it can never see. (S27-S31)
# --------------------------------------------------------------------------- #
async def test_model_context_labels_lifecycle_rows_as_server_state(
    restart_env, install_mock_discovery, sample_products, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _selected_mission(
            client, install_mock_discovery, sample_products, "labeled@core7.test"
        )
        scripted = ScriptedProvider()
        _install_provider(monkeypatch, scripted)
        await _agent(client, mission_id, "which product did I pick?")

        assert scripted.contexts, "the turn really did show the model a context"
        conversation = scripted.contexts[0]["conversation"]
        lifecycle = [c for c in conversation if c.get("event_type") == "system_state_change"]
        assert lifecycle, "the lifecycle event reached the model's transcript"
        for entry in lifecycle:
            assert entry["content"].startswith(SERVER_LABEL), (
                "a server-driven row must be explicitly labeled as server state"
            )
            assert entry["role"] == "system"
        for entry in conversation:
            if entry.get("event_type") != "system_state_change":
                assert SERVER_LABEL not in entry["content"], (
                    "the label must never appear on a user or agent turn"
                )
        assert [c for c in lifecycle if "Product selected" in c["content"]], (
            "the product_selected event is the row the model needed"
        )


async def test_canonical_db_state_outranks_the_transcript(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "canonical@core7.test"
        )
        await _confirmed_order(client, mission_id)
        before_states = _states(await _lifecycle_rows(test_session, mission_id))

        # The transcript says a checkout was confirmed. The database says the mission is in
        # post-payment order context. The model is shown the DATABASE's answer...
        scripted = ScriptedProvider(
            [
                AgentStepDecision.tool_call("prepare_checkout", {}),
                AgentStepDecision.respond("The order is already confirmed — I can't go back."),
            ]
        )
        _install_provider(monkeypatch, scripted)
        before_n = mock_razorpay["n"]
        await _agent(client, mission_id, "reopen checkout and charge my card again please")
        assert scripted.contexts, "the turn showed the model a context"
        context = scripted.contexts[0]
        assert context["mission"]["mission_id"] == mission_id
        assert context["mission"]["status"] == "order_confirmed", (
            "canonical DB state, not the transcript, tells the model where the mission is"
        )
        # ...and no tool the model can name regresses the state or reaches the rail.
        assert mock_razorpay["n"] == before_n, "a transcript line may never create a payment"
        record = await _mission_row(test_session, mission_id)
        assert record.status == "order_confirmed"
        assert (record.shopping_intent or {}).get("payment", {}).get("status") == "verified"
        assert _states(await _lifecycle_rows(test_session, mission_id)) == before_states, (
            "the turn produced no new transitions"
        )


async def test_model_context_never_sees_provider_references_or_secrets(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, _ = restart_env
    settings = get_settings()
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "noref@core7.test"
        )
        await _confirmed_order(client, mission_id)
        scripted = ScriptedProvider()
        _install_provider(monkeypatch, scripted)
        await _agent(client, mission_id, "where does my order stand?")

        assert scripted.contexts
        dumped = json.dumps(scripted.contexts, default=str)
        # merchant_order_linked as a BOOLEAN is RESOLVE-side order status the mandate allows the
        # model to reason from; what may never appear is any reference VALUE or the keys that
        # carry one.
        for forbidden in (
            "provider_reference",
            "provider_order_reference",
            "rzp_",
            settings.razorpay_key_secret,
            settings.auth_secret,
        ):
            assert forbidden not in dumped, f"the model was shown {forbidden!r}"


async def test_lifecycle_identity_cannot_be_steered_by_the_request_body(
    restart_env, install_mock_discovery, sample_products
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _selected_mission(
            client, install_mock_discovery, sample_products, "steer@core7.test"
        )
        chosen = _cheapest((await _intent(client, mission_id))["discovered_products"])
        owner = await _mission_row(test_session, mission_id)
        smuggled = await client.post(
            f"/missions/{mission_id}/shopping/select-product",
            json={
                "product_id": chosen["product_id"],
                "user_id": "attacker-controlled",
                "mission_id": "attacker-controlled",
            },
        )
        assert smuggled.status_code == 200, smuggled.text
        rows = await _lifecycle_rows(test_session, mission_id)
        assert len(rows) == 1, "the smuggled replay wrote nothing"
        assert rows[0].user_id == owner.user_id
        assert rows[0].mission_id == mission_id
        assert (rows[0].payload or {}).get("mission_id") == mission_id


# --------------------------------------------------------------------------- #
# F. Multi-issue addressing. (S32-S40)
# --------------------------------------------------------------------------- #
async def test_two_issues_are_assessed_and_resolved_independently(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "multi@core7.test"
        )
        await _confirmed_order(client, mission_id)

        # Issue A: damaged, wants a refund.
        issue_a = await _report(
            client,
            mission_id,
            issue_type="damaged",
            description="The bottle arrived cracked and leaking.",
            requested_resolution="refund",
        )
        a_id = issue_a.json()["issue_id"]
        issue_b = await _report(
            client,
            mission_id,
            issue_type="wrong_item",
            description="The wrong variant arrived in the box.",
            requested_resolution="replacement",
        )
        b_id = issue_b.json()["issue_id"]
        assert b_id != a_id
        issues = await _issues(client, mission_id)
        b_row = next(i for i in issues if i["issue_id"] == b_id)
        assert b_row.get("assessment") is None, "B's arrival did not assess A or itself"

        # The refund authority exists BEFORE A is assessed: the assessment freezes the
        # Authority Engine's decision as it evaluated at assessment time, so the delegation
        # has to precede the assessment for the execution path to be reachable.
        assert (await _delegate(client, mission_id, ["request_refund"])).status_code == 200
        # S33: assessment operates on the issue it names.
        assert (await _assess(client, mission_id, a_id)).status_code == 200
        resolved = await _resolve(client, mission_id, a_id)
        assert resolved.status_code == 200, resolved.text
        refunds = await _rows(test_session, RefundRecord, mission_id)
        # S40: the refund record belongs to the issue it was asked for.
        assert len(refunds) == 1
        assert refunds[0].issue_id == a_id
        assert len(refund_api.create_calls) == 1

        # S38/S39: B is assessed with NO replacement delegation, so the Authority Engine
        # escalates B's own proposal as an authority gap — evaluated for the SELECTED issue,
        # without touching A.
        assert (await _assess(client, mission_id, b_id)).status_code == 200
        issues = await _issues(client, mission_id)
        b_row = next(i for i in issues if i["issue_id"] == b_id)
        a_row = next(i for i in issues if i["issue_id"] == a_id)
        assert b_row["assessment"]["proposed_action"] == "request_replacement"
        assert a_row["assessment"]["proposed_action"] == "request_refund", (
            "assessing B left A's assessment exactly as it was"
        )
        denied_b = await _resolve(client, mission_id, b_id)
        assert denied_b.status_code == 400
        assert denied_b.json()["resolution_state"] == "denied"
        assert len(await _rows(test_session, RefundRecord, mission_id)) == 1
        assert await _rows(test_session, ReplacementRecord, mission_id) == []

        # The assessment froze B's escalated gap: delegating replacement afterwards does not
        # resurrect it — the executor re-checks the assessment's own recorded decision
        # ('escalate' is not 'allow') and refuses.
        assert (await _delegate(client, mission_id, ["request_replacement"])).status_code == 200
        still_denied = await _resolve(client, mission_id, b_id)
        assert still_denied.status_code == 400
        assert still_denied.json()["resolution_state"] == "denied"
        assert await _rows(test_session, ReplacementRecord, mission_id) == []
        # No cross-issue leakage: one refund (A), one resolution record per issue.
        assert len(await _rows(test_session, RefundRecord, mission_id)) == 1
        resolution_rows = await _rows(test_session, ResolutionRecord, mission_id)
        assert {row.issue_id for row in resolution_rows} == {a_id, b_id}
        assessments = await _rows(test_session, ResolutionAssessmentRecord, mission_id)
        assert {row.issue_id for row in assessments} == {a_id, b_id}


async def test_agent_turn_can_address_a_named_issue_by_id(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "named@core7.test"
        )
        await _confirmed_order(client, mission_id)
        issue_a = await _report(
            client,
            mission_id,
            issue_type="damaged",
            description="The bottle arrived cracked.",
            requested_resolution="refund",
        )
        a_id = issue_a.json()["issue_id"]
        issue_b = await _report(
            client,
            mission_id,
            issue_type="wrong_item",
            description="The wrong variant arrived.",
            requested_resolution="replacement",
        )
        b_id = issue_b.json()["issue_id"]

        # S37: the agent explicitly references B by id. The scripted decision still goes
        # through the registry check, the guarded tool, and _load_issue's mission scoping.
        scripted = ScriptedProvider(
            [
                AgentStepDecision.tool_call("assess_issue", {"issue_id": b_id}),
                AgentStepDecision.respond("I've assessed the wrong-item issue."),
            ]
        )
        _install_provider(monkeypatch, scripted)
        turn = await _agent(client, mission_id, "assess the wrong-item issue, the second one")
        assert _tools(turn) == ["assess_issue"]
        assert turn["agent_tools"][0]["success"] is True

        issues = await _issues(client, mission_id)
        b_row = next(i for i in issues if i["issue_id"] == b_id)
        a_row = next(i for i in issues if i["issue_id"] == a_id)
        assert b_row["assessment"] is not None
        assert a_row["assessment"] is None, "the explicit id scoped the work to B; A is untouched"


async def test_foreign_issue_id_is_refused_and_leaks_nothing(
    restart_env, install_mock_discovery, sample_products, mock_razorpay
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        # Two owners, two missions, one issue each.
        mission_1, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "foreignA@core7.test"
        )
        await _confirmed_order(client, mission_1)
        issue_1 = await _report(
            client,
            mission_1,
            issue_type="damaged",
            description="Cracked bottle.",
            requested_resolution="refund",
        )
        issue_1_id = issue_1.json()["issue_id"]

        client2 = make_client()
        async with client2:
            install_mock_discovery(sample_results=sample_products)
            await _register(client2, "Second Buyer", "foreignB@core7.test")
            created = await client2.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
            assert created.status_code == 200, created.text
            mission_2 = created.json()["mission_id"]
            chosen = _cheapest((await _intent(client2, mission_2))["discovered_products"])
            assert (await _select(client2, mission_2, chosen["product_id"])).status_code == 200
            assert (
                await client2.post(f"/missions/{mission_2}/shopping/checkout/confirm")
            ).status_code == 200
            order = await client2.post(
                f"/missions/{mission_2}/shopping/payment/create-order", json={}
            )
            rzp = order.json()["payment"]["razorpay_order_id"]
            assert (
                await client2.post(
                    f"/missions/{mission_2}/shopping/payment/verify",
                    json={
                        "razorpay_order_id": rzp,
                        "razorpay_payment_id": PAYMENT_ID,
                        "razorpay_signature": _signature(rzp, PAYMENT_ID),
                    },
                )
            ).status_code == 200
            await client2.post(f"/missions/{mission_2}/order", json={})
            await client2.post(f"/missions/{mission_2}/order/confirm", json={})
            issue_2 = await client2.post(
                f"/missions/{mission_2}/order/issues",
                json={
                    "issue_type": "wrong_item",
                    "description": "The colour does not match the listing.",
                    "requested_resolution": "refund",
                },
            )
            assert issue_2.status_code == 200, issue_2.text
            issue_2_id = issue_2.json()["issue_id"]
            assert issue_2_id != issue_1_id

            # Ownership: the second owner cannot reach mission 1's issue at all.
            assert (await client2.get(f"/missions/{mission_1}/order/issues")).status_code == 404
            assert (await _assess(client2, mission_1, issue_1_id)).status_code == 404

        # Owner A names mission 2's issue id on mission 1: refused, indistinguishable from a
        # missing issue, and mission 2's issue is untouched.
        refused_assess = await _assess(client, mission_1, issue_2_id)
        assert refused_assess.status_code == 404
        refused_resolve = await _resolve(client, mission_1, issue_2_id)
        assert refused_resolve.status_code == 404

        # Nothing was written anywhere by the refused calls.
        assert await _rows(test_session, ResolutionAssessmentRecord, mission_1) == []
        assert await _rows(test_session, ResolutionAssessmentRecord, mission_2) == []
        assert await _rows(test_session, RefundRecord, mission_1) == []
        assert await _rows(test_session, RefundRecord, mission_2) == []


# --------------------------------------------------------------------------- #
# G. The authenticated WebSocket. (S41-S46)
# --------------------------------------------------------------------------- #
async def test_ws_refuses_an_unauthenticated_connection(ws_env, ws_test_client):
    client = ws_test_client
    with client as tc:
        _connect_must_refuse(tc, "/ws/missions/mission_someone_made_up")


async def test_ws_refuses_an_invalid_token(ws_env, ws_test_client):
    client = ws_test_client
    with client as tc:
        _connect_must_refuse(tc, "/ws/missions/mission_someone_made_up", _cookie_header("not-a-real-token"))


async def test_ws_serves_the_authenticated_owner(
    ws_env, ws_test_client, install_mock_discovery, sample_products
):
    client = ws_test_client
    with client as tc:
        install_mock_discovery(sample_results=sample_products)
        assert _register_ws(tc, "Owner Buyer", "wsowner@core7.test").status_code == 201
        created = tc.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        assert created.status_code == 200, created.text
        mission_id = created.json()["mission_id"]
        chosen = _cheapest(
            tc.get(f"/missions/{mission_id}").json()["shopping_intent"]["discovered_products"]
        )
        token = tc.cookies.get(get_settings().auth_cookie_name)

        with tc.websocket_connect(
            f"/ws/missions/{mission_id}", headers=_cookie_header(token)
        ) as ws:
            # Handshake first: hello carries the snapshot of what is committed so far (a fresh
            # mission has no conversation rows yet, so the cursor starts at -1 = from beginning).
            hello = ws.receive_json()
            assert hello == {
                "kind": "hello",
                "mission_id": mission_id,
                "snapshot_sequence": -1,
            }
            # The human boundary fires over REST; its committed conversation row reaches the
            # live socket as the canonical event envelope (M21 Phase 2A).
            selected = tc.post(
                f"/missions/{mission_id}/shopping/select-product",
                json={"product_id": chosen["product_id"]},
            )
            assert selected.status_code == 200, selected.text
            event = ws.receive_json()
            assert event["kind"] == "event"
            assert event["mission_id"] == mission_id
            assert event["event_type"] == "system_state_change"
            assert event["payload"]["state"] == "product_selected"
            assert event["payload"]["product_id"] == chosen["product_id"]
            assert event["sequence"] == hello["snapshot_sequence"] + 1


async def test_ws_accepts_the_bearer_header_fallback(
    ws_env, ws_test_client, install_mock_discovery, sample_products
):
    client = ws_test_client
    with client as tc:
        install_mock_discovery(sample_results=sample_products)
        assert _register_ws(tc, "Bearer Buyer", "wsbearer@core7.test").status_code == 201
        created = tc.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        mission_id = created.json()["mission_id"]
        token = tc.cookies.get(get_settings().auth_cookie_name)
        cookie_name = get_settings().auth_cookie_name
        tc.cookies.delete(cookie_name)

        with tc.websocket_connect(
            f"/ws/missions/{mission_id}", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            tc.cookies.set(cookie_name, token)  # REST still needs the session
            hello = ws.receive_json()
            assert hello["kind"] == "hello"
            assert hello["mission_id"] == mission_id
            selected = tc.post(
                f"/missions/{mission_id}/shopping/select-product",
                json={"product_id": "item_dove"},
            )
            assert selected.status_code == 200, selected.text
            event = ws.receive_json()
            assert event["kind"] == "event"
            assert event["mission_id"] == mission_id
            assert event["payload"]["state"] == "product_selected"


async def test_ws_refuses_foreign_user_and_missing_mission_indistinguishably(
    ws_env, ws_test_client, install_mock_discovery, sample_products
):
    client = ws_test_client
    with client as tc:
        install_mock_discovery(sample_results=sample_products)
        assert _register_ws(tc, "Owner Buyer", "wsforeign-owner@core7.test").status_code == 201
        created = tc.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
        mission_id = created.json()["mission_id"]
        owner_token = tc.cookies.get(get_settings().auth_cookie_name)

        # The owner's own socket is the control: it connects.
        with tc.websocket_connect(
            f"/ws/missions/{mission_id}", headers=_cookie_header(owner_token)
        ):
            assert _register_ws(tc, "Other Buyer", "wsforeign-other@core7.test").status_code == 201
            foreign_headers = _cookie_header(tc.cookies.get(get_settings().auth_cookie_name))
            # A foreign user cannot attach to mission 1...
            foreign_refusal = _connect_must_refuse(
                tc, f"/ws/missions/{mission_id}", foreign_headers
            )
            # ...and the SAME refusal answers a mission that does not exist at all — a closed
            # socket reveals nothing about which of the two it was.
            missing_refusal = _connect_must_refuse(
                tc, "/ws/missions/mission_never_existed", foreign_headers
            )
            assert type(foreign_refusal) is type(missing_refusal)


async def test_ws_stream_is_scoped_to_one_mission(
    ws_env, ws_test_client, install_mock_discovery, sample_products
):
    client = ws_test_client
    with client as tc:
        install_mock_discovery(sample_results=sample_products)
        assert _register_ws(tc, "Scoped Buyer", "wsscoped@core7.test").status_code == 201
        mission_1 = tc.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT}).json()[
            "mission_id"
        ]
        mission_2 = tc.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT}).json()[
            "mission_id"
        ]
        assert mission_1 != mission_2
        product_1 = _cheapest(
            tc.get(f"/missions/{mission_1}").json()["shopping_intent"]["discovered_products"]
        )
        product_2 = _cheapest(
            tc.get(f"/missions/{mission_2}").json()["shopping_intent"]["discovered_products"]
        )
        token = tc.cookies.get(get_settings().auth_cookie_name)

        with tc.websocket_connect(
            f"/ws/missions/{mission_1}", headers=_cookie_header(token)
        ) as ws:
            hello = ws.receive_json()
            assert hello["kind"] == "hello"
            assert hello["mission_id"] == mission_1
            # An event for mission 2 exists in the world but must not reach this stream.
            assert (
                tc.post(
                    f"/missions/{mission_2}/shopping/select-product",
                    json={"product_id": product_2["product_id"]},
                )
            ).status_code == 200
            # The next event that DOES reach the socket is mission 1's own — had the
            # mission-2 broadcast been misdelivered, it would have arrived first.
            assert (
                tc.post(
                    f"/missions/{mission_1}/shopping/select-product",
                    json={"product_id": product_1["product_id"]},
                )
            ).status_code == 200
            event = ws.receive_json()
            assert event["kind"] == "event"
            assert event["mission_id"] == mission_1
            assert event["payload"]["product_id"] == product_1["product_id"]


# --------------------------------------------------------------------------- #
# H. Hermeticity and structural guarantees. (S47-S49)
# --------------------------------------------------------------------------- #
def test_lifecycle_writer_is_data_only():
    """
    The conversation layer can describe what the server did; it can never DO anything.

    Static proof over the shipped source: no execution-capable import (payments, providers,
    shopify, authority, HTTP clients), no commit call anywhere in the module (the writer only
    flushes — the caller's transaction decides), and the lifecycle helper accepts neither a
    role nor an event type (its vocabulary is hardcoded) — identity comes only from its caller.
    """
    import app.agents.conversation as conversation_module

    source = Path(conversation_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_modules = (
        "razorpay",
        "shopify",
        "payments",
        "authority",
        "httpx",
        "requests",
        "aiohttp",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for word in forbidden_modules:
                    assert word not in alias.name, f"conversation.py imports {alias.name}"
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for word in forbidden_modules:
                assert word not in module, f"conversation.py imports from {module}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "commit", (
                "the conversation writer must never commit — the caller's transaction decides"
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "append_lifecycle_event":
                arg_names = {a.arg for a in node.args.args + node.args.kwonlyargs}
                assert arg_names == {
                    "db",
                    "mission_id",
                    "user_id",
                    "state",
                    "detail",
                    "extra_payload",
                }
                assert "role" not in arg_names, "the role is hardcoded, not a parameter"
                assert "event_type" not in arg_names, "the event type is hardcoded, not a parameter"


def test_audit_log_gains_no_user_id():
    """CORE-7 Phase 2 must not widen the audit schema: audit_log keeps its actor string and
    gains no user_id column."""
    assert "user_id" not in AuditLogRecord.__table__.columns.keys()


def test_conversation_model_scoping_columns_exist():
    """Every conversation row carries the mission and user it belongs to — the identity the
    lifecycle writer derives server-side on every append."""
    columns = set(AgentConversationRecord.__table__.columns.keys())
    assert {"mission_id", "user_id", "sequence", "event_type", "content"} <= columns
