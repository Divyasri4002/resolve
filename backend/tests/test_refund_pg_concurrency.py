"""
PP Phase 2: PostgreSQL concurrency proof for refund financial integrity.

The hermetic battery (test_refund_accounting.py) proves the CONTRACT on SQLite, which
has no second writer and no advisory locks. This module proves the REAL guarantees on a
throwaway PostgreSQL database named by RESOLVE_PG_TEST_URL (never the development
database) — the same convention as test_purchase_authorization.py:

  - the per-payment advisory lock (namespace 11) actually serializes accounting;
  - the submission-lease conditional UPDATE admits exactly one concurrent owner;
  - two simultaneous executors on one issue produce exactly ONE provider refund;
  - two simultaneous executors on different issues of one payment can never push the
    cumulative refunds past what was paid — the loser is refused, never duplicated.

Skips entirely when RESOLVE_PG_TEST_URL is not set.
"""
import asyncio
import hashlib
import hmac
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models.db_models import MissionRecord, RefundRecord
from app.routers import missions as missions_module

PG_TEST_URL = os.environ.get("RESOLVE_PG_TEST_URL", "")

SHAMPOO_PROMPT = "I want to order shampoo under ₹600 for anti-hairfall."
PASSWORD = "SecurePassword123!"


@pytest.fixture
async def pg_refund_env():
    """Client factory + sessionmaker over a throwaway PostgreSQL database."""
    if not PG_TEST_URL:
        pytest.skip(
            "RESOLVE_PG_TEST_URL not set — PostgreSQL refund concurrency not exercised here"
        )
    pg_engine = create_async_engine(PG_TEST_URL, echo=False)
    pg_session = async_sessionmaker(pg_engine, expire_on_commit=False, class_=AsyncSession)
    async with pg_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with pg_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    def make_client() -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    try:
        yield make_client, pg_session
    finally:
        app.dependency_overrides.clear()
        await pg_engine.dispose()


class RefundApi:
    """In-memory Razorpay refund API with call counters and failure injection."""

    def __init__(self):
        self.test_mode = True
        self.refunds: dict[str, dict] = {}
        self.calls = {"create": 0, "fetch": 0, "list": 0}
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


@pytest.fixture
def mock_razorpay(monkeypatch):
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


async def _issue(client, mission_id) -> str:
    # Intake dedupes exact repeats (same type + description + resolution), so every
    # call must describe a genuinely different report to produce a distinct issue.
    _ISSUE_SEQ["n"] += 1
    resp = await client.post(
        f"/missions/{mission_id}/order/issues",
        json={
            "issue_type": "wrong_item",
            "description": f"Wrong item delivered; refund. (report #{_ISSUE_SEQ['n']})",
            "requested_resolution": "refund",
        },
    )
    assert resp.status_code == 200
    return resp.json()["issue_id"]


async def _set_mandate_authority(pg_session, mission_id, actions, budget):
    async with pg_session() as session:
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


async def _allowed_issue(client, pg_session, mission_id) -> str:
    await _set_mandate_authority(pg_session, mission_id, ["request_refund"], "1000")
    issue_id = await _issue(client, mission_id)
    assessment = await _assess(client, mission_id, issue_id)
    assert assessment["proposed_action"] == "request_refund"
    assert assessment["authority_decision"] == "allow"
    return issue_id


# --------------------------------------------------------------------------- #
# The advisory lock genuinely serializes per-payment accounting
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_payment_accounting_lock_blocks_until_holder_releases(pg_refund_env):
    make_client, pg_session = pg_refund_env

    async with pg_session() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:pid))"),
            {"ns": missions_module._REFUND_ACCOUNTING_LOCK_NAMESPACE, "pid": "pay_BLOCK"},
        )
        # A second connection cannot enter the same payment's accounting window.
        blocker_engine = create_async_engine(PG_TEST_URL, echo=False, poolclass=NullPool)
        try:
            blocked_session = async_sessionmaker(
                blocker_engine, expire_on_commit=False, class_=AsyncSession
            )()
            try:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        missions_module._lock_refund_accounting(blocked_session, "pay_BLOCK"),
                        timeout=1.0,
                    )
            finally:
                await blocked_session.close()
        finally:
            await blocker_engine.dispose()

        await holder.rollback()  # releases the lock

    async with pg_session() as after:
        await asyncio.wait_for(
            missions_module._lock_refund_accounting(after, "pay_BLOCK"), timeout=2.0
        )


# --------------------------------------------------------------------------- #
# The lease conditional UPDATE admits exactly one owner under real concurrency
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_lease_race_on_real_postgres_admits_one_owner(pg_refund_env):
    _, pg_session = pg_refund_env
    async with pg_session() as session:
        session.add(
            RefundRecord(
                issue_id="issue_pg_lease_race",
                mission_id="mission_pg_lease_race",
                order_id="order_pg_lease_race",
                user_id="user_pg_lease_race",
                razorpay_payment_id="pay_pg_lease_race",
                amount=399.00,
                amount_paise=39900,
                currency="INR",
                status="pending",
            )
        )
        await session.commit()

    async with pg_session() as reader:
        row = (
            await reader.execute(
                select(RefundRecord).where(RefundRecord.issue_id == "issue_pg_lease_race")
            )
        ).scalar_one()
    refund_id = row.refund_id  # the lease keys on the refund id, not the issue id

    tokens = ["token-a", "token-b"]
    async with pg_session() as first, pg_session() as second:
        won_a, won_b = await asyncio.gather(
            missions_module._take_refund_submission_lease(first, refund_id, "token-a"),
            missions_module._take_refund_submission_lease(second, refund_id, "token-b"),
        )
    winners = [t for t, w in zip(tokens, (won_a, won_b)) if w]
    assert len(winners) == 1

    async with pg_session() as reader:
        row = (
            await reader.execute(
                select(RefundRecord).where(RefundRecord.issue_id == "issue_pg_lease_race")
            )
        ).scalar_one()
    assert row.submission_state == "in_flight"
    assert row.submission_owner == winners[0]


# --------------------------------------------------------------------------- #
# Two simultaneous executors on ONE issue: exactly one provider refund, ever
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_same_issue_concurrent_refunds_create_exactly_one(
    pg_refund_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, pg_session = pg_refund_env
    async with make_client() as setup_client:
        mission_id = await _verified_mission(
            setup_client, install_mock_discovery, sample_products, mock_razorpay
        )
        issue_id = await _allowed_issue(setup_client, pg_session, mission_id)

    async with make_client() as client_a, make_client() as client_b:
        await _login(client_a, "olga@shop.com")
        await _login(client_b, "olga@shop.com")
        barrier = asyncio.Barrier(2)

        async def fire(racer: AsyncClient):
            await barrier.wait()
            return await racer.post(f"/missions/{mission_id}/order/issues/{issue_id}/refund")

        resp_a, resp_b = await asyncio.gather(fire(client_a), fire(client_b))

    statuses = {resp_a.status_code, resp_b.status_code}
    assert statuses <= {200, 409}
    assert 200 in statuses

    # Exactly one row and exactly one provider refund — the loser never duplicated.
    async with pg_session() as reader:
        rows = (
            await reader.execute(
                select(RefundRecord).where(RefundRecord.issue_id == issue_id)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "verified"
        assert rows[0].submission_state == "idle"
        assert rows[0].submission_owner is None
    assert refund_api.calls["create"] == 1
    assert len(refund_api.refunds) == 1

    for resp in (resp_a, resp_b):
        if resp.status_code == 200:
            data = resp.json()
            assert data["refund_id"] == rows[0].refund_id
            assert data["status"] == "verified"
        else:
            assert "in flight" in resp.json()["detail"] or "retry" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Two simultaneous executors on DIFFERENT issues of ONE payment: the cumulative
# ceiling holds under real concurrency — one settles, the other is refused
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_different_issues_concurrent_never_exceed_the_payment(
    pg_refund_env, install_mock_discovery, sample_products, mock_razorpay, refund_api
):
    make_client, pg_session = pg_refund_env
    async with make_client() as setup_client:
        mission_id = await _verified_mission(
            setup_client, install_mock_discovery, sample_products, mock_razorpay
        )
        issue_a = await _allowed_issue(setup_client, pg_session, mission_id)
        issue_b = await _allowed_issue(setup_client, pg_session, mission_id)

    async with make_client() as client_a, make_client() as client_b:
        await _login(client_a, "olga@shop.com")
        await _login(client_b, "olga@shop.com")
        barrier = asyncio.Barrier(2)

        async def fire(racer: AsyncClient, issue: str):
            await barrier.wait()
            return await racer.post(f"/missions/{mission_id}/order/issues/{issue}/refund")

        resp_a, resp_b = await asyncio.gather(
            fire(client_a, issue_a), fire(client_b, issue_b)
        )

    codes = sorted((resp_a.status_code, resp_b.status_code))
    # One settles; the other is refused on the cumulative ceiling (or, if it lost the
    # lease race it never even reached accounting — either way NOT a second settlement).
    assert codes in ([200, 400], [200, 409])
    assert refund_api.calls["create"] == 1

    async with pg_session() as reader:
        verified = (
            await reader.execute(
                select(func.count())
                .select_from(RefundRecord)
                .where(RefundRecord.status == "verified")
            )
        ).scalar_one()
        total = (
            await reader.scalar(
                select(func.coalesce(func.sum(RefundRecord.amount_paise), 0)).where(
                    RefundRecord.status == "verified"
                )
            )
        )
    assert verified == 1
    assert total <= 39900  # never more than was paid

    winner = resp_a if resp_a.status_code == 200 else resp_b
    assert winner.json()["status"] == "verified"
    loser = resp_b if resp_a.status_code == 200 else resp_a
    if loser.status_code == 400:
        assert "Cumulative refunds can never exceed" in loser.json()["detail"]
