"""
The RESOLVE agent's DECLARED capability surface (Task CORE-2).

This is the whole of what the agent may do. Nothing else is reachable from a model response.

Two rules decide every entry in this registry:

1. **Every tool is a thin wrapper around a guarded operation that already exists.** The HTTP
   endpoints in `app/routers/missions.py` are the authoritative implementations — they own the
   ownership check, the state preconditions, the canonical server-side derivation of amounts and
   identity, the idempotency gates, and the Authority Engine call. The tools below call those same
   functions (through the router module, exactly as `app/resolution/orchestrator.py` does) rather
   than re-implementing any of it, so there is only ever one authorization path per operation.

2. **The vocabulary is deliberately incomplete.** There is no `create_payment`, no
   `confirm_payment`, no `confirm_checkout`, no order-creation tool, and no arbitrary
   HTTP/Python/SQL/Shopify-mutation tool. Payment and explicit checkout confirmation stay human
   boundaries (safety boundaries 5-8), so a mission can be *prepared* to pay by this agent and can
   never be taken through the payment itself. The router imports these names from
   `app.tools.product_discovery`, so the same test seam that covers discovery today covers it here.

Anything the model can name is validated against a strict schema here BEFORE the operation runs,
and an unknown tool name is a refusal, not an error to recover from (safety boundaries 12-13).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import MissionRecord, OrderIssueRecord, UserRecord
from app.models.mission import IssueType

logger = logging.getLogger("resolve.agents.tools")


class AgentToolError(Exception):
    """Base for a tool surface refusal: bad name, bad arguments, or a failed operation."""


class UnknownToolError(AgentToolError):
    """The model asked for a capability that does not exist. Fails closed, nothing runs."""


class InvalidToolArgumentsError(AgentToolError):
    """The model named a real tool but sent arguments that do not satisfy its schema."""


class ToolArguments(BaseModel):
    """
    Base for every tool's argument model.

    `extra="forbid"` is the load-bearing part: a tool call that carries an unrequested field —
    `amount`, `user_id`, a merchant catalog identity, an authority block, a provider reference —
    whatever the model invents — is rejected outright instead of being quietly ignored. Nothing the
    model can send has a destination here except the fields declared below.
    """

    model_config = ConfigDict(extra="forbid")


class SearchProductsArgs(ToolArguments):
    query: str | None = Field(
        default=None,
        max_length=200,
        description="What to look for. Budget, currency and preferences come from the "
        "mission's own persisted intent, never from this call.",
    )


class ClarifyVariantArgs(ToolArguments):
    product_id: str = Field(
        ..., min_length=1, max_length=128, description="One of THIS mission's discovered products"
    )
    answer: str = Field(..., min_length=1, max_length=120)


class SelectProductArgs(ToolArguments):
    product_id: str = Field(
        ..., min_length=1, max_length=128, description="One of THIS mission's discovered products"
    )


class RecommendProductArgs(ToolArguments):
    product_id: str = Field(
        ..., min_length=1, max_length=128, description="One of THIS mission's current discovered products"
    )


class PrepareCheckoutArgs(ToolArguments):
    """No fields at all: the checkout review is derived from the server's own selection."""


class ReportIssueArgs(ToolArguments):
    issue_type: str = Field(..., description=f"One of {sorted(t.value for t in IssueType)}")
    description: str = Field(..., min_length=1, max_length=2000)
    requested_resolution: str | None = Field(
        default=None,
        description="The user's stated PREFERENCE. It authorizes nothing; the Authority "
        "Engine decides the remedy.",
    )

    @field_validator("issue_type")
    @classmethod
    def _known_issue_type(cls, v: str) -> str:
        allowed = {t.value for t in IssueType}
        if v not in allowed:
            raise ValueError(f"issue_type must be one of {sorted(allowed)}")
        return v

    @field_validator("requested_resolution")
    @classmethod
    def _known_preference(cls, v: str | None) -> str | None:
        if v is None:
            return None
        allowed = {"none", "refund", "replacement"}
        if v not in allowed:
            raise ValueError(f"requested_resolution must be one of {sorted(allowed)}")
        return v


class IssueScopedArgs(ToolArguments):
    issue_id: str | None = Field(
        default=None,
        max_length=64,
        description="Which reported issue to act on. Omit to use the issue the server resolved "
        "from the user's message, or the mission's most recent eligible issue; a value is always "
        "checked against the mission.",
    )


class ResolveIssueArgs(IssueScopedArgs):
    pass


class RequestMoreInformationArgs(IssueScopedArgs):
    question: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="What the agent needs from the user before it can continue.",
    )


class EscalateToHumanArgs(IssueScopedArgs):
    reason: str | None = Field(default=None, max_length=500)


class CheckPurchaseEligibilityArgs(ToolArguments):
    """No fields: eligibility derives entirely from the mission's own persisted state."""


@dataclass(frozen=True)
class AgentToolDefinition:
    """One declared capability: a name, what it means, the schema its arguments must satisfy."""

    name: str
    description: str
    input_schema: dict[str, Any]
    arguments_model: type[ToolArguments]
    runner: Callable[["ToolCall"], Awaitable["AgentToolResult"]]


class AgentToolResult(BaseModel):
    """
    The structured answer a tool gives the loop — and the only thing about that operation the
    model gets to see afterwards.

    `result` is a bounded, secret-free summary of the operation's own outcome; `next_state` is the
    mission status as the DATABASE now reports it (never what the tool hoped); `user_message` is
    the honest, already-written sentence the operation produced; `error_code` is set when the
    guarded operation refused, and the refusal text is carried in `user_message` rather than
    swallowed.
    """

    tool_name: str
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    next_state: str | None = None
    user_message: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """A validated call plus everything server-side it is allowed to touch."""

    mission_id: str
    arguments: ToolArguments
    record: MissionRecord
    current_user: UserRecord | None
    db: AsyncSession
    reasoning: Any
    user_input: str


_TOOLS: dict[str, AgentToolDefinition] = {}


def _register(
    name: str,
    description: str,
    arguments_model: type[ToolArguments],
    runner: Callable[[ToolCall], Awaitable[AgentToolResult]],
) -> None:
    schema = _declared_schema(arguments_model)
    _TOOLS[name] = AgentToolDefinition(
        name=name,
        description=description,
        input_schema=schema,
        arguments_model=arguments_model,
        runner=runner,
    )


def _declared_schema(arguments_model: type[ToolArguments]) -> dict[str, Any]:
    """
    The JSON Schema a tool accepts, tightened for declaration.

    `extra="forbid"` becomes `additionalProperties: false` on the object itself (Pydantic puts it
    under a validation keyword that is not part of the wire format), and every property listed here
    is a name the model may actually send.
    """
    schema = arguments_model.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def tool_definitions() -> list[AgentToolDefinition]:
    """The complete declared capability surface, in declaration order."""
    return list(_TOOLS.values())


def tool_names() -> list[str]:
    return [definition.name for definition in tool_definitions()]


def declarations_for_model() -> list[dict[str, Any]]:
    """
    The declared surface as a reasoning provider receives it: a name, what it means, and the JSON
    Schema its arguments must satisfy. Nothing else about a tool — not its runner, not the guards it
    inherits — is expressible to the model.
    """
    return [
        {
            "name": definition.name,
            "description": definition.description,
            "parameters": definition.input_schema,
        }
        for definition in tool_definitions()
    ]


def get_tool_definition(name: str) -> AgentToolDefinition:
    definition = _TOOLS.get(name)
    if definition is None:
        raise UnknownToolError(
            f"'{name}' is not a RESOLVE agent capability. The agent has no tool by that name, "
            f"so nothing was executed. Declared tools: {', '.join(tool_names())}"
        )
    return definition


def validate_tool_call(name: str, arguments: Any) -> tuple[AgentToolDefinition, ToolArguments]:
    """
    Resolve a model-proposed call against the declared surface (Phase 3).

    Unknown name -> UnknownToolError. Arguments that are not an object, carry an undeclared field,
    or fail a declared field's type/length/enum constraint -> InvalidToolArgumentsError. Both are
    refusals raised before `execute_tool` is reached, so no guarded operation sees a malformed call.
    """
    definition = get_tool_definition(name)
    if arguments is None:
        payload: dict[str, Any] = {}
    elif isinstance(arguments, dict):
        payload = arguments
    else:
        raise InvalidToolArgumentsError(
            f"Arguments for '{name}' must be a JSON object, not {type(arguments).__name__}."
        )
    try:
        return definition, definition.arguments_model.model_validate(payload)
    except ValidationError as exc:
        raise InvalidToolArgumentsError(
            f"Arguments for '{name}' do not satisfy its declared schema: "
            f"{_describe_validation_error(exc)}"
        ) from exc


def _describe_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(piece) for piece in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid arguments"


async def execute_tool(
    name: str,
    arguments: Any,
    *,
    mission_id: str,
    record: MissionRecord,
    current_user: UserRecord | None,
    db: AsyncSession,
    reasoning: Any,
    user_input: str = "",
) -> AgentToolResult:
    """
    Validate, then run, one declared capability.

    A guarded operation's own HTTPException is returned as a failed `AgentToolResult` — the
    refusal is data the agent reasons over, not an exception that escapes into the request. No
    exception raised here ever leaves a partially-executed action behind, because each runner
    delegates to a function that either completes its own transaction or declines without one.
    """
    try:
        definition, validated = validate_tool_call(name, arguments)
    except AgentToolError as exc:
        logger.warning("agent tool refusal on mission %s: %s", mission_id, exc)
        raise

    call = ToolCall(
        mission_id=mission_id,
        arguments=validated,
        record=record,
        current_user=current_user,
        db=db,
        reasoning=reasoning,
        user_input=user_input,
    )
    try:
        result = await definition.runner(call)
    except HTTPException as exc:
        return _failure(
            name,
            record,
            error_code=_error_code(exc.status_code),
            user_message=_text(exc.detail),
        )
    except Exception as exc:  # noqa: BLE001 — a tool failure is a result, never a 500
        logger.exception("agent tool %s failed on mission %s", name, mission_id)
        return _failure(
            name,
            record,
            error_code="tool_execution_failed",
            user_message=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
    return await _with_next_state(result, mission_id, db)


def _error_code(status_code: int) -> str:
    if status_code == 404:
        return "not_found_or_not_yours"
    if status_code == 400:
        return "precondition_refused"
    if status_code == 403:
        return "forbidden"
    if status_code == 503:
        return "temporarily_unavailable"
    return f"http_{status_code}"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value[:1000]
    return json.dumps(value, default=str)[:1000]


def _failure(name: str, record: MissionRecord, *, error_code: str, user_message: str) -> AgentToolResult:
    return AgentToolResult(
        tool_name=name,
        success=False,
        next_state=record.status,
        user_message=user_message,
        error_code=error_code,
    )


async def _with_next_state(
    result: AgentToolResult, mission_id: str, db: AsyncSession
) -> AgentToolResult:
    """Stamp the state the database actually holds after the operation, not what it returned."""
    from app.routers import missions as m

    record = await m._get_mission_or_none(db, mission_id)
    if record is not None:
        result.next_state = record.status
    return result


# --------------------------------------------------------------------------- #
# The declared capabilities. Nine were declared in Phase 2; `recommend_product`
# is the one deliberate addition (CORE-5 Phase 5 §11 — the smallest capability
# that lets "the one you recommended" resolve from structured, persisted identity).
# Each one is a wrapper.
# --------------------------------------------------------------------------- #


async def _search_products(call: ToolCall) -> AgentToolResult:
    from app.agents.search_planning import (
        REUSE,
        SearchPlan,
        apply_reuse,
        plan_search,
        reuse_message,
    )
    from app.routers import missions as m

    arguments: SearchProductsArgs = call.arguments  # type: ignore[assignment]
    intent = m._intent_from_record(call.record)
    if intent is None:
        return _failure(
            "search_products",
            call.record,
            error_code="not_a_shopping_mission",
            user_message="Product discovery only applies to a shopping mission.",
        )
    plan: SearchPlan = plan_search(intent)
    if plan.action == REUSE:
        source = next(
            record
            for record in reversed(intent.result_sets)
            if record.set_id == plan.source_set_id
        )
        reused = apply_reuse(intent, plan)
        message = reuse_message(reused, source)
        m._apply_conversational_state(
            call.record, intent, m.MissionStatus.DISCOVERY_COMPLETED.value
        )
        call.record.shopping_intent = intent.model_dump(mode="json")
        call.record.resolution_summary = message
        await call.db.commit()
        return AgentToolResult(
            tool_name="search_products",
            success=True,
            result={
                "products": [product.model_dump(mode="json") for product in reused.products],
                "product_count": len(reused.products),
                "query": intent.discovery_query,
                "intent_state": intent.state,
                "planning": _planning_payload(plan, reused.set_id),
            },
            user_message=message,
            error_code=None,
        )
    outcome = await m.run_agent_discovery(
        call.reasoning, intent, query_override=arguments.query
    )
    message = m.apply_discovery_outcome(call.record, intent, outcome)
    await call.db.commit()
    return AgentToolResult(
        tool_name="search_products",
        success=outcome.succeeded,
        result={
            "products": outcome.products_json,
            "product_count": len(outcome.products_json),
            "query": outcome.query,
            "intent_state": intent.state,
            "planning": {"action": plan.action, "reason": plan.reason},
        },
        user_message=message,
        error_code=None if outcome.succeeded else "discovery_failed",
    )


def _planning_payload(plan: SearchPlan, result_set_id: str) -> dict:
    return {
        "action": plan.action,
        "reason": plan.reason,
        "filters": [
            {
                "kind": spec.get("kind"),
                "value": str(spec.get("value"))
                if isinstance(spec.get("value"), Decimal)
                else spec.get("value"),
            }
            for spec in plan.filters
        ],
        "source_set_id": plan.source_set_id,
        "result_set_id": result_set_id,
    }


async def _clarify_variant(call: ToolCall) -> AgentToolResult:
    from app.routers import missions as m

    arguments: ClarifyVariantArgs = call.arguments  # type: ignore[assignment]
    response = await m.clarify_shopping_variant(
        call.mission_id,
        m.ClarifyVariantRequest(product_id=arguments.product_id, answer=arguments.answer),
        call.current_user,
        call.db,
    )
    return AgentToolResult(
        tool_name="clarify_variant",
        success=True,
        result={
            "resolved": response["resolved"],
            "requires_variant_clarification": response["requires_variant_clarification"],
            "variant_clarification": response["variant_clarification"],
            "product_id": response["product_id"],
        },
        user_message=response["agent_message"],
    )


async def _select_product(call: ToolCall) -> AgentToolResult:
    from app.routers import missions as m

    arguments: SelectProductArgs = call.arguments  # type: ignore[assignment]
    response = await m.select_shopping_product(
        call.mission_id,
        m.SelectProductRequest(product_id=arguments.product_id),
        call.current_user,
        call.db,
    )
    return AgentToolResult(
        tool_name="select_product",
        success=True,
        result={"selected_product": response["selected_product"]},
        user_message=response["agent_message"],
    )


async def _recommend_product(call: ToolCall) -> AgentToolResult:
    """
    Record which of the mission's own current products the agent is recommending.

    CORE-5 Phase 5 (§11's smallest capability): without a persisted, structured record of a
    recommendation, a later "the one you recommended" could never resolve deterministically.
    The product id is validated against the ACTIVE result set here, so the model cannot mint
    identity — it can only name a product the server already discovered.
    """
    from app.agents.conversation import append_event
    from app.agents.reference_resolution import (
        ANCHOR_EVENT_TYPE,
        ANCHOR_KIND_RECOMMENDATION,
        active_result_products,
    )
    from app.routers import missions as m

    arguments: RecommendProductArgs = call.arguments  # type: ignore[assignment]
    intent = m._intent_from_record(call.record)
    if intent is None:
        return _failure(
            "recommend_product",
            call.record,
            error_code="not_a_shopping_mission",
            user_message="Product recommendations only apply to a shopping mission.",
        )
    active = active_result_products(intent)
    if not active:
        return _failure(
            "recommend_product",
            call.record,
            error_code="no_active_results",
            user_message=(
                "This mission has no current results to recommend from — run a search first."
            ),
        )
    position = None
    match = None
    for index, product in enumerate(active, start=1):
        if product.product_id is not None and product.product_id == arguments.product_id:
            match, position = product, index
            break
    if match is None:
        return _failure(
            "recommend_product",
            call.record,
            error_code="not_in_current_results",
            user_message=(
                "That product is not part of this mission's current results, so no "
                "recommendation was recorded."
            ),
        )
    await append_event(
        call.db,
        mission_id=call.mission_id,
        user_id=call.record.user_id,
        role="system",
        event_type=ANCHOR_EVENT_TYPE,
        content=(
            f"Recorded a recommendation for the product at position {position} of the "
            "current results."
        ),
        payload={
            "kind": ANCHOR_KIND_RECOMMENDATION,
            "product_id": match.product_id,
            "set_id": intent.active_result_set_id,
        },
    )
    await call.db.commit()
    return AgentToolResult(
        tool_name="recommend_product",
        success=True,
        result={"recommended_product_id": match.product_id, "product_count": len(active)},
        user_message=(
            f"Recorded the recommendation for '{match.title}'. Nothing was selected, and no "
            "checkout review was prepared."
        ),
    )


async def _prepare_checkout(call: ToolCall) -> AgentToolResult:
    from app.routers import missions as m

    response = await m.get_shopping_checkout_review(
        call.mission_id, call.current_user, call.db
    )
    return AgentToolResult(
        tool_name="prepare_checkout",
        success=True,
        result={
            "checkout": response["checkout"],
            "already_confirmed_by_user": response["confirmed"],
        },
        user_message=(
            "Here is the checkout review for the product you selected. Nothing has been charged "
            "and no order exists: confirming it is a step only you can take."
        ),
    )


async def _report_issue(call: ToolCall) -> AgentToolResult:
    from app.routers import missions as m

    arguments: ReportIssueArgs = call.arguments  # type: ignore[assignment]
    response = await m.report_order_issue(
        call.mission_id,
        m.ReportOrderIssueRequest(
            issue_type=arguments.issue_type,
            description=arguments.description.strip(),
            requested_resolution=arguments.requested_resolution,
        ),
        call.current_user,
        call.db,
    )
    return AgentToolResult(
        tool_name="report_issue",
        success=True,
        result={
            "issue_id": response["issue_id"],
            "status": response["status"],
            "already_existed": response["already_existed"],
            "requested_resolution": response["requested_resolution"],
        },
        user_message=response["agent_message"],
    )


async def _assess_issue(call: ToolCall) -> AgentToolResult:
    from app.routers import missions as m

    issue = await _assessable_issue(call, m)
    if issue is None:
        return _failure(
            "assess_issue",
            call.record,
            error_code="nothing_to_assess",
            user_message=(
                "There is no reported issue on this mission waiting to be assessed — report one "
                "first."
            ),
        )
    if getattr(issue, "more_information_question", None):
        # The more-information round trip (Phase 13): the buyer has now answered, so their words
        # become part of the issue record and the same issue is assessed again through the same gate.
        # This resumes the mission instead of leaving it parked in a question.
        await m.attach_more_information_answer(issue, call.user_input, call.db)
    if await m._load_assessment(call.db, issue.issue_id) is not None:
        response = await m.reassess_issue(
            call.mission_id, issue.issue_id, call.current_user, call.db
        )
    else:
        response = await m.assess_order_issue(
            call.mission_id, issue.issue_id, call.current_user, call.db
        )
    return AgentToolResult(
        tool_name="assess_issue",
        success=True,
        result=_assessment_summary(response),
        user_message=response["agent_message"],
    )


async def _resolve_issue(call: ToolCall) -> AgentToolResult:
    from app.routers import missions as m

    arguments: ResolveIssueArgs = call.arguments  # type: ignore[assignment]
    issue = await _resolvable_issue(call, m, arguments.issue_id)
    if issue is None:
        return _failure(
            "resolve_issue",
            call.record,
            error_code="nothing_to_resolve",
            user_message=(
                "There is no assessed issue on this mission to resolve — assess one first."
            ),
        )
    response = await m.resolve_order_issue(call.mission_id, issue.issue_id, call.current_user, call.db)
    payload = _response_payload(response)
    gate = payload.get("authority_decision")
    state = payload.get("resolution_state")
    return AgentToolResult(
        tool_name="resolve_issue",
        success=state not in (None, "denied"),
        result={
            "issue_id": issue.issue_id,
            "action": payload.get("action"),
            "authority_decision": gate,
            "authority_reason": payload.get("authority_reason"),
            "resolution_state": state,
            "executor": payload.get("executor"),
            "provider_attempted": payload.get("provider_attempted"),
            "resolution_note": payload.get("resolution_note"),
        },
        user_message=payload.get("agent_message") or payload.get("resolution_note"),
        error_code=None if gate == "allow" else "authority_" + str(gate or "unknown"),
    )


async def _request_more_information(call: ToolCall) -> AgentToolResult:
    from app.routers import missions as m

    arguments: RequestMoreInformationArgs = call.arguments  # type: ignore[assignment]
    issue = await _issue_for_question(call, m, arguments.issue_id)
    question = arguments.question.strip()

    if issue is not None:
        # The state change belongs to the orchestrator, not to this tool: its persisted proposal
        # already says `request_more_information`, and the non-executing branch records exactly that
        # — no executor, no provider, one audit row. Only then is the question parked on the issue,
        # so a refusal here leaves nothing half-recorded.
        response = await m.resolve_order_issue(call.mission_id, issue.issue_id, call.current_user, call.db)
        payload = _response_payload(response)
        state = payload.get("resolution_state")
        if state != "more_information_required":
            return AgentToolResult(
                tool_name="request_more_information",
                success=False,
                result={"resolution_state": state},
                user_message=payload.get("agent_message") or payload.get("resolution_note"),
                error_code="issue_not_awaiting_information",
            )

    if issue is not None:
        m.record_more_information_request(call.record, issue, question)
        await call.db.commit()
        issue_id = issue.issue_id
    else:
        m.record_more_information_request(call.record, None, question)
        await call.db.commit()
        issue_id = None

    return AgentToolResult(
        tool_name="request_more_information",
        success=True,
        result={
            "issue_id": issue_id,
            "question": question,
            "awaiting_user": True,
            "resolution_state": "more_information_required" if issue_id else None,
        },
        user_message=question,
    )


async def _escalate_to_human(call: ToolCall) -> AgentToolResult:
    from app.routers import missions as m

    arguments: EscalateToHumanArgs = call.arguments  # type: ignore[assignment]
    issue = await _issue_for_question(call, m, arguments.issue_id)
    if issue is None:
        m.record_escalation_without_issue(call.record, arguments.reason)
        await call.db.commit()
        return AgentToolResult(
            tool_name="escalate_to_human",
            success=True,
            result={"issue_id": None, "issue_record": False},
            user_message=(
                "I have recorded your request for a human review, but there is no issue on this "
                "mission yet, so there is nothing for a reviewer to look at. No human has been "
                "contacted and no ticket number exists."
            ),
        )

    # Two existing operations, in this order: the customer's opt-out from automated decisions, then
    # the resolution flow, which honours that opt-out by recording an escalated outcome and contacting
    # no provider. Neither one is new logic, and the second is what makes the escalation a state the
    # mission carries rather than a sentence that evaporated.
    await m.request_manual_review(call.mission_id, issue.issue_id, call.current_user, call.db)
    response = await m.resolve_order_issue(call.mission_id, issue.issue_id, call.current_user, call.db)
    payload = _response_payload(response)
    return AgentToolResult(
        tool_name="escalate_to_human",
        success=True,
        result={
            "issue_id": issue.issue_id,
            "manual_review_requested": True,
            "resolution_state": payload.get("resolution_state"),
            "provider_attempted": payload.get("provider_attempted"),
            "executor": payload.get("executor"),
        },
        user_message=(
            "Your issue has been escalated for human review and recorded on the issue. The "
            "autonomous resolution path is paused for it; no human has reviewed it yet, and no "
            "support ticket number exists."
        ),
    )


# --------------------------------------------------------------------------- #
# Issue selection: always server-side, always checked against this mission.
# --------------------------------------------------------------------------- #


async def _load_mission_issues(mission_id: str, db: AsyncSession) -> list[OrderIssueRecord]:
    from app.routers import missions as m

    return await m._load_issues(db, mission_id)


async def _named_or_latest_issue(
    call: ToolCall, m: Any, issue_id: str | None
) -> OrderIssueRecord | None:
    """
    One of THIS mission's issues, chosen from its own rows.

    A supplied `issue_id` is only ever a lookup into this mission — `_load_issue` makes a foreign id
    indistinguishable from a missing one, so an agent cannot point a consequential tool at another
    mission's issue.
    """
    if issue_id:
        return await m._load_issue(call.db, call.mission_id, issue_id)
    issues = await _load_mission_issues(call.mission_id, call.db)
    return issues[-1] if issues else None


async def _assessable_issue(call: ToolCall, m: Any) -> OrderIssueRecord | None:
    """
    The issue an assessment is owed: never assessed, waiting on the buyer's answer, refused under
    a mandate that may since have changed, or escalated with an authority gap the owner may have
    closed by delegating (M22).

    The last two cases are what make a refusal recoverable at all. `reassess_issue` re-runs the
    same gate against the mandate as it reads now, so a remedy the user delegates after the
    refusal can still be delivered — and a remedy they never delegate is refused or escalated
    again, at the same cost of one audit row.
    """
    arguments: IssueScopedArgs = call.arguments  # type: ignore[assignment]
    if arguments.issue_id:
        return await m._load_issue(call.db, call.mission_id, arguments.issue_id)
    for issue in reversed(await _load_mission_issues(call.mission_id, call.db)):
        if getattr(issue, "more_information_question", None):
            return issue
        assessment = await m._load_assessment(call.db, issue.issue_id)
        if assessment is None:
            return issue
        resolution = await m._load_resolution_row(call.db, issue.issue_id)
        if resolution is not None and resolution.resolution_state == "denied":
            return issue
        if assessment.authority_decision == "escalate":
            return issue
    return None


async def _resolvable_issue(
    call: ToolCall, m: Any, issue_id: str | None
) -> OrderIssueRecord | None:
    """The newest issue that has a persisted proposal — the only kind `/resolve` will act on."""
    if issue_id:
        return await m._load_issue(call.db, call.mission_id, issue_id)
    for issue in reversed(await _load_mission_issues(call.mission_id, call.db)):
        if await m._load_assessment(call.db, issue.issue_id) is not None:
            return issue
    return None


async def _issue_for_question(
    call: ToolCall, m: Any, issue_id: str | None
) -> OrderIssueRecord | None:
    """
    The issue a question or an escalation belongs to, or None when the mission has no issue yet.

    Asking a human for a review, or asking the buyer for more detail, is meaningful before an
    issue row exists — so unlike the consequential tools this never fabricates an issue, it just
    records against the mission.
    """
    if issue_id:
        return await m._load_issue(call.db, call.mission_id, issue_id)
    issues = await _load_mission_issues(call.mission_id, call.db)
    return issues[-1] if issues else None


def _assessment_response_keys(response: dict) -> dict:
    return {
        key: response[key]
        for key in (
            "assessment_id",
            "issue_id",
            "proposed_action",
            "authority_decision",
            "decision_reason",
            "requested_amount",
            "currency",
            "proposal_source",
            "already_existed",
        )
        if key in response
    }


def _assessment_summary(response: dict) -> dict:
    """What the model may see afterwards about an assessment: the decision and its reason, no ids
    that could be replayed, no credentials."""
    summary = _assessment_response_keys(response)
    summary["execution_note"] = response.get("execution_note")
    return summary


def _response_payload(response: Any) -> dict:
    """A tool runner's return value is a dict or a JSONResponse; the loop needs its body."""
    from starlette.responses import JSONResponse

    if isinstance(response, dict):
        return response
    if isinstance(response, JSONResponse):
        return json.loads(response.body)
    return {"value": _text(getattr(response, "body", response))}


_register(
    "search_products",
    "Run product discovery for this shopping mission through the configured discovery provider. "
    "Optionally narrow it with a query; the mission's own persisted budget, currency, brand and "
    "preferences are what actually filter and price the results.",
    SearchProductsArgs,
    _search_products,
)

_register(
    "clarify_variant",
    "Answer a variant question the discovery provider asked about one of this mission's own "
    "discovered products, so the provider can narrow its catalog lookup.",
    ClarifyVariantArgs,
    _clarify_variant,
)

_register(
    "select_product",
    "Select one product from this mission's own discovered results. Records a selection only — "
    "no checkout, no payment, no order.",
    SelectProductArgs,
    _select_product,
)

_register(
    "recommend_product",
    "Record which of this mission's own current discovered products you are recommending, so "
    "later phrases like 'the one you recommended' resolve to it. Records a reference only — "
    "no selection, no checkout review, no payment.",
    RecommendProductArgs,
    _recommend_product,
)

_register(
    "prepare_checkout",
    "Show the server-derived checkout review for the selected product and move the mission to "
    "checkout_review. It cannot confirm the checkout and cannot start a payment: those are steps "
    "only the user takes.",
    PrepareCheckoutArgs,
    _prepare_checkout,
)

_register(
    "report_issue",
    "Record a post-purchase issue against this mission's confirmed RESOLVE order. A stated "
    "preferred resolution is stored as a preference and authorizes nothing.",
    ReportIssueArgs,
    _report_issue,
)

_register(
    "assess_issue",
    "Assess one reported issue on this mission and have the Authority Engine gate the resulting "
    "proposal. Proposes and records a decision only; nothing is executed.",
    ResolveIssueArgs,
    _assess_issue,
)

_register(
    "resolve_issue",
    "Run the autonomous resolution flow for one already-assessed issue on this mission. The "
    "action comes from the persisted proposal, the Authority Engine is re-evaluated right now, and "
    "this tool chooses nothing.",
    ResolveIssueArgs,
    _resolve_issue,
)

_register(
    "request_more_information",
    "Ask the user for a specific detail needed to continue with an issue, and record that the "
    "answer is outstanding. Contacts no provider and changes no resolution.",
    RequestMoreInformationArgs,
    _request_more_information,
)

_register(
    "escalate_to_human",
    "Record that an issue is escalated for human review. This pauses automated resolution for it; "
    "it does not create a support ticket and does not mean a human has looked at it.",
    EscalateToHumanArgs,
    _escalate_to_human,
)


async def _check_purchase_eligibility(call: ToolCall) -> AgentToolResult:
    from app.authority.engine import evaluate_purchase
    from app.routers import missions as m

    # Read-only by construction: this runner writes nothing — no record, no commit, no
    # trigger submission, no payment change. It reports what the deterministic purchase
    # gate would currently decide, and the user's own POST endpoint remains the only
    # purchase authorization boundary.
    order = await m._load_order(call.db, call.mission_id)
    if order is None:
        return AgentToolResult(
            tool_name="check_purchase_eligibility",
            success=True,
            result={"eligible": False, "reason": "No RESOLVE order exists for this mission yet."},
            user_message=(
                "This mission has no order record yet, so there is nothing to check for "
                "purchase eligibility."
            ),
        )
    existing = await m._load_purchase_authorization(call.db, order.order_id)
    decision = evaluate_purchase(m._purchase_facts(call.record, order, existing))
    return AgentToolResult(
        tool_name="check_purchase_eligibility",
        success=True,
        result={
            "eligible": decision.approved,
            "reason": decision.reason,
            "purchase_authorization_status": (
                existing.status if existing is not None else None
            ),
            # No linkage detail here on purpose: the gate itself refuses an already-linked
            # order through `eligible`/`reason`, which is the only linkage signal the agent
            # layer receives.
        },
        user_message=(
            f"The deterministic purchase gate reports: {decision.reason} This was a "
            "read-only check — it authorized nothing, submitted nothing to any merchant "
            "channel, and starting a purchase is a step only you can take."
        ),
    )


_register(
    "check_purchase_eligibility",
    "Report whether this mission's confirmed RESOLVE order currently passes the deterministic "
    "merchant-purchase gate — verified TEST payment evidence, complete merchant catalog "
    "identity, purchase authority enabled for the deployment, and no purchase already on "
    "record. Strictly read-only: it authorizes nothing, submits nothing to the merchant "
    "channel, and changes no state; starting a purchase is a step only the user takes.",
    CheckPurchaseEligibilityArgs,
    _check_purchase_eligibility,
)
