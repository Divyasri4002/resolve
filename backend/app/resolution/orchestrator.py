"""
The autonomous resolution orchestrator (Task 13).

One place that takes a persisted post-purchase issue, asks the existing Authority Engine what it
allows RIGHT NOW, and then hands the work to the existing controlled executor for that action.
This module owns no money, no provider call and no permission logic of its own:

  * it never touches Razorpay, the replacement provider or the return provider directly — only
    `run_refund_executor` / `run_replacement_executor` / `run_return_executor` do that, and those
    are the very same functions the individual execution endpoints call;
  * it never trusts the stored Task 10 decision alone: `evaluate()` is re-run against the
    canonical persisted mandate before anything is attempted;
  * it never calls the reasoning model: the assessment made in Task 10 is read from storage, and
    if it does not exist the caller is told so instead of a silent new assessment;
  * it never re-decides between actions: if the chosen action cannot run because another
    resolution already exists, the executor's deterministic conflict result is returned as-is.

`app.routers.missions` is imported lazily inside the functions because the router imports this
module — the executors live there, and duplicating their guards here would create exactly the
second authorization path the architecture forbids.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from starlette.responses import JSONResponse

from app.audit import logger as audit
from app.authority.engine import (
    AllowedAction,
    AuthorityDecision,
    Decision,
    ProposedAction,
    evaluate,
)
from app.models.db_models import (
    OrderIssueRecord,
    ResolutionRecord,
    ResolveOrderRecord,
    UserRecord,
)
from app.models.mission import Authority, ResolutionState

logger = logging.getLogger(__name__)

EXECUTING_ACTIONS: dict[AllowedAction, str] = {
    AllowedAction.REQUEST_REFUND: "refund",
    AllowedAction.REQUEST_REPLACEMENT: "replacement",
    AllowedAction.REQUEST_RETURN: "return",
}

NON_EXECUTING_ACTIONS: dict[AllowedAction, ResolutionState] = {
    AllowedAction.REQUEST_MORE_INFORMATION: ResolutionState.MORE_INFORMATION_REQUIRED,
    AllowedAction.ESCALATE_TO_HUMAN: ResolutionState.ESCALATED,
    AllowedAction.NO_ACTION: ResolutionState.NO_ACTION,
}

# A resolution already in one of these states is finished: a repeat /resolve returns what is
# persisted and does not touch an executor (and therefore no provider) at all.
TERMINAL_STATES = {
    ResolutionState.VERIFIED.value,
    ResolutionState.CONFIRMED.value,
    ResolutionState.ESCALATED.value,
    ResolutionState.MORE_INFORMATION_REQUIRED.value,
    ResolutionState.NO_ACTION.value,
}

EXECUTOR_BY_STATUS: dict[str, dict[str, str]] = {
    "refund": {
        "verified": ResolutionState.VERIFIED.value,
        "pending": ResolutionState.PENDING.value,
        "failed": ResolutionState.FAILED.value,
    },
    "replacement": {
        "confirmed": ResolutionState.CONFIRMED.value,
        "requested": ResolutionState.REQUESTED.value,
        "pending": ResolutionState.PENDING.value,
        "failed": ResolutionState.FAILED.value,
    },
    "return": {
        "confirmed": ResolutionState.CONFIRMED.value,
        "requested": ResolutionState.REQUESTED.value,
        "pending": ResolutionState.PENDING.value,
        "failed": ResolutionState.FAILED.value,
    },
}


def _resolution_note(state: str) -> str:
    """What a state does and does not mean. 'allowed' is never presented as 'done'."""
    return {
        ResolutionState.DENIED.value: (
            "The Authority Engine refused this action right now. Nothing was submitted to any "
            "provider — no refund, replacement, or return exists."
        ),
        ResolutionState.PENDING.value: (
            "RESOLVE recorded the intent through the controlled executor, but no provider has "
            "accepted it (or no provider is connected). This is NOT a completed resolution."
        ),
        ResolutionState.REQUESTED.value: (
            "A connected provider accepted the request. That is acceptance only — not shipped, "
            "not delivered, not completed."
        ),
        ResolutionState.VERIFIED.value: (
            "The refund provider itself reported the refund processed. This is a Razorpay "
            "TEST-mode refund, not merchant fulfillment."
        ),
        ResolutionState.CONFIRMED.value: (
            "The provider itself confirmed the outcome. RESOLVE still makes no claim about "
            "shipping or delivery."
        ),
        ResolutionState.FAILED.value: (
            "The provider refused or the submission failed. No resolution has been delivered; "
            "retrying reuses the same controlled record."
        ),
        ResolutionState.ESCALATED.value: (
            "This issue is routed for human review. No external ticket system was contacted and "
            "no ticket number exists."
        ),
        ResolutionState.MORE_INFORMATION_REQUIRED.value: (
            "More information is needed before any resolution can be executed. Nothing external "
            "was contacted."
        ),
        ResolutionState.NO_ACTION.value: (
            "The resolution is deliberately no action. Nothing was executed."
        ),
    }.get(state, "Resolution state recorded by the orchestrator.")


def resolution_response(
    row: ResolutionRecord,
    issue: OrderIssueRecord | None = None,
    execution: dict[str, Any] | None = None,
    message: str | None = None,
    already_existed: bool = False,
    authority_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "resolution_id": row.resolution_id,
        "mission_id": row.mission_id,
        "issue_id": row.issue_id,
        "order_id": row.order_id,
        "user_id": row.user_id,
        "action": row.action,
        "authority_decision": row.authority_decision,
        # The gate's own sentence for THIS call, or None when no gate ran (an early return on an
        # already-settled issue, or a manual-review pause). The agent loop reports this as its
        # authority decision — which is exactly why it reports nothing when nothing was evaluated.
        "authority_reason": authority_reason,
        "executor": row.executor,
        "executor_record_id": row.executor_record_id,
        "resolution_state": row.resolution_state,
        "provider": row.provider,
        "provider_reference": row.provider_reference,
        "provider_attempted": row.provider_attempted,
        "already_existed": already_existed,
        "issue_status": issue.status if issue is not None else None,
        "next_state": issue.status if issue is not None else row.resolution_state,
        "message": message or row.message,
        "agent_message": message or row.message,
        "resolution_note": _resolution_note(row.resolution_state),
        "execution": execution,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def load_resolution(db, issue_id: str) -> ResolutionRecord | None:
    result = await db.execute(
        select(ResolutionRecord).where(ResolutionRecord.issue_id == issue_id)
    )
    return result.scalar_one_or_none()


def _status_code_for(state: str) -> int:
    if state == ResolutionState.DENIED.value:
        return 400
    if state == ResolutionState.PENDING.value:
        return 503
    if state == ResolutionState.FAILED.value:
        return 502
    return 200


def _provider_attempted(executor_name: str, payload: dict[str, Any], state: str) -> bool:
    """
    Was anything actually put in front of a provider? Reported honestly for the audit; a claim
    that never left the database, and a 'none' provider refusing the call, both count as no.
    """
    if executor_name == "refund":
        return state != ResolutionState.PENDING.value or bool(payload.get("razorpay_refund_id"))
    return payload.get("provider") not in (None, "none") and state in {
        ResolutionState.REQUESTED.value,
        ResolutionState.CONFIRMED.value,
        ResolutionState.FAILED.value,
    }


async def resolve_issue(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None,
    db,
) -> dict | JSONResponse:
    """
    Run the autonomous resolution flow for one issue. No request body exists: mission, order,
    issue, assessment and mandate are all loaded from the database, and the action comes from the
    persisted, validated Task 10 proposal — never from the client and never from the model at call
    time.
    """
    from app.routers import missions as m  # late import: the router owns the executors

    record = await m._load_mission_for_shopping_action(mission_id, current_user, db)
    order = await m._load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    issue = await m._load_issue(db, mission_id, issue_id)

    # Automated-decision opt-out (Task 14B.5A): if the customer requested manual review, the
    # autonomous path stops here — no executor, no provider, no reasoning model. This is a
    # suppression gate, not a permission decision: the Authority Engine is deliberately NOT
    # consulted (nothing is being proposed for execution) and its rules are unchanged. The
    # existing autonomous flow is completely untouched for issues without this flag.
    if getattr(issue, "manual_review_requested", False):
        return await _manual_review_response(m, db, record, order, issue, mission_id, issue_id)

    assessment = await m._load_assessment(db, issue_id)
    if (
        assessment is None
        or assessment.issue_id != issue_id
        or assessment.mission_id != mission_id
    ):
        # Deliberately NOT assessed here (Task 9 of this spec): the caller must run /assess first.
        raise HTTPException(
            status_code=400,
            detail=(
                "The issue must be assessed (Task 10) before it can be resolved — /resolve never "
                "calls the resolution model itself"
            ),
        )

    try:
        action = AllowedAction(assessment.proposed_action)
    except ValueError as exc:  # untrusted stored value: fail closed, never guess
        raise HTTPException(
            status_code=400,
            detail=f"Stored proposal '{assessment.proposed_action}' is not a valid action",
        ) from exc

    existing = await load_resolution(db, issue_id)
    if existing is not None and existing.resolution_state in TERMINAL_STATES:
        return resolution_response(
            existing,
            issue=issue,
            message=(
                f"Issue {issue_id} is already resolved as {existing.resolution_state} "
                f"({existing.resolution_id}) — no new execution was attempted."
            ),
            already_existed=True,
        )

    # Authority is re-evaluated from canonical persisted state on EVERY resolve call.
    authority_data = (record.mandate or {}).get("authority")
    authority = Authority(**(authority_data or {"allowed_actions": [], "recovery_budget": "0"}))
    amount = None
    if action is AllowedAction.REQUEST_REFUND:
        intent_data, _selected = m._require_selected_product(record)
        payment_data = intent_data.get("payment") or {}
        if payment_data.get("status") == "verified":
            amount, _paise, _currency = m._canonical_refund_amount(payment_data, order)
    gate = evaluate(authority, ProposedAction(action=action, amount=amount))
    decision = "allow" if gate.approved else "deny"

    if action in NON_EXECUTING_ACTIONS:
        state = NON_EXECUTING_ACTIONS[action].value
        row = await _persist(
            db,
            existing,
            issue=issue,
            order=order,
            user_id=record.user_id,
            action=action.value,
            decision=decision,
            executor="none",
            state=state,
            provider=None,
            provider_reference=None,
            provider_attempted=False,
        )
        await audit.append(
            db, mission_id, "buyer_agent", gate,
            note=(
                f"resolve {issue_id}: action={action.value} decision={decision} "
                f"executor=none state={state} provider_attempted=False"
            ),
        )
        await db.commit()
        return resolution_response(
            row, issue=issue, message=row.message, authority_reason=gate.reason
        )

    if not gate.approved:
        row = await _persist(
            db,
            existing,
            issue=issue,
            order=order,
            user_id=record.user_id,
            action=action.value,
            decision="deny",
            executor=EXECUTING_ACTIONS[action],
            state=ResolutionState.DENIED.value,
            provider=None,
            provider_reference=None,
            provider_attempted=False,
        )
        await audit.append(
            db, mission_id, "buyer_agent", gate,
            note=(
                f"resolve {issue_id}: action={action.value} decision=deny "
                f"executor={row.executor} state={row.resolution_state} "
                f"provider_attempted=False — refused before any provider contact"
            ),
        )
        await db.commit()
        detail = (
            f"The Authority Engine refused {action.value} for issue {issue_id}: {gate.reason}"
        )
        return JSONResponse(
            status_code=400,
            content={
                **resolution_response(
                    row, issue=issue, message=detail, authority_reason=gate.reason
                ),
                "detail": detail,
            },
        )

    # Allowed: hand the work to the existing controlled executor — the same function the
    # individual endpoint calls, with all of its guards and idempotency intact.
    executor_name = EXECUTING_ACTIONS[action]
    runner = getattr(m, f"run_{executor_name}_executor")
    try:
        result = await runner(mission_id, issue_id, current_user, db)
    except HTTPException as exc:
        # Conflicts and executor-level refusals are deterministic results, not invitations to
        # pick a different action. Record the denial and propagate the same status/detail.
        row = await _persist(
            db,
            existing,
            issue=issue,
            order=order,
            user_id=record.user_id,
            action=action.value,
            decision=decision,
            executor=executor_name,
            state=ResolutionState.DENIED.value,
            provider=None,
            provider_reference=None,
            provider_attempted=False,
        )
        await audit.append(
            db, mission_id, "buyer_agent", gate,
            note=(
                f"resolve {issue_id}: action={action.value} executor={executor_name} "
                f"state={ResolutionState.DENIED.value} provider_attempted=False "
                f"executor_status={exc.status_code} reason={str(exc.detail)[:300]}"
            ),
        )
        await db.commit()
        # Re-read the issue: an executor may have advanced it before refusing downstream.
        await db.refresh(issue)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                **resolution_response(
                    row, issue=issue, message=str(exc.detail), authority_reason=gate.reason
                ),
                "detail": str(exc.detail),
            },
        )

    payload = result if isinstance(result, dict) else json.loads(result.body)
    state = EXECUTOR_BY_STATUS[executor_name].get(str(payload.get("status")))
    if state is None:  # unknown executor status vocabulary: fail closed, never claim success
        state = ResolutionState.PENDING.value
    row = await _persist(
        db,
        existing,
        issue=issue,
        order=order,
        user_id=record.user_id,
        action=action.value,
        decision=decision,
        executor=executor_name,
        state=state,
        provider=payload.get("provider") or (
            "razorpay_test_mode" if executor_name == "refund" else None
        ),
        provider_reference=payload.get("razorpay_refund_id")
        or payload.get("provider_reference"),
        provider_attempted=_provider_attempted(executor_name, payload, state),
    )
    row.executor_record_id = (
        payload.get("refund_id")
        or payload.get("replacement_id")
        or payload.get("return_id")
    )
    row.message = payload.get("agent_message")
    await audit.append(
        db, mission_id, "buyer_agent", gate,
        note=(
            f"resolve {issue_id}: action={action.value} decision={decision} "
            f"executor={executor_name} record={row.executor_record_id} state={state} "
            f"provider={row.provider} provider_reference={row.provider_reference} "
            f"provider_attempted={row.provider_attempted}"
        ),
    )
    await db.commit()
    await db.refresh(issue)

    body = resolution_response(
        row,
        issue=issue,
        execution=payload,
        message=row.message,
        already_existed=bool(payload.get("already_existed")),
        authority_reason=gate.reason,
    )
    code = _status_code_for(state)
    if code == 200:
        return body
    return JSONResponse(
        status_code=code,
        content={**body, "detail": payload.get("detail") or body["resolution_note"]},
    )


async def _manual_review_response(
    m, db, record, order, issue, mission_id: str, issue_id: str
) -> dict:
    """
    Honor a customer's automated-decision opt-out (Task 14B.5A). Persists one ESCALATED
    resolution summary for the issue, audits that manual review was requested, emits a single
    event, and returns — contacting NO provider and running NO executor. No ticket number is
    invented: 'escalated' here means exactly 'a human chose to look at this', nothing more.
    """
    existing = await load_resolution(db, issue_id)
    message = (
        f"Issue {issue_id} is under customer-requested manual review. The autonomous resolution "
        "path is paused for this issue: no action was executed and no provider was contacted."
    )
    row = await _persist(
        db,
        existing,
        issue=issue,
        order=order,
        user_id=record.user_id,
        action=AllowedAction.ESCALATE_TO_HUMAN.value,
        decision="manual_review",
        executor="none",
        state=ResolutionState.ESCALATED.value,
        provider=None,
        provider_reference=None,
        provider_attempted=False,
    )
    row.message = message
    await db.flush()

    decision = AuthorityDecision(
        decision=Decision.APPROVED,
        reason=(
            "Customer opted out of automated resolution — manual review requested "
            "(Task 14B.5A). Autonomous execution suppressed; Authority Engine rules unchanged."
        ),
        action=AllowedAction.ESCALATE_TO_HUMAN,
        requested_amount=None,
    )
    await audit.append(
        db, mission_id, "buyer_agent", decision,
        note=(
            f"manual_review {issue_id}: action=escalate_to_human executor=none "
            f"state={ResolutionState.ESCALATED.value} provider_attempted=False "
            "— autonomous resolution blocked while manual review is active"
        ),
    )
    await db.commit()
    return resolution_response(
        row, issue=issue, message=message, already_existed=existing is not None
    )


async def _persist(
    db,
    existing: ResolutionRecord | None,
    *,
    issue: OrderIssueRecord,
    order: ResolveOrderRecord,
    user_id: str,
    action: str,
    decision: str,
    executor: str,
    state: str,
    provider: str | None,
    provider_reference: str | None,
    provider_attempted: bool,
) -> ResolutionRecord:
    """One row per issue, updated in place — repeated /resolve never multiplies resolutions."""
    row = existing or ResolutionRecord(
        resolution_id=f"res_{uuid.uuid4().hex[:12]}",
        issue_id=issue.issue_id,
        mission_id=issue.mission_id,
        order_id=order.order_id,
        user_id=user_id,
    )
    row.action = action
    row.authority_decision = decision
    row.executor = executor
    row.resolution_state = state
    row.provider = provider
    row.provider_reference = provider_reference
    row.provider_attempted = provider_attempted
    if existing is None:
        db.add(row)
    await db.flush()
    return row
