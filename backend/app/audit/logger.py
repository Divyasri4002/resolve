"""
Insert-only audit logging.

This module is the only writer to `audit_log` and exposes no update() and no delete() on purpose.
Two entry points, because two kinds of thing deserve recording:

  * append()       — every Authority Engine decision, approved or blocked;
  * append_event() — a decision the Engine did not make but RESOLVE still must not forget, such
                     as an authenticated integration event that established a merchant linkage.

Both insert; neither mutates; neither may be pointed at an existing row.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.authority.engine import AuthorityDecision
from app.models.db_models import AuditLogRecord


async def append(
    db: AsyncSession,
    mission_id: str,
    actor: str,
    decision: AuthorityDecision,
    note: str | None = None,
) -> AuditLogRecord:
    """INSERT-ONLY. `note` appends deterministic execution context (e.g. provider refund
    reference and final status) to the reason — it never replaces the Engine's own reason."""
    reason = decision.reason if note is None else f"{decision.reason} | {note}"
    return await _insert(
        db,
        mission_id=mission_id,
        actor=actor,
        action=decision.action.value,
        requested_amount=(
            Decimal(decision.requested_amount) if decision.requested_amount else None
        ),
        decision=decision.decision.value,
        reason=reason,
    )


async def append_event(
    db: AsyncSession,
    *,
    mission_id: str,
    actor: str,
    action: str,
    recorded: bool,
    reason: str,
) -> AuditLogRecord:
    """
    INSERT-ONLY record of something that happened, not of something the Engine approved.

    `decision` carries 'observed' or 'refused' rather than 'approved'/'blocked' precisely so an
    auditor can tell the two kinds of row apart: no Authority Engine policy was consulted here,
    and pretending otherwise would put words in a gate that was never asked.
    """
    return await _insert(
        db,
        mission_id=mission_id,
        actor=actor,
        action=action,
        requested_amount=None,
        decision="observed" if recorded else "refused",
        reason=reason,
    )


async def _insert(
    db: AsyncSession,
    *,
    mission_id: str,
    actor: str,
    action: str,
    requested_amount: Decimal | None,
    decision: str,
    reason: str,
) -> AuditLogRecord:
    record = AuditLogRecord(
        mission_id=mission_id,
        actor=actor,
        action=action,
        requested_amount=requested_amount,
        decision=decision,
        reason=reason,
    )
    db.add(record)
    await db.flush()
    return record
