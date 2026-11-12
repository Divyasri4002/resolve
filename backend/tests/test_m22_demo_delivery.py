"""
M22 Phase 3 — the persisted demo-environment delivery (spec §1), proven over the real HTTP routes.

A confirmed order progresses to `delivered` when the customer reloads it, stays delivered on every
reload after that, never advances an unverified/unpaid order, never simulates over an order with a
merchant-purchase authorization claim, and never fires on a machine read. The delivery is RESOLVE's
own demo behavior, disclosed by delivery_mode=demo_simulated — distinguished forever from a
carrier- or merchant-confirmed delivery — and the delivery facts survive an issue being reported
afterwards.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.models.db_models import (
    AuditLogRecord,
    MissionRecord,
    PurchaseAuthorizationRecord,
    ResolveOrderRecord,
)
from app.routers import missions as missions_router

from tests.test_core3a_golden_lifecycle import (
    _audit,
    _confirmed_order,
    _report,
    _rows,
    _to_paid_mission,
)
from tests.test_refund_execution import mock_razorpay, restart_env  # noqa: F401

DAMAGED = {
    "issue_type": "damaged",
    "description": "The bottle arrived cracked.",
    "requested_resolution": "refund",
}


def _flag_on_gated(monkeypatch, gate: float) -> None:
    monkeypatch.setattr(get_settings(), "shopping_demo_delivery_enabled", True)
    monkeypatch.setattr(missions_router, "_DEMO_DELIVERY_STAGE_SECONDS", gate)


def _flag_on(monkeypatch) -> None:
    # Sprint M2: gate 0 collapses the staged ladder into the first fetch, keeping these
    # single-fetch assertions exact. The staged timing itself is proven in test_S* below.
    _flag_on_gated(monkeypatch, 0)


async def _order_row(test_session, mission_id) -> ResolveOrderRecord:
    async with test_session() as session:
        row = await session.scalar(
            select(ResolveOrderRecord).where(ResolveOrderRecord.mission_id == mission_id)
        )
    assert row is not None
    return row


async def _mission_row(test_session, mission_id) -> MissionRecord:
    async with test_session() as session:
        row = await session.get(MissionRecord, mission_id)
    assert row is not None
    return row


async def _paid_confirmed_mission(client, install_mock_discovery, sample_products, email) -> str:
    mission_id, _chosen = await _to_paid_mission(
        client, install_mock_discovery, sample_products, email
    )
    await _confirmed_order(client, mission_id)
    return mission_id


# --------------------------------------------------------------------------- #
# A. Reload persists the demo delivery exactly once, with its evidence.
# --------------------------------------------------------------------------- #
async def test_A_customer_reload_persists_the_demo_delivery_exactly_once(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    _flag_on(monkeypatch)
    async with client:
        mission_id = await _paid_confirmed_mission(
            client, install_mock_discovery, sample_products, "deliverA@m22p3.test"
        )

        first = await client.get(f"/missions/{mission_id}/order")
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["status"] == "delivered"
        assert body["delivery_mode"] == "demo_simulated"
        assert body["delivered_at"]
        delivered_at = body["delivered_at"]

        # The mission mirrors the order, and the audit trail records one system transition.
        mission = await _mission_row(test_session, mission_id)
        assert mission.status == "delivered"
        assert mission.shopping_intent["state"] == "delivered"
        grouped = await _audit(test_session, mission_id)
        assert len(grouped.get("order_delivered", [])) == 1
        assert grouped["order_delivered"][0]["actor"] == "system"

        # Two more reloads: delivered stays delivered, the timestamp never moves, and no
        # second transition is recorded.
        for _ in range(2):
            again = await client.get(f"/missions/{mission_id}/order")
            assert again.status_code == 200, again.text
            assert again.json()["status"] == "delivered"
            assert again.json()["delivered_at"] == delivered_at
            assert again.json()["delivery_mode"] == "demo_simulated"
        grouped = await _audit(test_session, mission_id)
        assert len(grouped.get("order_delivered", [])) == 1

        # The persisted row itself carries the facts.
        order = await _order_row(test_session, mission_id)
        assert order.status == "delivered"
        assert order.delivery_mode == "demo_simulated"
        assert order.delivered_at is not None


# --------------------------------------------------------------------------- #
# B. The deployment switch is the only difference between reload behaviors.
# --------------------------------------------------------------------------- #
async def test_B_flag_off_keeps_the_order_confirmed_on_reload(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    async with client:
        mission_id = await _paid_confirmed_mission(
            client, install_mock_discovery, sample_products, "deliverB@m22p3.test"
        )

        # The demo .env ships the switch ON for the live demo, so the OFF leg pins it
        # explicitly — the test proves the switch, not the ambient environment.
        monkeypatch.setattr(get_settings(), "shopping_demo_delivery_enabled", False)

        # Default posture (switch off): a reload changes nothing.
        first = await client.get(f"/missions/{mission_id}/order")
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "order_confirmed"
        assert first.json()["delivered_at"] is None
        assert first.json()["delivery_mode"] is None
        grouped = await _audit(test_session, mission_id)
        assert "order_delivered" not in grouped

        # With the switch on, the very same reload persists the transition.
        _flag_on(monkeypatch)
        second = await client.get(f"/missions/{mission_id}/order")
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "delivered"
        assert second.json()["delivery_mode"] == "demo_simulated"


# --------------------------------------------------------------------------- #
# C. An unconfirmed order_created never delivers, whatever the reload count.
# --------------------------------------------------------------------------- #
async def test_C_order_created_never_delivers(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    _flag_on(monkeypatch)
    async with client:
        mission_id, _chosen = await _to_paid_mission(
            client, install_mock_discovery, sample_products, "deliverC@m22p3.test"
        )
        created = await client.post(f"/missions/{mission_id}/order", json={})
        assert created.status_code == 200, created.text
        assert created.json()["status"] == "order_created"

        for _ in range(2):
            reloaded = await client.get(f"/missions/{mission_id}/order")
            assert reloaded.status_code == 200, reloaded.text
            assert reloaded.json()["status"] == "order_created"
            assert reloaded.json()["delivered_at"] is None
        grouped = await _audit(test_session, mission_id)
        assert "order_delivered" not in grouped


# --------------------------------------------------------------------------- #
# D. An unverified payment never changes status — the pairing proof fails.
# --------------------------------------------------------------------------- #
async def test_D_unverified_payment_never_delivers(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    _flag_on(monkeypatch)
    async with client:
        mission_id = await _paid_confirmed_mission(
            client, install_mock_discovery, sample_products, "deliverD@m22p3.test"
        )
        # Directly unpersist the payment's verified status: the order row still says
        # order_confirmed, but the payment this reload would lean on no longer proves it.
        # deepcopy, not dict(): a shallow copy shares the payment sub-dict with the
        # committed state, so the in-place mutation reads through both references and
        # SQLAlchemy skips the write as a no-change assignment.
        async with test_session() as session:
            mission = await session.get(MissionRecord, mission_id)
            intent = copy.deepcopy(mission.shopping_intent)
            intent["payment"]["status"] = "pending"
            mission.shopping_intent = intent
            await session.commit()

        reloaded = await client.get(f"/missions/{mission_id}/order")
        assert reloaded.status_code == 200, reloaded.text
        assert reloaded.json()["status"] == "order_confirmed"
        assert reloaded.json()["delivered_at"] is None
        grouped = await _audit(test_session, mission_id)
        assert "order_delivered" not in grouped


# --------------------------------------------------------------------------- #
# E. A merchant-purchase authorization claim blocks the simulation outright.
# --------------------------------------------------------------------------- #
async def test_E_purchase_authorization_claim_blocks_demo_delivery(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    _flag_on(monkeypatch)
    async with client:
        mission_id = await _paid_confirmed_mission(
            client, install_mock_discovery, sample_products, "deliverE@m22p3.test"
        )
        order = await _order_row(test_session, mission_id)
        async with test_session() as session:
            session.add(
                PurchaseAuthorizationRecord(
                    order_id=order.order_id,
                    mission_id=mission_id,
                    user_id=order.user_id,
                    authority_decision="approved",
                    decision_reason="test claim — the merchant channel owns this order's outcome",
                    status="authorized",
                )
            )
            await session.commit()

        reloaded = await client.get(f"/missions/{mission_id}/order")
        assert reloaded.status_code == 200, reloaded.text
        assert reloaded.json()["status"] == "order_confirmed"
        assert reloaded.json()["delivered_at"] is None
        grouped = await _audit(test_session, mission_id)
        assert "order_delivered" not in grouped


# --------------------------------------------------------------------------- #
# F. A delivered order still reports issues, and keeps its delivery facts.
# --------------------------------------------------------------------------- #
async def test_F_delivered_order_reports_an_issue_and_keeps_its_delivery_facts(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    _flag_on(monkeypatch)
    async with client:
        mission_id = await _paid_confirmed_mission(
            client, install_mock_discovery, sample_products, "deliverF@m22p3.test"
        )
        delivered = await client.get(f"/missions/{mission_id}/order")
        assert delivered.json()["status"] == "delivered"

        reported = await _report(client, mission_id, **DAMAGED)
        assert reported.status_code == 200, reported.text

        after = await client.get(f"/missions/{mission_id}/order")
        assert after.status_code == 200, after.text
        # The lifecycle moved to post-purchase context; the delivery facts survive it.
        assert after.json()["status"] == "awaiting_post_purchase_context"
        assert after.json()["delivery_mode"] == "demo_simulated"
        assert after.json()["delivered_at"]
        order = await _order_row(test_session, mission_id)
        assert order.status == "awaiting_post_purchase_context"
        assert order.delivery_mode == "demo_simulated"
        assert order.delivered_at is not None


# --------------------------------------------------------------------------- #
# G. The machine read never mutates: the service-principal GET is read-only.
# --------------------------------------------------------------------------- #
async def test_G_machine_read_never_mutates_the_order(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    _flag_on(monkeypatch)
    monkeypatch.setattr(get_settings(), "service_runner_token", "svc-token-m22p3")
    async with client:
        mission_id = await _paid_confirmed_mission(
            client, install_mock_discovery, sample_products, "deliverG@m22p3.test"
        )

        # This mock-discovered order has no Shopify catalog identity, so the machine read's
        # own identity contract answers the same 404 it always has — and whatever it answers,
        # it must leave the row exactly where it was.
        machine = await client.get(
            f"/missions/{mission_id}/order",
            headers={"X-Resolve-Service-Token": "svc-token-m22p3"},
        )
        assert machine.status_code == 404, machine.text
        grouped = await _audit(test_session, mission_id)
        assert "order_delivered" not in grouped
        order = await _order_row(test_session, mission_id)
        assert order.status == "order_confirmed"
        assert order.delivered_at is None

        # The customer's own reload still transitions — proving the machine read skipped it.
        human = await client.get(f"/missions/{mission_id}/order")
        assert human.status_code == 200, human.text
        assert human.json()["status"] == "delivered"


# --------------------------------------------------------------------------- #
# H. A delivered order is not purchase-eligible: the demo path and the merchant
#    path never meet.
# --------------------------------------------------------------------------- #
async def test_H_delivered_order_is_not_purchase_eligible(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    make_client, test_session = restart_env
    client = make_client()
    _flag_on(monkeypatch)
    async with client:
        mission_id = await _paid_confirmed_mission(
            client, install_mock_discovery, sample_products, "deliverH@m22p3.test"
        )
        delivered = await client.get(f"/missions/{mission_id}/order")
        assert delivered.json()["status"] == "delivered"

        purchase = await client.post(f"/missions/{mission_id}/order/purchase")
        assert purchase.status_code == 403, purchase.text
        # No authorization claim was left behind by the refusal.
        order = await _order_row(test_session, mission_id)
        grouped = await _audit(test_session, mission_id)
        assert "order_delivered" in grouped
        async with test_session() as session:
            claims = list(
                (
                    await session.execute(
                        select(PurchaseAuthorizationRecord).where(
                            PurchaseAuthorizationRecord.order_id == order.order_id
                        )
                    )
                ).scalars()
            )
        assert claims == []


# --------------------------------------------------------------------------- #
# S1. The staged ladder advances one gate window per fetch, deterministically.
# --------------------------------------------------------------------------- #
async def test_S1_staged_ladder_advances_one_window_per_fetch_and_catches_up(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    """PACKED -> SHIPPED -> OUT FOR DELIVERY -> DELIVERED, one stage at a time.

    A one-hour stage gate makes every fetch deterministic: no wall-clock sleep decides the
    outcome — the test rewinds the persisted stage timestamp instead, which is precisely the
    catch-up window the ladder exists for. Each overdue stage is persisted with its scheduled
    timestamp (previous stage + one gate), so a long gap is caught up, never skipped."""
    make_client, test_session = restart_env
    client = make_client()
    _flag_on_gated(monkeypatch, 3600.0)
    async with client:
        mission_id = await _paid_confirmed_mission(
            client, install_mock_discovery, sample_products, "deliverS1@m22p3.test"
        )

        # First fetch past confirmation: the simulation starts at 'packed'; the order itself
        # has not delivered and the mission has not moved.
        first = await client.get(f"/missions/{mission_id}/order")
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["status"] == "order_confirmed"
        assert body["delivery_stage"] == "packed"
        assert body["delivered_at"] is None and body["delivery_mode"] is None
        mission = await _mission_row(test_session, mission_id)
        assert mission.status == "order_confirmed"
        grouped = await _audit(test_session, mission_id)
        assert len(grouped.get("order_delivery_stage", [])) == 1
        assert grouped["order_delivery_stage"][0]["actor"] == "system"
        assert "order_delivered" not in grouped

        async def _rewind_stage(seconds: float):
            async with test_session() as session:
                order = await session.scalar(
                    select(ResolveOrderRecord).where(
                        ResolveOrderRecord.mission_id == mission_id
                    )
                )
                scheduled = order.delivery_stage_at - timedelta(seconds=seconds)
                order.delivery_stage_at = scheduled
                await session.commit()
            return scheduled

        # One gate overdue: exactly one stage advances, with its scheduled timestamp.
        shipped_due = await _rewind_stage(3600.0)
        second = await client.get(f"/missions/{mission_id}/order")
        body = second.json()
        assert body["status"] == "order_confirmed"
        assert body["delivery_stage"] == "shipped"
        assert body["delivered_at"] is None
        assert body["delivery_stage_at"] == (shipped_due + timedelta(seconds=3600)).isoformat()

        out_due = await _rewind_stage(3600.0)
        third = await client.get(f"/missions/{mission_id}/order")
        body = third.json()
        assert body["status"] == "order_confirmed"
        assert body["delivery_stage"] == "out_for_delivery"
        assert body["delivered_at"] is None

        delivered_due = await _rewind_stage(3600.0)
        final = await client.get(f"/missions/{mission_id}/order")
        body = final.json()
        assert body["status"] == "delivered"
        assert body["delivery_stage"] == "delivered"
        assert body["delivery_mode"] == "demo_simulated"
        assert body["delivered_at"] == (delivered_due + timedelta(seconds=3600)).isoformat()

        # The ladder left complete evidence: three simulated stages plus the delivery, every
        # one a system actor, none skipped.
        mission = await _mission_row(test_session, mission_id)
        assert mission.status == "delivered"
        assert mission.shopping_intent["state"] == "delivered"
        grouped = await _audit(test_session, mission_id)
        assert len(grouped.get("order_delivery_stage", [])) == 3
        assert len(grouped.get("order_delivered", [])) == 1
        assert grouped["order_delivered"][0]["actor"] == "system"


# --------------------------------------------------------------------------- #
# S2. The real elapsed-time path: one reload after the ladder window delivers,
#     and the catch-up persists every overdue stage.
# --------------------------------------------------------------------------- #
async def test_S2_wall_clock_cadence_delivers_with_every_stage_persisted(
    restart_env, install_mock_discovery, sample_products, mock_razorpay, monkeypatch
):
    """The production gate is 0.5s per stage; at 0.05s the ladder completes far inside the
    demo budget. One reload after the whole window finds the order delivered with all three
    simulated stages persisted — the catch-up never skips a stage silently."""
    make_client, test_session = restart_env
    client = make_client()
    _flag_on_gated(monkeypatch, 0.05)
    async with client:
        mission_id = await _paid_confirmed_mission(
            client, install_mock_discovery, sample_products, "deliverS2@m22p3.test"
        )
        started = await client.get(f"/missions/{mission_id}/order")
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "order_confirmed"
        assert started.json()["delivery_stage"] == "packed"

        # Three stages x 0.05s gate = 0.15s; the sleep is deliberately far past the window.
        await asyncio.sleep(0.3)

        after = await client.get(f"/missions/{mission_id}/order")
        body = after.json()
        assert body["status"] == "delivered"
        assert body["delivery_stage"] == "delivered"
        assert body["delivery_mode"] == "demo_simulated"
        assert body["delivered_at"]

        grouped = await _audit(test_session, mission_id)
        stages = grouped.get("order_delivery_stage", [])
        assert len(stages) == 3
        assert all(entry["actor"] == "system" for entry in stages)
        reasons = [entry["reason"] for entry in stages]
        for stage in ("packed", "shipped", "out_for_delivery"):
            assert any(f"stage '{stage}' simulated" in reason for reason in reasons)
        assert len(grouped.get("order_delivered", [])) == 1
