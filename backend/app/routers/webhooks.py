"""
Razorpay webhook receiver.

A mission is NEVER marked resolved because the initiating payment API call returned 200. It is
marked resolved only once the matching webhook arrives here, its signature is verified, and the
amount is reconciled against what the mission's audit log says was approved.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.database import get_db
from app.models.db_models import AuditLogRecord, MissionRecord
from app.payments.razorpay_client import verify_webhook_signature

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()

    if not verify_webhook_signature(body, x_razorpay_signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = await request.json()
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {}) or payload.get(
        "payload", {}
    ).get("payout", {}).get("entity", {})

    mission_id = (entity.get("notes") or {}).get("mission_id")
    if not mission_id:
        # Not every Razorpay event is mission-scoped — acknowledge and ignore.
        return {"status": "ignored"}

    record = await db.get(MissionRecord, mission_id)
    if record is None:
        return {"status": "ignored", "reason": "unknown mission"}

    # Reconcile: confirm the confirmed amount matches what the audit log approved for this
    # mission before treating the payment as genuinely settled.
    result = await db.execute(
        select(AuditLogRecord)
        .where(AuditLogRecord.mission_id == mission_id, AuditLogRecord.decision == "approved")
        .order_by(AuditLogRecord.created_at.desc())
    )
    last_approval = result.scalars().first()
    confirmed_amount = (entity.get("amount") or 0) / 100

    reconciled = bool(
        last_approval
        and last_approval.requested_amount is not None
        and abs(float(last_approval.requested_amount) - confirmed_amount) < 0.01
    )

    return {"status": "processed", "reconciled": reconciled}
