"""
PP Phase 2: payment-level cumulative refund integrity.

The contract under test (the executor's accounting block):
  - a refund can never push a payment's cumulative refunds past what was actually paid
    (ceiling = the canonical PAID amount), with local reservations AND provider-side
    refunds no local row knows about both counted;
  - reservations are released ONLY on established outcomes (provider-confirmed failure or
    a verified settlement) — every unknown stays reserved;
  - a provider listing runs once per create-capable attempt, serving crash-window
    adoption and reconciliation; a listing failure with an ambiguous prior attempt fails
    CLOSED (no blind second create);
  - one provider-call window per refund row: a live lease refuses with 409, a stale lease
    is recovered, and the database's unique(issue_id) constraint is the final arbiter.

Hermetic: mock reasoning/discovery via conftest; the Razorpay refund API is fully
mocked — NO real payment or refund API calls are made. PostgreSQL advisory-lock
serialization is proven separately in test_refund_pg_concurrency.py.
"""
import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.db_models import (
    AuditLogRecord,
    MissionRecord,
    RefundRecord,
    ResolveOrderRecord,
)
from app.routers import missions as missions_module

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

SHAMPOO_PROMPT = "I want to order shampoo under ₹600 for anti-hairfall."
PASSWORD = "SecurePassword123!"

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def restart_env():
    """Client factory + sessionmaker sharing one engine."""
    test_engine = create_engine_shim()
    test_session = make_sessionmaker(test_engine)

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


def create_engine_shim():
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(TEST_DATABASE_URL, echo=False)


def make_sessionmaker(engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
def mock_razorpay(monkeypatch):
    """Deterministic stand-in for the Razorpay TEST order API."""
    counter = {"n": 0}

    def fake_create_test_order(amount_paise, currency, receipt, notes):
        counter["n"] += 1
        return {
            "id": f"order_TEST{counter['n']:04d}",
            "amount": amount_paise,
            "currency": currency,
            "status": "created",
            "receipt": receipt,
        }

    monkeypatch.setattr(missions_module, "create_test_order", fake_create_test_order)
    return counter


class RefundApi:
    """In-memory Razorpay refund API with call counters and failure injection."""

    def __init__(self):
        self.test_mode = True
        self.refunds: dict[str, dict] = {}
        self.calls = {"create": 0, "fetch": 0, "list": 0}
        self.create_calls: list[tuple[str, int, str]] = []
        self.create_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.list_error: Exception | None = None
        self.create_response_status = "processed"
        self.fetch_status: str | None = None
        self.create_lands_despite_error = False

    def is_test_mode(self) -> bool:
        return self.test_mode

    def create(self, payment_id, amount_paise, resolve_refund_id):
        self.calls["create"] += 1
        self.create_calls.append((payment_id, amount_paise, resolve_refund_id))
        if self.create_error is not None:
            if self.create_lands_despite_error:
                rid = f"rfnd_TEST{len(self.refunds) + 1}"
                self.refunds[rid] = {
                    "id": rid, "status": "processed", "payment_id": payment_id,
                    "amount": amount_paise,
                    "notes": {"resolve_refund_id": resolve_refund_id},
                }
            raise self.create_error
        rid = f"rfnd_TEST{len(self.refunds) + 1}"
        self.refunds[rid] = {
            "id": rid, "status": self.create_response_status, "payment_id": payment_id,
            "amount": amount_paise, "notes": {"resolve_refund_id": resolve_refund_id},
        }
        return dict(self.refunds[rid])

    def fetch(self, payment_id, razorpay_refund_id):
        self.calls["fetch"] += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        stored = self.refunds.get(
            razorpay_refund_id, {"id": razorpay_refund_id, "status": "processed"}
        )
        status = self.fetch_status if self.fetch_status is not None else stored["status"]
        return {**stored, "status": status}

    def list_for_payment(self, payment_id):
        self.calls["list"] += 1
        if self.list_error is not None:
            raise self.list_error
        return [dict(r) for r in self.refunds.values() if r.get("payment_id") == payment_id]


@pytest.fixture
def refund_api(monkeypatch):
    api = RefundApi()
    monkeypatch.setattr(missions_module, "is_test_mode", api.is_test_mode)
    monkeypatch.setattr(missions_module, "create_payment_refund", api.create)
    monkeypatch.setattr(missions_module, "fetch_refund", api.fetch)
    monkeypatch.setattr(missions_module, "list_refunds_for_payment", api.list_for_payment)
    return api


def _checkout_signature(order_id: str, payment_id: str) -> str:
    secret = get_settings().razorpay_key_secret.encode()
    return hmac.new(secret, f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


async def _register(client: AsyncClient, name: str, email: str) -> None:
    assert (
        await client.post(
            "/auth/register", json={"name": name, "email": email, "password": PASSWORD}
        )
    ).status_code == 201


async def _login(client: AsyncClient, email: str) -> None:
    assert (
        await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    ).status_code == 200


async def _verified_mission(client, install_mock_discovery, sample_products, mock_razorpay) -> str:
    install_mock_discovery(sample_results=sample_products)
    await _register(client, "Olga Owner", "olga@shop.com")
    resp = await client.post("/missions/shopping", json={"prompt": SHAMPOO_PROMPT})
    mission_id = resp.json()["mission_id"]
    assert (
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": "item_dove"}
        )
    ).status_code == 200
    assert (await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")).status_code == 200
    order_resp = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
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
    # Issues live on a confirmed order: create + confirm it before any issue is filed.
    assert (await client.post(f"/missions/{mission_id}/order", json={})).status_code == 200
    assert (await client.post(f"/missions/{mission_id}/order/confirm", json={})).status_code == 200
    return mission_id


_ISSUE_SEQ = {"n": 0}


async def _issue(client, mission_id, description=None) -> str:
    # Intake dedupes exact repeats (same type + description + resolution), so every
    # call must describe a genuinely different report to produce a distinct issue.
    _ISSUE_SEQ["n"] += 1
    if description is None:
        description = f"I received the wrong shampoo. (report #{_ISSUE_SEQ['n']})"
    resp = await client.post(
        f"/missions/{mission_id}/order/issues",
        json={
            "issue_type": "wrong_item",
            "description": description,
            "requested_resolution": "refund",
        },
    )
    assert resp.status_code == 200
    return resp.json()["issue_id"]


async def _set_mandate_authority(test_session, mission_id, actions, budget):
    async with test_session() as session:
        rec = await session.get(MissionRecord, mission_id)
        mandate = dict(rec.mandate)
        authority = dict(mandate.get("authority") or {})
        authority["allowed_actions"] = list(actions)
        authority["recovery_budget"] = str(budget)
        mandate["authority"] = authority
        rec.mandate = mandate
        await session.commit()


async def _assess(client, mission_id, issue_id):
    resp = await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/assess")
    assert resp.status_code == 200
    return resp.json()


async def _refund(client, mission_id, issue_id):
    return await client.post(f"/missions/{mission_id}/order/issues/{issue_id}/refund")


async def _allowed_issue(client, test_session, mission_id) -> str:
    await _set_mandate_authority(test_session, mission_id, ["request_refund"], "1000")
    issue_id = await _issue(client, mission_id)
    assessment = await _assess(client, mission_id, issue_id)
    assert assessment["proposed_action"] == "request_refund"
    assert assessment["authority_decision"] == "allow"
    return issue_id


async def _tamper_payment_paise(test_session, mission_id, amount_paise: int) -> None:
    async with test_session() as session:
        rec = await session.get(MissionRecord, mission_id)
        intent = dict(rec.shopping_intent)
        payment = dict(intent["payment"])
        payment["amount_paise"] = amount_paise
        intent["payment"] = payment
        rec.shopping_intent = intent
        await session.commit()


async def _refund_row(test_session, issue_id) -> RefundRecord:
    async with test_session() as session:
        return (
            await session.execute(
                select(RefundRecord).where(RefundRecord.issue_id == issue_id)
            )
        ).scalar_one()


async def _audit_notes(test_session, mission_id) -> list[str]:
    async with test_session() as session:
        rows = (
            await session.execute(
                select(AuditLogRecord).where(AuditLogRecord.mission_id == mission_id)
            )
        ).scalars().all()
    return [r.reason or "" for r in rows]


# --------------------------------------------------------------------------- #
# Ceiling: cumulative refunds across issues can never exceed what was paid
# --------------------------------------------------------------------------- #
async def test_second_issue_refund_blocked_after_full_consumption(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        # Test data only: the payment actually captured 20000 paise. The canonical refund
        # is min(payment, order) — the first issue consumes the whole ceiling.
        await _tamper_payment_paise(test_session, mission_id, 20000)

        issue_1 = await _allowed_issue(client, test_session, mission_id)
        first = await _refund(client, mission_id, issue_1)
        assert first.status_code == 200
        assert first.json()["status"] == "verified"
        assert first.json()["amount_paise"] == 20000

        issue_2 = await _allowed_issue(client, test_session, mission_id)
        second = await _refund(client, mission_id, issue_2)
        assert second.status_code == 400
        detail = second.json()["detail"]
        assert "Cumulative refunds can never exceed" in detail
        assert "reserved locally: 20000" in detail

        # The refusal left no row and touched the provider once (the first refund only).
        async with test_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(RefundRecord).where(
                    RefundRecord.issue_id == issue_2
                )
            )
            assert count == 0
        assert refund_api.calls["create"] == 1


async def test_provider_confirmed_failure_releases_reservation(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        refund_api.create_response_status = "failed"  # provider confirms the failure

        issue_1 = await _allowed_issue(client, test_session, mission_id)
        first = (await _refund(client, mission_id, issue_1)).json()
        assert first["status"] == "failed"
        assert first["provider_status"] == "failed"

        # A provider-confirmed failure consumed nothing: the second issue's refund proceeds.
        refund_api.create_response_status = "processed"
        issue_2 = await _allowed_issue(client, test_session, mission_id)
        second = await _refund(client, mission_id, issue_2)
        assert second.status_code == 200
        assert second.json()["status"] == "verified"
        assert second.json()["amount_paise"] == 39900
        assert refund_api.calls["create"] == 2


async def test_ambiguous_failure_keeps_reservation(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        refund_api.create_error = RuntimeError("provider unavailable")

        issue_1 = await _allowed_issue(client, test_session, mission_id)
        assert (await _refund(client, mission_id, issue_1)).status_code == 502
        row_1 = await _refund_row(test_session, issue_1)
        assert row_1.status == "failed"
        assert row_1.razorpay_refund_id is None  # outcome at the provider is unknown

        # The unknown may have landed: it stays reserved, so the ceiling is exhausted.
        issue_2 = await _allowed_issue(client, test_session, mission_id)
        second = await _refund(client, mission_id, issue_2)
        assert second.status_code == 400
        assert "reserved locally: 39900" in second.json()["detail"]


# --------------------------------------------------------------------------- #
# Reconciliation: provider-side money no local row knows about counts too
# --------------------------------------------------------------------------- #
async def test_unattributed_provider_refund_counts_against_ceiling(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        # A refund made outside RESOLVE (no notes claim any local row knows).
        refund_api.refunds["rfnd_GHOST"] = {
            "id": "rfnd_GHOST",
            "status": "processed",
            "payment_id": "pay_TESTORDER1",
            "amount": 20000,
            "notes": {"origin": "outside"},
        }

        issue_1 = await _allowed_issue(client, test_session, mission_id)
        refused = await _refund(client, mission_id, issue_1)
        assert refused.status_code == 400
        detail = refused.json()["detail"]
        assert "provider-side unattributed: 20000" in detail
        assert refund_api.calls["create"] == 0  # the provider is NOT relied on to reject

        # Once the outside money is gone, the full ceiling is available again.
        del refund_api.refunds["rfnd_GHOST"]
        allowed = await _refund(client, mission_id, issue_1)
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "verified"
        assert refund_api.calls["create"] == 1


async def test_adopted_provider_refund_is_not_double_counted(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        # The create lands at the provider but the response is lost -> 502, then adoption.
        refund_api.create_error = TimeoutError("connection reset after provider accepted")
        refund_api.create_lands_despite_error = True
        issue_1 = await _allowed_issue(client, test_session, mission_id)
        assert (await _refund(client, mission_id, issue_1)).status_code == 502

        refund_api.create_error = None
        refund_api.create_lands_despite_error = False
        adopted = (await _refund(client, mission_id, issue_1)).json()
        assert adopted["status"] == "verified"
        assert adopted["razorpay_refund_id"] == "rfnd_TEST1"
        assert refund_api.calls["create"] == 1  # never a second create

        # Shrink the adopted row (test data) so the ceiling math is discriminating: the
        # provider item for the adopted refund must NOT be counted again as unattributed.
        async with test_session() as session:
            row = await session.get(RefundRecord, adopted["refund_id"])
            row.amount_paise = 10000
            await session.commit()

        issue_2 = await _allowed_issue(client, test_session, mission_id)
        second = await _refund(client, mission_id, issue_2)
        assert second.status_code == 400
        detail = second.json()["detail"]
        assert "reserved locally: 10000" in detail
        assert "provider-side unattributed: 0" in detail


# --------------------------------------------------------------------------- #
# Listing failure: honest fallback, and fail-closed on an ambiguous prior attempt
# --------------------------------------------------------------------------- #
async def test_listing_failure_first_attempt_proceeds_honestly(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        refund_api.list_error = RuntimeError("listing endpoint down")

        issue_id = await _allowed_issue(client, test_session, mission_id)
        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
        assert refund_api.calls["create"] == 1

        # The honest fallback is disclosed on the audit trail.
        notes = await _audit_notes(test_session, mission_id)
        assert any("provider refund listing failed" in n for n in notes)


async def test_listing_failure_with_ambiguous_row_fails_closed(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        # First attempt: the create errored (outcome unknown), leaving a row with no id.
        refund_api.create_error = RuntimeError("provider unavailable")
        issue_id = await _allowed_issue(client, test_session, mission_id)
        assert (await _refund(client, mission_id, issue_id)).status_code == 502
        creates_after_first = refund_api.calls["create"]

        # Retry while the listing is down: whether the first create landed is unknowable,
        # so no second create may be attempted.
        refund_api.list_error = RuntimeError("listing endpoint down")
        refused = await _refund(client, mission_id, issue_id)
        assert refused.status_code == 503
        assert "no second refund is created" in refused.json()["detail"]
        assert refund_api.calls["create"] == creates_after_first

        # Once the listing is back, the retry resolves honestly (nothing had landed).
        refund_api.create_error = None
        refund_api.list_error = None
        recovered = await _refund(client, mission_id, issue_id)
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "verified"
        assert refund_api.calls["create"] == creates_after_first + 1


# --------------------------------------------------------------------------- #
# Submission lease: one provider-call window per refund row
# --------------------------------------------------------------------------- #
async def test_live_lease_refuses_with_409_without_touching_provider(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        refund_api.create_error = RuntimeError("provider unavailable")
        issue_id = await _allowed_issue(client, test_session, mission_id)
        assert (await _refund(client, mission_id, issue_id)).status_code == 502

        # Simulate a concurrent executor holding the window right now.
        async with test_session() as session:
            row = await session.get(RefundRecord, (await _refund_row(test_session, issue_id)).refund_id)
            row.submission_state = "in_flight"
            row.submission_owner = "ghost-owner"
            row.submission_claimed_at = datetime.utcnow()
            await session.commit()

        creates_before = refund_api.calls["create"]
        refused = await _refund(client, mission_id, issue_id)
        assert refused.status_code == 409
        assert "in flight" in refused.json()["detail"]
        assert refund_api.calls["create"] == creates_before

        # A live owner's lease is never stolen.
        row = await _refund_row(test_session, issue_id)
        assert row.submission_state == "in_flight"
        assert row.submission_owner == "ghost-owner"


async def test_stale_lease_is_recovered_and_execution_proceeds(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        refund_api.create_error = RuntimeError("provider unavailable")
        issue_id = await _allowed_issue(client, test_session, mission_id)
        assert (await _refund(client, mission_id, issue_id)).status_code == 502

        # The lease was abandoned past the staleness bound (backend died mid-window).
        async with test_session() as session:
            row = await session.get(RefundRecord, (await _refund_row(test_session, issue_id)).refund_id)
            row.submission_state = "in_flight"
            row.submission_owner = "ghost-owner"
            row.submission_claimed_at = datetime.utcnow() - timedelta(seconds=120)
            await session.commit()

        refund_api.create_error = None
        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
        assert refund_api.calls["create"] == 2

        # Terminal commits always clear the lease.
        row = await _refund_row(test_session, issue_id)
        assert row.submission_state == "idle"
        assert row.submission_owner is None


async def test_terminal_commits_clear_the_lease_on_failure_and_pending(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        issue_id = await _allowed_issue(client, test_session, mission_id)

        # Failed create: the row stays failed, the window is released.
        refund_api.create_error = RuntimeError("provider unavailable")
        assert (await _refund(client, mission_id, issue_id)).status_code == 502
        row = await _refund_row(test_session, issue_id)
        assert row.submission_state == "idle"
        assert row.submission_owner is None

        # Retry of the SAME row into a pending provider state: stays resubmittable,
        # window released again — one row, one lease lifecycle.
        refund_api.create_error = None
        refund_api.create_response_status = "pending"
        pending = (await _refund(client, mission_id, issue_id)).json()
        assert pending["status"] == "pending"
        row = await _refund_row(test_session, issue_id)
        assert row.refund_id == pending["refund_id"]
        assert row.submission_state == "idle"
        assert row.submission_owner is None


async def test_lease_race_admits_exactly_one_owner(tmp_path):
    """The atomic claim: simultaneous takes of one row's lease admit exactly one owner."""
    file_engine = create_engine_shim_file(tmp_path / "lease_race.db")
    file_session = make_sessionmaker(file_engine)
    async with file_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with file_session() as session:
            session.add(
                RefundRecord(
                    issue_id="issue_lease_race",
                    mission_id="mission_lease_race",
                    order_id="order_lease_race",
                    user_id="user_lease_race",
                    razorpay_payment_id="pay_lease_race",
                    amount=Decimal("399.00"),
                    amount_paise=39900,
                    currency="INR",
                    status="pending",
                )
            )
            await session.commit()

        async with file_session() as reader:
            row = (
                await reader.execute(
                    select(RefundRecord).where(RefundRecord.issue_id == "issue_lease_race")
                )
            ).scalar_one()
        refund_id = row.refund_id  # the lease keys on the refund id, not the issue id

        async with file_session() as first, file_session() as second:
            won_a, won_b = await asyncio.gather(
                missions_module._take_refund_submission_lease(first, refund_id, "token-a"),
                missions_module._take_refund_submission_lease(second, refund_id, "token-b"),
            )
        winners = [t for t in (won_a, won_b) if t]
        assert len(winners) == 1

        async with file_session() as reader:
            row = (
                await reader.execute(
                    select(RefundRecord).where(RefundRecord.issue_id == "issue_lease_race")
                )
            ).scalar_one()
        assert row.submission_state == "in_flight"
        assert row.submission_owner in ("token-a", "token-b")

        # While owned, no second owner is admitted.
        async with file_session() as third:
            assert (
                await missions_module._take_refund_submission_lease(
                    third, refund_id, "token-c"
                )
                is False
            )
    finally:
        await file_engine.dispose()


def create_engine_shim_file(path):
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)


# --------------------------------------------------------------------------- #
# IntegrityError arbitration: the DB constraint is the final protection
# --------------------------------------------------------------------------- #
async def test_integrity_error_arbitration_adopts_the_winner(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api,
    monkeypatch,
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        issue_id = await _allowed_issue(client, test_session, mission_id)

        # Simulate the race window: the request's first load misses the row that a
        # concurrent request has just committed. unique(issue_id) must arbitrate.
        real_load = missions_module._load_refund
        load_calls = {"n": 0}

        async def raced_load(db, issue_id_arg):
            load_calls["n"] += 1
            if load_calls["n"] == 1:
                return None
            return await real_load(db, issue_id_arg)

        monkeypatch.setattr(missions_module, "_load_refund", raced_load)

        async with test_session() as session:
            order = (
                await session.execute(
                    select(ResolveOrderRecord).where(ResolveOrderRecord.mission_id == mission_id)
                )
            ).scalar_one()
            session.add(
                RefundRecord(
                    refund_id="refund_winner0001",
                    issue_id=issue_id,
                    mission_id=mission_id,
                    order_id=order.order_id,
                    user_id="user_winner",
                    razorpay_payment_id="pay_TESTORDER1",
                    amount=Decimal("399.00"),
                    amount_paise=39900,
                    currency="INR",
                    status="pending",
                )
            )
            await session.commit()

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 200
        data = resp.json()
        assert data["refund_id"] == "refund_winner0001"  # the winner's row, not a second one
        assert data["status"] == "verified"
        assert load_calls["n"] >= 3  # top load, accounting re-load, post-rollback reload

        async with test_session() as session:
            assert (
                await session.scalar(select(func.count()).select_from(RefundRecord))
            ) == 1
        assert refund_api.calls["create"] == 1  # exactly one provider refund, period


# --------------------------------------------------------------------------- #
# The ceiling re-check also covers the stale-verified race under the lock
# --------------------------------------------------------------------------- #
async def test_assessment_gate_precedes_any_provider_or_accounting_work(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _verified_mission(
            client, install_mock_discovery, sample_products, mock_razorpay
        )
        issue_id = await _issue(client, mission_id)
        await _set_mandate_authority(test_session, mission_id, [], "0")
        await _assess(client, mission_id, issue_id)

        resp = await _refund(client, mission_id, issue_id)
        assert resp.status_code == 400
        assert refund_api.calls == {"create": 0, "fetch": 0, "list": 0}
        notes = await _audit_notes(test_session, mission_id)
        assert any("refund execution refused at the gate" in n for n in notes)
