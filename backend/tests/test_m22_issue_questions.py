"""
Tests for M22 §2 (P4): the issue information-collection workflow.

A reported issue starts with only the buyer's free-text description. The backend
presents a DETERMINISTIC per-category question set (a pure constant — no model
writes it, no client supplies it), stores the buyer's answers on the issue row as
assessment context, and hands both to the resolution assessment. The answers are
the buyer's own words — never a decision, an authorization, or an order fact.

Covers:
- TEST 1:  every issue category has a deterministic, non-empty question set
- TEST 2:  GET questions returns the category's set + empty answers before submission
- TEST 3:  unknown issue id -> 404 (indistinguishable from cross-mission)
- TEST 4:  answers persist (DB row + GET echo) + audit issue_answers_recorded
- TEST 5:  re-submit REPLACES the whole answer map (never a partial mix)
- TEST 6:  validation: unknown id / empty / >500 chars / empty map -> 400 and
           nothing written; non-string value / non-object -> 422 at the body model
- TEST 7:  identity gating: anonymous -> 404, another user -> 404
- TEST 8:  answers after assessment -> 400 (evidence already consumed cannot be rewritten)
- TEST 9:  the assessment context carries category_questions + answers verbatim
- TEST 10: intake response exposes answers (empty at intake); no status change on submit

Hermetic: mock reasoning/discovery via conftest, Razorpay order API mocked —
NO real payment API calls are made.
"""
from __future__ import annotations

import hashlib
import hmac

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.conversation import as_event_frame
from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.db_models import AuditLogRecord, OrderIssueRecord
from app.resolution.issue_questions import (
    ISSUE_CATEGORY_QUESTIONS,
    ANSWER_MAX_CHARS,
    questions_payload,
)
from app.routers import missions as missions_module

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

SHAMPOO_PROMPT = "I want to order shampoo under ₹600 for anti-hairfall."
PASSWORD = "SecurePassword123!"

DAMAGED_ANSWERS = {
    "damage_nature": "The bottle arrived cracked at the cap seam.",
    "packaging_state": "Outer box was crushed on one corner.",
}
# One question, one answer: a partial map is valid — it just REPLACES the whole set.
DAMAGED_ANSWERS_PARTIAL = {"damage_nature": "Cracked cap seam, leaked in transit."}


@pytest.fixture
async def restart_env():
    """Client factory + sessionmaker sharing one engine."""
    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    test_session = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with test_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    def make_client() -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield make_client, test_session

    app.dependency_overrides.clear()
    await test_engine.dispose()


@pytest.fixture
def mock_razorpay(monkeypatch):
    """Deterministic stand-in for the Razorpay TEST order API (records all calls)."""
    calls: list[dict] = []
    counter = {"n": 0}

    def fake_create_test_order(amount_paise, currency, receipt, notes):
        calls.append({"amount_paise": amount_paise, "receipt": receipt})
        counter["n"] += 1
        return {
            "id": f"order_TEST{counter['n']:04d}",
            "amount": amount_paise,
            "currency": currency,
            "status": "created",
            "receipt": receipt,
        }

    monkeypatch.setattr(missions_module, "create_test_order", fake_create_test_order)
    return calls


@pytest.fixture
def recorded_events(monkeypatch):
    """Capture every canonical event frame broadcast during a test (M21 Phase 2A)."""
    events: list[dict] = []

    async def fake_broadcast(row) -> None:
        events.append(as_event_frame(row))

    monkeypatch.setattr(missions_module, "broadcast_event_row", fake_broadcast)
    return events


class ControlledProvider:
    """Stub reasoning provider with a call log — the same seam Task 10 tests use."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.contexts: list[dict] = []

    async def assess_resolution(self, context):
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def install_fake_reasoning(monkeypatch):
    """Swap the reasoning provider factory inside the missions router (same seam
    production uses)."""

    def _install(result=None, error=None) -> ControlledProvider:
        provider = ControlledProvider(result=result, error=error)
        monkeypatch.setattr(
            missions_module, "get_reasoning_provider", lambda name, key: provider
        )
        return provider

    return _install


def _checkout_signature(order_id: str, payment_id: str) -> str:
    secret = get_settings().razorpay_key_secret.encode()
    return hmac.new(secret, f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


async def _register(client: AsyncClient, name: str, email: str) -> None:
    reg = await client.post(
        "/auth/register", json={"name": name, "email": email, "password": PASSWORD}
    )
    assert reg.status_code == 201


async def _login(client: AsyncClient, email: str) -> None:
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200


async def _verified_mission(
    client: AsyncClient, install_mock_discovery, sample_products, mock_razorpay
) -> str:
    install_mock_discovery(sample_results=sample_products)
    await _register(client, "Olga Owner", "olga@shop.com")

    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    assert resp.status_code == 200
    mission_id = resp.json()["mission_id"]

    sel = await client.post(
        f"/missions/{mission_id}/shopping/select-product", json={"product_id": "item_dove"}
    )
    assert sel.status_code == 200
    conf = await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
    assert conf.status_code == 200

    order_resp = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
    assert order_resp.status_code == 200
    razorpay_order_id = order_resp.json()["payment"]["razorpay_order_id"]
    payment_id = "pay_TESTORDER1"
    verify = await client.post(
        f"/missions/{mission_id}/shopping/payment/verify",
        json={
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _checkout_signature(razorpay_order_id, payment_id),
        },
    )
    assert verify.status_code == 200
    return mission_id


async def _confirmed_order(client: AsyncClient, mission_id: str) -> str:
    created = await client.post(f"/missions/{mission_id}/order", json={})
    assert created.status_code == 200
    confirmed = await client.post(f"/missions/{mission_id}/order/confirm", json={})
    assert confirmed.status_code == 200
    return created.json()["order_id"]


async def _damaged_issue(client: AsyncClient, mission_id: str, **overrides) -> str:
    body = {
        "issue_type": "damaged",
        "description": "The shampoo bottle arrived cracked and leaked.",
        "requested_resolution": "refund",
    }
    body.update(overrides)
    resp = await client.post(f"/missions/{mission_id}/order/issues", json=body)
    assert resp.status_code == 200
    return resp.json()["issue_id"]


def _questions_url(mission_id: str, issue_id: str) -> str:
    return f"/missions/{mission_id}/order/issues/{issue_id}/questions"


def _answers_url(mission_id: str, issue_id: str) -> str:
    return f"/missions/{mission_id}/order/issues/{issue_id}/answers"


def _model_proposal(action="request_refund", **overrides):
    proposal = {
        "issue_assessment": "The buyer reports a damaged bottle on a confirmed order.",
        "proposed_action": action,
        "reasoning_summary": "Damage reported on delivery; needs an authority decision.",
        "requested_amount": None,
        "confidence": 0.9,
        "needs_escalation": False,
    }
    proposal.update(overrides)
    return proposal


# --------------------------------------------------------------------------- #
# TEST 1: the question sets are a deterministic constant — every category in the
# issue vocabulary has a non-empty set, and the same type always yields the same
# payload. No client input, no model output can influence the questions.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_every_category_has_a_deterministic_question_set():
    from app.models.mission import IssueType

    categories = {t.value for t in IssueType}
    assert categories == set(ISSUE_CATEGORY_QUESTIONS.keys())

    for issue_type, questions in ISSUE_CATEGORY_QUESTIONS.items():
        assert len(questions) > 0, f"{issue_type} must define questions"
        ids = [q.question_id for q in questions]
        assert len(ids) == len(set(ids)), f"{issue_type} has duplicate question ids"
        for q in questions:
            assert q.prompt.strip(), f"{issue_type}/{q.question_id} prompt is empty"

    # The payload is stable across calls and matches the constant order.
    assert questions_payload("damaged") == [
        {"question_id": q.question_id, "prompt": q.prompt}
        for q in ISSUE_CATEGORY_QUESTIONS["damaged"]
    ]
    assert questions_payload("damaged") == questions_payload("damaged")
    # An unknown category yields no questions — never a fabricated set.
    assert questions_payload("meteor_strike") == []


# --------------------------------------------------------------------------- #
# TEST 2: GET questions before any submission — the category's set with empty
# answers, read from the persisted issue.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_questions_returns_category_set_with_empty_answers(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, recorded_events
):
    make_client, _ = restart_env
    client = make_client()
    mission_id = await _verified_mission(
        client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(client, mission_id)
    issue_id = await _damaged_issue(client, mission_id)

    resp = await client.get(_questions_url(mission_id, issue_id))
    assert resp.status_code == 200
    data = resp.json()

    assert data["issue_id"] == issue_id
    assert data["issue_type"] == "damaged"
    assert data["questions"] == questions_payload("damaged")
    assert data["answers"] == {}


# --------------------------------------------------------------------------- #
# TEST 3: an unknown issue id is 404 — same answer for a wrong id and for a
# cross-mission issue id, never an existence oracle.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unknown_issue_is_404(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, recorded_events
):
    make_client, _ = restart_env
    client = make_client()
    mission_id = await _verified_mission(
        client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(client, mission_id)

    unknown = await client.get(_questions_url(mission_id, "issue_doesnotexist"))
    assert unknown.status_code == 404
    post_unknown = await client.post(
        _answers_url(mission_id, "issue_doesnotexist"), json={"answers": DAMAGED_ANSWERS}
    )
    assert post_unknown.status_code == 404


# --------------------------------------------------------------------------- #
# TEST 4: the happy path — answers persist on the issue row, GET echoes them,
# and the append-only audit records the intake as user context that authorizes
# nothing. Submission changes no issue/order status.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_answers_persist_and_are_audited(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, recorded_events
):
    make_client, test_session = restart_env
    client = make_client()
    mission_id = await _verified_mission(
        client, install_mock_discovery, sample_products, mock_razorpay
    )
    order_id = await _confirmed_order(client, mission_id)
    issue_id = await _damaged_issue(client, mission_id)

    resp = await client.post(
        _answers_url(mission_id, issue_id), json={"answers": DAMAGED_ANSWERS}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["answers"] == DAMAGED_ANSWERS
    assert data["questions"] == questions_payload("damaged")

    # The row itself carries the answers.
    async with test_session() as session:
        row = await session.get(OrderIssueRecord, issue_id)
        assert row.answers == DAMAGED_ANSWERS
        assert row.status == "reported"
        assert row.order_id == order_id

    # Append-only audit: the submission is recorded as user context, authorizing nothing.
    async with test_session() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLogRecord).where(AuditLogRecord.action == "issue_answers_recorded")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].actor == "user"
        assert "authorizes nothing" in rows[0].reason
        assert issue_id in rows[0].reason

    # GET echoes the stored map.
    got = await client.get(_questions_url(mission_id, issue_id))
    assert got.status_code == 200
    assert got.json()["answers"] == DAMAGED_ANSWERS

    # Whitespace-only trim: submitted padding is stripped in storage.
    resp2 = await client.post(
        _answers_url(mission_id, issue_id),
        json={"answers": {"packaging_state": "  box was intact  "}},
    )
    assert resp2.status_code == 200
    got2 = await client.get(_questions_url(mission_id, issue_id))
    assert got2.json()["answers"] == {"packaging_state": "box was intact"}


# --------------------------------------------------------------------------- #
# TEST 5: re-submitting REPLACES the whole map — the stored set is never a
# partial mix of old and new answers.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resubmit_replaces_the_whole_answer_map(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, recorded_events
):
    make_client, test_session = restart_env
    client = make_client()
    mission_id = await _verified_mission(
        client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(client, mission_id)
    issue_id = await _damaged_issue(client, mission_id)

    first = await client.post(
        _answers_url(mission_id, issue_id), json={"answers": DAMAGED_ANSWERS}
    )
    assert first.status_code == 200

    second = await client.post(
        _answers_url(mission_id, issue_id), json={"answers": DAMAGED_ANSWERS_PARTIAL}
    )
    assert second.status_code == 200
    assert second.json()["answers"] == DAMAGED_ANSWERS_PARTIAL

    async with test_session() as session:
        row = await session.get(OrderIssueRecord, issue_id)
        assert row.answers == DAMAGED_ANSWERS_PARTIAL


# --------------------------------------------------------------------------- #
# TEST 6: validation. Unknown question id / empty answer / overlong answer /
# empty map are 400 rejections of the whole submission; non-string values and
# non-object bodies are 422 at the request model. After every rejection the
# stored answers are unchanged.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_answer_validation_rejections_write_nothing(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, recorded_events
):
    make_client, test_session = restart_env
    client = make_client()
    mission_id = await _verified_mission(
        client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(client, mission_id)
    issue_id = await _damaged_issue(client, mission_id)

    # Seed a known-good map that must survive every rejection below.
    seed = await client.post(_answers_url(mission_id, issue_id), json={"answers": DAMAGED_ANSWERS})
    assert seed.status_code == 200

    rejections_400 = {
        "unknown_question_id": {"answers": {"not_a_real_question": "hello"}},
        "cross_category_id": {"answers": {"defect_behavior": "hello"}},
        "empty_answer": {"answers": {"damage_nature": "   "}},
        "overlong_answer": {
            "answers": {"damage_nature": "x" * (ANSWER_MAX_CHARS + 1)}
        },
        "empty_map": {"answers": {}},
    }
    for label, body in rejections_400.items():
        resp = await client.post(_answers_url(mission_id, issue_id), json=body)
        assert resp.status_code == 400, f"{label}: expected 400, got {resp.status_code}"

    rejections_422 = {
        "non_string_value": {"answers": {"damage_nature": 12345}},
        "non_object_body": {"answers": ["damage_nature", "hello"]},
    }
    for label, body in rejections_422.items():
        resp = await client.post(_answers_url(mission_id, issue_id), json=body)
        assert resp.status_code == 422, f"{label}: expected 422, got {resp.status_code}"

    # Nothing was written by any rejection; the seeded map survives intact.
    got = await client.get(_questions_url(mission_id, issue_id))
    assert got.json()["answers"] == DAMAGED_ANSWERS
    async with test_session() as session:
        row = await session.get(OrderIssueRecord, issue_id)
        assert row.answers == DAMAGED_ANSWERS


# --------------------------------------------------------------------------- #
# TEST 7: identity gating. An anonymous caller and a different authenticated
# user both see 404 on both surfaces — ownership is proven, not asserted.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_identity_gating_anonymous_and_other_user(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, recorded_events
):
    make_client, _ = restart_env
    client = make_client()
    mission_id = await _verified_mission(
        client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(client, mission_id)
    issue_id = await _damaged_issue(client, mission_id)

    # Anonymous caller: the mission belongs to a registered user, not demo-user.
    anon = make_client()
    assert (await anon.get(_questions_url(mission_id, issue_id))).status_code == 404
    assert (
        await anon.post(_answers_url(mission_id, issue_id), json={"answers": DAMAGED_ANSWERS})
    ).status_code == 404

    # A different registered user: 404 on both surfaces.
    other = make_client()
    await _register(other, "Oleg Other", "oleg@shop.com")
    await _login(other, "oleg@shop.com")
    assert (await other.get(_questions_url(mission_id, issue_id))).status_code == 404
    assert (
        await other.post(_answers_url(mission_id, issue_id), json={"answers": DAMAGED_ANSWERS})
    ).status_code == 404


# --------------------------------------------------------------------------- #
# TEST 8: the reported-status gate. Once an assessment has consumed the issue,
# its evidence — including the answers — cannot be rewritten afterwards.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_answers_rejected_after_assessment_consumed_the_issue(
    restart_env, install_mock_discovery, sample_products, mock_razorpay,
    install_fake_reasoning, recorded_events,
):
    make_client, test_session = restart_env
    client = make_client()
    mission_id = await _verified_mission(
        client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(client, mission_id)
    issue_id = await _damaged_issue(client, mission_id)

    provider = install_fake_reasoning(result=_model_proposal(action="request_more_information"))
    assessed = await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/assess")
    assert assessed.status_code == 200

    async with test_session() as session:
        row = await session.get(OrderIssueRecord, issue_id)
        assert row.status == "under_review"

    resp = await client.post(
        _answers_url(mission_id, issue_id), json={"answers": DAMAGED_ANSWERS}
    )
    assert resp.status_code == 400
    assert "reported" in resp.json()["detail"]

    # The row still carries exactly the answers the assessment saw (none).
    async with test_session() as session:
        row = await session.get(OrderIssueRecord, issue_id)
        assert row.answers is None


# --------------------------------------------------------------------------- #
# TEST 9: the canonical assessment context carries the category's question set
# and the buyer's answers verbatim — the model reasons over what the buyer
# actually said, next to the backend's own DB facts.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_assessment_context_carries_questions_and_answers(
    restart_env, install_mock_discovery, sample_products, mock_razorpay,
    install_fake_reasoning, recorded_events,
):
    make_client, _ = restart_env
    client = make_client()
    mission_id = await _verified_mission(
        client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(client, mission_id)
    issue_id = await _damaged_issue(client, mission_id)

    submitted = await client.post(
        _answers_url(mission_id, issue_id), json={"answers": DAMAGED_ANSWERS}
    )
    assert submitted.status_code == 200

    provider = install_fake_reasoning(result=_model_proposal(action="request_refund"))
    assessed = await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/assess")
    assert assessed.status_code == 200

    assert len(provider.contexts) == 1
    issue_ctx = provider.contexts[0]["issue"]
    assert issue_ctx["category_questions"] == questions_payload("damaged")
    assert issue_ctx["answers"] == DAMAGED_ANSWERS


# --------------------------------------------------------------------------- #
# TEST 10: the canonical issue payload exposes answers (empty at intake), and a
# successful submission changes no issue status — intake state stays exactly as
# the §2 lifecycle defines it.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_issue_response_exposes_answers_and_submit_changes_no_status(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, recorded_events
):
    make_client, test_session = restart_env
    client = make_client()
    mission_id = await _verified_mission(
        client, install_mock_discovery, sample_products, mock_razorpay
    )
    await _confirmed_order(client, mission_id)

    intake = await client.post(
        f"/missions/{mission_id}/order/issues",
        json={
            "issue_type": "damaged",
            "description": "The shampoo bottle arrived cracked and leaked.",
            "requested_resolution": "refund",
        },
    )
    assert intake.status_code == 200
    data = intake.json()
    issue_id = data["issue_id"]
    assert data["answers"] == {}
    assert data["questions"] == questions_payload("damaged")
    assert data["status"] == "reported"

    submitted = await client.post(
        _answers_url(mission_id, issue_id), json={"answers": DAMAGED_ANSWERS}
    )
    assert submitted.status_code == 200

    async with test_session() as session:
        row = await session.get(OrderIssueRecord, issue_id)
        assert row.status == "reported"
        assert row.answers == DAMAGED_ANSWERS
