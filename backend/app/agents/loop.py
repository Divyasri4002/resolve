"""
The bounded RESOLVE agent loop (Task CORE-2, Phase 4).

One user message in, one agent turn out. A turn is a loop, not a call:

    context -> the model names a declared tool -> the server validates and runs the guarded
    operation behind it -> the result is re-read from the database and put back into the context ->
    the model names the next step -> … -> the model answers the user.

Four properties this file exists to guarantee, and none of them is the model's job:

* **It is bounded.** `MAX_AGENT_STEPS` steps per request, from config, with a hard default. Running
  out of steps is a normal ending: the agent says what it needs from the user rather than starting a
  second call that could run forever.
* **It is server-authoritative.** The model only ever names a tool and its arguments. The state the
  next step sees is re-read from the database after each operation, so a claim that something
  happened changes nothing unless the guarded operation actually wrote it. Nothing the model can say
  selects a product identity, an amount, an owner, or a payment.
* **It fails closed.** An unknown tool name, arguments that violate the declared schema, a decision
  that cannot be parsed: each ends the turn with an honest sentence and no execution.
* **It does not regress the mission.** The lifecycle ladder in `app.agents.lifecycle` is applied to
  every status a conversational turn wants to write, so chatting after `order_created` cannot put a
  mission back to `discovery_completed`.

What is persisted is a transcript and a state trail — the user's message, the tool the agent named,
its structured result, the mission's new status, and the sentence the user gets. No chain of
thought, no raw provider payload, no credential.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.conversation import append_event, as_model_context, load_recent
from app.agents.context import build_agent_context, redact_secrets
from app.agents.reference_resolution import (
    AMBIGUOUS,
    ANCHOR_EVENT_TYPE,
    ANCHOR_KIND_RESOLVED,
    ISSUE_ANCHOR_EVENT_TYPE,
    ISSUE_ANCHOR_KIND_RESOLVED,
    NOT_FOUND,
    RESOLVED,
    IssueReference,
    ProductReference,
    resolve_issue_reference,
    resolve_product_reference,
)
from app.agents.response_grounding import grounded_response
from app.agents.tools import AgentToolError, execute_tool, tool_definitions
from app.config import get_settings
from app.models.agent import AgentStepDecision
from app.models.db_models import MissionRecord
from app.models.mission import ShoppingIntent

logger = logging.getLogger("resolve.agents.loop")

DEFAULT_MAX_AGENT_STEPS = 8
CONVERSATION_WINDOW = 20

_REFUSAL_MESSAGE = (
    "I could not work out a safe next step, so nothing was changed on your mission."
)
_STEP_BUDGET_MESSAGE = (
    "I have used the steps available for this message and have not finished. Nothing is broken and "
    "nothing was left half-done — tell me how you want to continue, or take the step yourself from "
    "the panel."
)

# The label `/agent` has always reported for a discovery step in `agent_proposal.action`. The tool's
# own name is newer than that field, so the two are reconciled here instead of in ten places.
_ACTION_LABELS = {"search_products": "product_discovery"}

# A turn ends here, not further, after these tools succeed. Discovery is the one whose whole purpose
# is to put a list in front of the buyer: its message is the grounded summary of what the provider
# actually returned, and the next move is theirs. `request_more_information` and `escalate_to_human`
# both end in something only another party can supply — the buyer's answer, a human's attention — so
# continuing to churn after them would be the agent talking over the pause it just asked for.
# Everything else in the surface is a step the agent may chain on its own, up to the step budget.
STOP_AFTER_TOOLS = frozenset(
    {"search_products", "request_more_information", "escalate_to_human"}
)

# The tools whose `issue_id` argument is server-owned whenever the deterministic resolver named
# an issue for this message: the loop substitutes the canonical id, and a model-supplied id that
# disagrees with it is refused before anything runs.
_ISSUE_SCOPED_TOOLS = frozenset(
    {"assess_issue", "resolve_issue", "request_more_information", "escalate_to_human"}
)


@dataclass
class AgentTurnOutcome:
    """Everything one agent turn knows it did, in the shape the endpoint returns."""

    agent_message: str
    action: str = "clarify_intent"
    amount: str | None = None
    authority_decision: dict[str, Any] | None = None
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    discovered_products: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    stop: bool = False


class AgentTurn:
    """
    One turn's state, kept in one place so the loop body stays readable.

    The record is re-read after every tool because the guarded operation, not this loop, decides
    what the mission's status ends up being.
    """

    def __init__(
        self,
        *,
        mission_id: str,
        record: MissionRecord,
        current_user: Any,
        db: AsyncSession,
        reasoning: Any,
        max_steps: int,
        reference: ProductReference | None = None,
        issue_reference: IssueReference | None = None,
    ) -> None:
        self.mission_id = mission_id
        self.record = record
        self.current_user = current_user
        self.db = db
        self.reasoning = reasoning
        self.max_steps = max_steps
        self.reference = reference
        self.issue_reference = issue_reference
        self.entry_status = record.status
        self.tool_history: list[dict[str, Any]] = []
        self.seen_calls: set[str] = set()
        self.authority_decision: dict[str, Any] | None = None
        self.declared_names = [definition.name for definition in tool_definitions()]

    @property
    def declarations(self) -> list[dict[str, Any]]:
        from app.agents.tools import declarations_for_model

        return declarations_for_model()

    async def context(self, user_input: str, step: int) -> dict[str, Any]:
        from app.routers import missions as m

        rows = await load_recent(
            self.db, self.mission_id, limit=CONVERSATION_WINDOW - len(self.tool_history)
        )
        (
            order,
            linkage,
            issue,
            assessment,
            resolution,
            issues,
            issue_outcomes,
        ) = await m.agent_mission_rows(self.db, self.record)
        return build_agent_context(
            self.record,
            order=order,
            linkage_state=linkage,
            issue=issue,
            assessment=assessment,
            resolution=resolution,
            issues=issues,
            issue_outcomes=issue_outcomes,
            conversation=as_model_context(rows),
            product_reference=self.reference,
            issue_reference=self.issue_reference,
            steps_used=step,
            steps_remaining=max(self.max_steps - step, 0),
            tool_history=self.tool_history,
            user_input=user_input,
        )

    async def refresh_record(self) -> None:
        from app.routers import missions as m

        refreshed = await m._get_mission_or_none(self.db, self.mission_id)
        if refreshed is not None:
            self.record = refreshed

    def _enforce_issue_identity(self, name: str, validated: Any) -> str | None:
        """
        The server's issue identity, enforced on the model's tool call.

        When the deterministic resolver resolved this message's issue phrase, that canonical id
        IS the identity for the issue-scoped tools: a model that omitted the id gets it injected,
        and a model that named a different one is refused before anything runs — an id the model
        supplies can never override what the server resolved from the user's own words. When the
        resolver named nothing, the call passes through untouched and the existing mission-scoped
        validation remains the whole safety story, exactly as before.
        """
        reference = self.issue_reference
        if (
            name not in _ISSUE_SCOPED_TOOLS
            or reference is None
            or reference.state != RESOLVED
            or not reference.issue_id
        ):
            return None
        supplied = getattr(validated, "issue_id", None)
        if supplied is None:
            validated.issue_id = reference.issue_id
            return None
        if supplied == reference.issue_id:
            return None
        return (
            f"The user's message resolved to the issue at position {reference.position} of this "
            "mission's reported issues, but the tool call named a different issue id. The "
            "server-resolved identity wins and nothing was executed."
        )

    async def run(
        self, user_input: str, decision: AgentStepDecision, context: dict[str, Any]
    ) -> AgentTurnOutcome:
        if decision.action == "respond":
            draft = decision.message or _REFUSAL_MESSAGE
            grounded = await self._grounded_message(user_input, draft, context)
            return AgentTurnOutcome(
                agent_message=redact_secrets(grounded, limit=4000),
                action=self._final_action(None),
                authority_decision=self.authority_decision,
                tool_trace=self.tool_history,
                stop=True,
            )
        return await self._run_tool(user_input, decision)

    async def _grounded_message(
        self, user_input: str, draft: str, context: dict[str, Any]
    ) -> str:
        """
        The respond branch's text, verified against the product evidence this turn carried.

        Only the sentence that reaches the user is verified — tool results and system events are
        never user-facing product claims. A turn with no product evidence at all (a recovery
        mission, a mission that has not searched) skips verification entirely.
        """
        shopping = context.get("shopping") or {}
        products = list(shopping.get("discovered_products") or [])
        additional: list[dict[str, Any]] = []
        selected = shopping.get("selected_product")
        if isinstance(selected, dict):
            additional.append(selected)
        reference = context.get("product_reference") or {}
        if isinstance(reference.get("product"), dict):
            additional.append(reference["product"])
        additional.extend(
            candidate
            for candidate in reference.get("other_candidates") or []
            if isinstance(candidate, dict)
        )
        if not products and not additional:
            return draft
        order = context.get("order") or {}
        grounded = await grounded_response(
            self.reasoning,
            draft,
            user_request=user_input,
            products=products,
            relevant_context={
                "budget": shopping.get("budget"),
                "currency": shopping.get("currency"),
                "amounts": [order["amount"]] if order.get("amount") is not None else [],
                "additional_products": additional,
            },
        )
        return grounded.message

    def _final_action(self, tool_name: str | None) -> str:
        if tool_name:
            return _ACTION_LABELS.get(tool_name, tool_name)
        return "clarify_intent"

    async def _run_tool(self, user_input: str, decision: AgentStepDecision) -> AgentTurnOutcome:
        from app.agents.tools import validate_tool_call

        name = str(decision.tool_name)
        arguments = decision.arguments or {}

        # Validate before anything is written down. A call the schema refuses must not leave its
        # payload in storage — the model can put a `user_id`, an amount or a provider reference in an
        # argument list, and persisting the refused attempt would be a second copy of data that was
        # deliberately not accepted.
        try:
            _definition, validated = validate_tool_call(name, arguments)
        except AgentToolError as exc:
            await self._record_refusal(name, str(exc))
            return AgentTurnOutcome(
                agent_message=redact_secrets(str(exc), limit=4000),
                action="refused",
                authority_decision=self.authority_decision,
                tool_trace=self.tool_history,
                stop=True,
            )
        identity_refusal = self._enforce_issue_identity(name, validated)
        if identity_refusal is not None:
            await self._record_refusal(name, identity_refusal)
            return AgentTurnOutcome(
                agent_message=redact_secrets(identity_refusal, limit=4000),
                action="refused",
                authority_decision=self.authority_decision,
                tool_trace=self.tool_history,
                stop=True,
            )
        safe_arguments = validated.model_dump(mode="json")

        signature = json.dumps({"tool": name, "arguments": safe_arguments}, sort_keys=True, default=str)
        if signature in self.seen_calls:
            # The same call twice in one turn is a model that has not noticed it already ran.
            # Running it again would re-attempt a guarded operation for no new reason.
            return AgentTurnOutcome(
                agent_message=(
                    f"I already ran {name} for this message; doing it again would repeat the same "
                    "request. Here is where your mission stands: "
                    f"{self.record.status}."
                ),
                action=self._final_action(name),
                authority_decision=self.authority_decision,
                tool_trace=self.tool_history,
                stop=True,
            )
        self.seen_calls.add(signature)

        await append_event(
            self.db,
            mission_id=self.mission_id,
            user_id=self.record.user_id,
            role="agent",
            event_type="tool_call",
            content=f"Requested {name}",
            tool_name=name,
            payload={"arguments": safe_arguments},
        )

        try:
            result = await execute_tool(
                name,
                safe_arguments,
                mission_id=self.mission_id,
                record=self.record,
                current_user=self.current_user,
                db=self.db,
                reasoning=self.reasoning,
                user_input=user_input,
            )
        except AgentToolError as exc:
            await self._record_refusal(name, str(exc))
            return AgentTurnOutcome(
                agent_message=redact_secrets(str(exc), limit=4000),
                action="refused",
                authority_decision=self.authority_decision,
                tool_trace=self.tool_history,
                stop=True,
            )

        await append_event(
            self.db,
            mission_id=self.mission_id,
            user_id=self.record.user_id,
            role="system",
            event_type="tool_result",
            content=result.user_message or f"{name} returned no message",
            tool_name=name,
            payload=_result_payload(result),
        )
        self.tool_history.append(
            {
                "tool": name,
                "success": result.success,
                "state": result.next_state,
                "error_code": result.error_code,
            }
        )

        if name == "resolve_issue":
            self.authority_decision = _authority_decision(result)

        await self.refresh_record()
        discovered = (
            list(result.result.get("products") or [])
            if name == "search_products"
            else []
        )
        return AgentTurnOutcome(
            agent_message=result.user_message or f"{name} completed.",
            action=self._final_action(name),
            amount=_intent_amount(self.record),
            authority_decision=self.authority_decision,
            tool_trace=self.tool_history,
            discovered_products=discovered,
            degraded=not result.success and result.error_code == "discovery_failed",
            stop=result.success and name in STOP_AFTER_TOOLS,
        )

    async def _record_refusal(self, name: str, reason: str) -> None:
        await append_event(
            self.db,
            mission_id=self.mission_id,
            user_id=self.record.user_id,
            role="system",
            event_type="tool_result",
            content=reason,
            tool_name=name,
            payload={"refused": True},
        )
        self.tool_history.append(
            {"tool": name, "success": False, "state": self.record.status, "error_code": "refused"}
        )


async def run_agent_turn(
    *,
    mission_id: str,
    user_input: str,
    record: MissionRecord,
    current_user: Any,
    db: AsyncSession,
    reasoning: Any,
    max_steps: int | None = None,
) -> AgentTurnOutcome:
    """
    Drive one bounded agent turn and return the sentence plus the trail that produced it.

    The caller has already verified the mission exists, that this caller may act on it, and that it
    is a shopping mission; this function assumes ownership has been settled and never re-decides it.
    """
    settings = get_settings()
    budget = max_steps or int(
        getattr(settings, "agent_max_steps", DEFAULT_MAX_AGENT_STEPS) or DEFAULT_MAX_AGENT_STEPS
    )
    budget = max(1, min(budget, 24))

    # Product phrases in the message are resolved by the server, from the mission's own persisted
    # state, before the model is ever consulted. The history read here is deliberately the one
    # from BEFORE this message is appended, so "that one" resolves against what was already said.
    from app.routers import missions as m

    prior_rows = await load_recent(db, mission_id, limit=CONVERSATION_WINDOW)
    reference = resolve_product_reference(
        user_input, m._intent_from_record(record), prior_rows
    )
    # Issue phrases ("the second issue", "the refund issue") resolve the same way, against this
    # mission's own persisted issue list and nothing else — another mission's issues are not
    # refused here, they are structurally invisible.
    issue_rows = await m._load_issues(db, mission_id)
    issue_reference = resolve_issue_reference(user_input, issue_rows)

    turn = AgentTurn(
        mission_id=mission_id,
        record=record,
        current_user=current_user,
        db=db,
        reasoning=reasoning,
        max_steps=budget,
        reference=reference,
        issue_reference=issue_reference,
    )

    # The buyer's message is the first row of the turn: whatever happens next, what they asked is
    # already on record. When the server itself resolved a product phrase in it, the resolution is
    # anchored immediately after, so later turns can say "that one" against a canonical product id
    # instead of prose.
    await append_event(
        db,
        mission_id=mission_id,
        user_id=record.user_id,
        role="user",
        event_type="user_message",
        content=user_input,
    )
    if reference.state == RESOLVED and reference.product is not None:
        await append_event(
            db,
            mission_id=mission_id,
            user_id=record.user_id,
            role="system",
            event_type=ANCHOR_EVENT_TYPE,
            content=(
                f"Resolved {reference.expression!r} to the product at position "
                f"{reference.position} of the current results."
            ),
            payload={
                "kind": ANCHOR_KIND_RESOLVED,
                "expression": reference.expression,
                "product_id": reference.product.product_id,
                "position": reference.position,
                "set_id": reference.set_id,
            },
        )
    if issue_reference.state == RESOLVED and issue_reference.issue_id is not None:
        await append_event(
            db,
            mission_id=mission_id,
            user_id=record.user_id,
            role="system",
            event_type=ISSUE_ANCHOR_EVENT_TYPE,
            content=(
                f"Resolved {issue_reference.expression!r} to the issue at position "
                f"{issue_reference.position} of this mission's reported issues."
            ),
            payload={
                "kind": ISSUE_ANCHOR_KIND_RESOLVED,
                "expression": issue_reference.expression,
                "issue_id": issue_reference.issue_id,
                "issue_type": getattr(issue_reference.issue, "issue_type", None),
                "position": issue_reference.position,
            },
        )
    await db.commit()

    # An ambiguity or a definite miss the resolver can answer honestly is answered here, without
    # consulting the model at all: no step runs, no tool is named, and therefore no assessment,
    # authority decision, resolution, or provider call can happen off the back of a phrase the
    # server could not resolve to exactly one issue.
    if issue_reference.short_circuit:
        outcome = _issue_reference_outcome(issue_reference)
        await _finish(turn, outcome, user_input)
        return outcome

    outcome: AgentTurnOutcome | None = None
    for step in range(budget):
        context = await turn.context(user_input, step)
        try:
            decision = await reasoning.choose_agent_action(context, turn.declarations)
        except Exception as exc:  # noqa: BLE001 — an unreachable model must never 500 a turn
            logger.warning("agent step chooser failed on mission %s: %s", mission_id, exc)
            outcome = await _park_for_retry(turn, exc)
            break
        if not isinstance(decision, AgentStepDecision):
            outcome = AgentTurnOutcome(
                agent_message=_REFUSAL_MESSAGE,
                authority_decision=turn.authority_decision,
                tool_trace=turn.tool_history,
            )
            break

        outcome = await turn.run(user_input, decision, context)
        if outcome.stop:
            # The agent chose to speak, or the step was refused before anything ran. Either way
            # there is nothing further for this message to do.
            break
        # A tool ran: rebuild the context from what the database now says and take another step,
        # until the model answers, the call repeats itself, or the step budget is spent.
    else:
        # Reached only when the loop ran out of steps without ever stopping on its own.
        outcome = AgentTurnOutcome(
            agent_message=_STEP_BUDGET_MESSAGE,
            action=outcome.action if outcome else "clarify_intent",
            amount=_intent_amount(turn.record),
            authority_decision=turn.authority_decision,
            tool_trace=turn.tool_history,
            stop=True,
        )

    if outcome is None:  # pragma: no cover — only a zero-step budget could get here
        outcome = AgentTurnOutcome(
            agent_message=_STEP_BUDGET_MESSAGE,
            authority_decision=turn.authority_decision,
            tool_trace=turn.tool_history,
        )

    await _finish(turn, outcome, user_input)
    return outcome


def _issue_reference_outcome(issue_reference: IssueReference) -> AgentTurnOutcome:
    """
    The deterministic answer to an issue phrase the server could not resolve to exactly one
    issue: the ambiguity is put to the buyer with the candidates named, or the miss is stated
    plainly. The model is not consulted, so nothing downstream of it can run either.
    """
    if issue_reference.state == AMBIGUOUS:
        listing = "; ".join(
            f"{position}. {getattr(row, 'issue_type', 'issue')}"
            f" — {redact_secrets(str(getattr(row, 'description', '') or ''), limit=120)}"
            for position, row in issue_reference.candidates
        )
        message = (
            f"{issue_reference.expression} matches more than one issue reported on this "
            f"mission, and I will not guess which you meant: {listing}. Tell me which one — "
            "for example 'the first issue' — or describe it in your own words."
        )
    else:
        message = issue_reference.reason or (
            "I could not resolve that to one of the issues reported on this mission, so I "
            "changed nothing."
        )
    return AgentTurnOutcome(
        agent_message=redact_secrets(message, limit=4000),
        tool_trace=[],
        stop=True,
    )


async def _park_for_retry(turn: AgentTurn, exc: Exception) -> AgentTurnOutcome:
    """
    The chooser itself could not be reached: keep the mission usable and say so plainly.

    This preserves the degradation contract the shopping flow has always had — the request is
    preserved, the mission parks in a retryable state, the message names the reasoning service, and
    the response is a 200 with an honest sentence rather than a 500.
    """
    from app.routers import missions as m

    combined = turn.record.shopping_intent or {}
    intent = m._intent_from_record(turn.record) or ShoppingIntent(
        raw_prompt=str(combined.get("raw_prompt") or "")
    )
    m._apply_conversational_state(turn.record, intent, m.MissionStatus.NEEDS_CLARIFICATION.value)
    turn.record.shopping_intent = intent.model_dump(mode="json")
    turn.record.resolution_summary = (
        f"Reasoning service temporarily unavailable: {exc}. I've kept your mission safe, "
        "please try again in a moment."
    )
    await turn.db.commit()
    return AgentTurnOutcome(
        agent_message=turn.record.resolution_summary,
        action="clarify_intent",
        amount=_intent_amount(turn.record),
        authority_decision=None,
        tool_trace=turn.tool_history,
        degraded=True,
    )


async def _finish(turn: AgentTurn, outcome: AgentTurnOutcome, user_input: str) -> None:
    """Write the agent's sentence, and the state change if the turn caused one, then commit."""
    await append_event(
        turn.db,
        mission_id=turn.mission_id,
        user_id=turn.record.user_id,
        role="agent",
        event_type="agent_message",
        content=outcome.agent_message,
    )
    if turn.record.status != turn.entry_status:
        await append_event(
            turn.db,
            mission_id=turn.mission_id,
            user_id=turn.record.user_id,
            role="system",
            event_type="system_state_change",
            content=f"Mission moved from {turn.entry_status} to {turn.record.status}.",
            payload={"from": turn.entry_status, "to": turn.record.status},
        )
    await turn.db.commit()


def _result_payload(result: Any) -> dict[str, Any]:
    """The bounded, secret-free summary stored with a tool_result row."""
    return {
        "success": bool(result.success),
        "next_state": result.next_state,
        "error_code": result.error_code,
        "result_keys": sorted((result.result or {}).keys()),
        "user_message": redact_secrets(result.user_message or "", limit=600) or None,
    }


def _authority_decision(result: Any) -> dict[str, Any] | None:
    """
    The Authority Engine's own word about the consequential step this turn took — or None.

    A turn that evaluated no authority reports no authority decision. That is the honest replacement
    for the `{"approved": true}` the endpoint used to claim for every shopping message.
    """
    gate = (result.result or {}).get("authority_decision")
    if gate is None:
        return None
    reason = (result.result or {}).get("authority_reason") or (result.result or {}).get(
        "resolution_note"
    )
    return {
        "approved": gate == "allow",
        "decision": "approved" if gate == "allow" else "blocked",
        "reason": redact_secrets(str(reason or ""), limit=600) or None,
        "action": (result.result or {}).get("action"),
        "resolution_state": (result.result or {}).get("resolution_state"),
    }


def _intent_amount(record: MissionRecord) -> str | None:
    intent = record.shopping_intent or {}
    budget = intent.get("budget")
    return str(budget) if budget is not None else None
