"""
The Mission Engine — a small, fully-readable async loop that advances missions over time.

This deliberately replaces a heavyweight workflow engine (Temporal, etc.) with something that
fits on one screen and can be explained line by line: it wakes on a timer, pulls missions that
are due for another step, and advances each one through the loop described in the product spec
(Detect -> Investigate -> Plan -> Authority check -> Act -> Verify).

For a real production deployment at scale, this is the component you would eventually swap for
durable workflow orchestration — the Mission and Authority models don't change either way,
which is the point of keeping the reasoning/execution logic separate from the scheduling layer.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import logger as audit
from app.authority.engine import ProposedAction, evaluate
from app.database import async_session
from app.models.db_models import MissionRecord
from app.models.mission import AllowedAction, MissionStatus

log = logging.getLogger("resolve.mission_engine")

POLL_INTERVAL_SECONDS = 5


async def _advance_one(db: AsyncSession, record: MissionRecord) -> None:
    """
    One tick of progress for a single mission. In this reference implementation the actual
    LLM-driven planning and negotiation are invoked from app/routers/missions.py in response to
    explicit events (evidence uploaded, offer received) rather than from this loop, so this
    function focuses on what a background loop is uniquely responsible for: re-checking
    deadlines and expiring stale authority — the kind of check nothing else in the system will
    otherwise remember to do.
    """
    authority = record.mandate.get("authority", {})
    expires_at = authority.get("expires_at")
    if expires_at and record.status not in (
        MissionStatus.RESOLVED.value,
        MissionStatus.ESCALATED.value,
    ):
        from datetime import datetime

        if datetime.fromisoformat(expires_at) < datetime.utcnow():
            decision = evaluate(
                authority=_authority_from_dict(authority),
                proposal=ProposedAction(action=AllowedAction.ESCALATE_TO_HUMAN),
            )
            await audit.append(db, record.mission_id, "mission_engine", decision)
            record.status = MissionStatus.ESCALATED.value
            log.info("mission %s escalated: mandate expired", record.mission_id)


def _authority_from_dict(d: dict):
    from decimal import Decimal

    from app.models.mission import Authority

    refund_limit = d.get("refund_limit")
    return Authority(
        allowed_actions=d.get("allowed_actions", []),
        recovery_budget=Decimal(str(d.get("recovery_budget", "0"))),
        spent_so_far=Decimal(str(d.get("spent_so_far", "0"))),
        refund_limit=Decimal(str(refund_limit)) if refund_limit is not None else None,
        expires_at=d.get("expires_at"),
    )


async def run_forever() -> None:
    log.info("mission engine started — polling every %ss", POLL_INTERVAL_SECONDS)
    while True:
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(MissionRecord).where(
                        MissionRecord.status.notin_(
                            [MissionStatus.RESOLVED.value, MissionStatus.ESCALATED.value]
                        )
                    )
                )
                for record in result.scalars().all():
                    await _advance_one(db, record)
                await db.commit()
        except Exception:  # noqa: BLE001 — a background loop must never crash the process
            log.exception("mission engine tick failed")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
