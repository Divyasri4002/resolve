"""
REST API for creating and driving missions.

This layer is intentionally thin: it validates input into the Pydantic models from
app/models/mission.py, persists state, and calls the agents / Authority Engine / audit logger.
It never makes an authority decision itself.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.buyer_agent import BuyerAgent
from app.agents.constraints import compose_intent_turn_input, merge_intent_turn
from app.agents.conversation import (
    append_event,
    append_lifecycle_event,
    as_client_transcript,
    latest_sequence,
    load_after,
    load_recent,
)
from app.agents.context import canonical_discovered_products
from app.agents.lifecycle import guarded_shopping_status
from app.agents.loop import AgentTurnOutcome, run_agent_turn
from app.agents.reasoning_provider import get_reasoning_provider
from app.agents.response_grounding import grounded_response
from app.agents.search_planning import query_from_constraints, record_search_set
from app.agents.tools import tool_definitions
from app.audit import logger as audit
from app.authority.engine import (
    AllowedAction,
    AuthorityDecision,
    BlockCategory,
    Decision,
    ProposedAction,
    PurchaseFacts,
    classify_block,
    evaluate,
    evaluate_purchase,
    refund_ceiling,
)
from app.config import get_settings
from app.database import get_db
from app.db_enforcement import require_protected_ops
from app.auth.dependencies import (
    get_current_user,
    get_current_user_optional,
    is_service_principal,
)
from app.ledger.verify import CURRENCY_NOT_CURRENT, submission_currency_for_app
from app.models.db_models import (
    MessageRecord,
    PurchaseAuthorizationRecord,
    ResolutionRecord,
    MissionRecord,
    OrderIssueRecord,
    RefundRecord,
    ReplacementRecord,
    ReturnRecord,
    ResolutionAssessmentRecord,
    ResolveOrderRecord,
    UserRecord,
)
from app.models.message import NegotiationMessage, MessageType, Party

logger = logging.getLogger("app.routers.missions")
from app.models.mission import (
    Authority,
    CheckoutConfirmation,
    EvidenceItem,
    IssueStatus,
    IssueType,
    Mission,
    MissionStatus,
    MissionType,
    Objective,
    PurchaseAuthorizationStatus,
    RecoveryMandate,
    RefundStatus,
    ReplacementStatus,
    ReturnStatus,
    RESOLUTION_ACTIONS,
    ResolutionProposal,
    ResolutionState,
    ShoppingIntent,
    ShoppingPayment,
    TransactionRef,
)
from app.models.product import (
    SHOPIFY_SOURCE,
    ProductResult,
    shopify_identity_problem,
)
from app.payments.razorpay_client import (
    create_payment_refund,
    create_test_order,
    fetch_refund,
    is_test_mode,
    list_refunds_for_payment,
    verify_payment_signature,
)
from app.resolution.issue_questions import normalize_answers, questions_payload
from app.resolution.orchestrator import (
    load_resolution as _load_resolution_row,
    resolution_response as _resolution_response,
    resolve_issue,
)
from app.routers.ws import broadcast_event_row
from app.tools.product_discovery import get_product_discovery_tool
from app.tools.product_search.base import ProductSearchError
from app.tools.purchase_trigger import (
    CONFIRMED_LINKED,
    NOT_SENT,
    REFUSED_AFTER_CREATE,
    REFUSED_BEFORE_CREATE,
    UNKNOWN,
    TriggerReport,
    submit_purchase,
)
from app.tools.replacement import (
    ReplacementError,
    ReplacementProviderUnavailable,
    ReplacementRequest,
    get_replacement_provider,
)
from app.tools.returns import (
    ReturnError,
    ReturnOutcomeUnknown,
    ReturnProviderUnavailable,
    ReturnRequest,
    ReturnSubmission,
    ReturnTarget,
    get_return_provider,
)

router = APIRouter(prefix="/missions", tags=["missions"])
settings = get_settings()


class CreateShoppingMissionRequest(BaseModel):
    prompt: str


class CreateMissionRequest(BaseModel):
    user_id: str = "demo-user"
    mission_type: str = "recovery"
    prompt: str | None = None
    objective: Objective | None = None
    authority: Authority | None = None
    transaction: TransactionRef | None = None
    shopping_intent: ShoppingIntent | None = None


class AgentMessageRequest(BaseModel):
    """User sends a message to the agent — the agent reasons about it in real-time."""
    user_input: str


def _verify_mission_access(record: MissionRecord, current_user: UserRecord | None) -> None:
    """
    Ensure the caller is authorized to access the requested mission:
    - Authenticated users can only access their own missions.
    - Unauthenticated callers can only access demo-user missions.
    """
    if current_user:
        if record.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Mission not found")
    else:
        if record.user_id != "demo-user":
            raise HTTPException(status_code=404, detail="Mission not found")


# --------------------------------------------------------------------------- #
# Shared shopping-step helpers (Task CORE-2).
#
# One discovery step, one status write, and one read of a mission's own post-purchase rows — the
# three things the creation path, the agent loop, and the agent's tools all need to do identically.
# They live here, next to the state they touch, because the endpoints and the tools must not grow
# separate versions of the same step.
# --------------------------------------------------------------------------- #

DISCOVERY_LIMIT = 5


@dataclass
class DiscoveryOutcome:
    """
    What one product-discovery step produced, with no opinion about how to say it.

    The mission's own state (`intent`) is updated by the step, because the provider's answer — not
    the caller's expectation — is what `discovered_products` and `state` must reflect.
    """

    succeeded: bool
    query: str | None
    products_json: list[dict]
    state: str
    status: str
    summary: str | None = None
    error: Exception | None = None

    @property
    def failure_message(self) -> str:
        category = self.query or "the products you asked about"
        return (
            f"Your intent is structured for {category}, but product search is temporarily "
            f"unavailable: {self.error}. Nothing was lost — ask again and the same search runs."
        )


async def run_agent_discovery(
    reasoning: object,
    intent: ShoppingIntent,
    *,
    query_override: str | None = None,
    limit: int = DISCOVERY_LIMIT,
) -> DiscoveryOutcome:
    """
    Run one discovery step through the configured provider and ground a summary in its real answer.

    The query is the only thing a caller may suggest, and it is built from the mission's own
    structured constraints — the caller's suggestion is a fallback for when the constraints
    alone say nothing, never a replacement for them. The mission's persisted budget, currency,
    brand, preferences and variant narrow and price the results — never the caller. On a
    provider failure nothing is attached to the intent and the mission parks in
    `ready_for_discovery` with the request preserved, which is the same degradation the
    shopping flow has always had; the previous result set, if any, stays exactly as it was.
    """
    query = query_from_constraints(intent, fallback=query_override) or ""
    tool = get_product_discovery_tool()
    try:
        products = await tool.search(
            query=query,
            budget=intent.budget,
            currency=intent.currency,
            preferences=intent.preferences,
            brand=intent.brand,
            size_or_variant=intent.size_or_variant,
            limit=limit,
        )
        products_json = [product.model_dump(mode="json") for product in products]
        # The summarize surface gets the SAME canonical projection the agent context shows, and
        # its sentence is verified against that evidence; the client-facing `products_json` keeps
        # the full product dumps untouched.
        summary_payload = canonical_discovered_products(products)
        summary = await reasoning.summarize_product_results(  # type: ignore[attr-defined]
            query, intent.budget, summary_payload
        )
        grounded = await grounded_response(
            reasoning,
            summary,
            user_request=query,
            products=summary_payload,
            relevant_context={"budget": intent.budget},
        )
        summary = grounded.message
    except Exception as exc:  # noqa: BLE001 — a provider outage is a state, never a 500
        intent.state = "ready_for_discovery"
        return DiscoveryOutcome(
            succeeded=False,
            query=query,
            products_json=[],
            state="ready_for_discovery",
            status=MissionStatus.READY_FOR_DISCOVERY.value,
            error=exc,
        )

    # One provider answer becomes one recorded result set: the previous set stays in the
    # history, this one becomes current, and discovered_products mirrors it for selection.
    record_search_set(intent, products, query)
    intent.discovery_query = query
    intent.state = "discovery_completed"
    return DiscoveryOutcome(
        succeeded=True,
        query=query,
        products_json=products_json,
        state="discovery_completed",
        status=MissionStatus.DISCOVERY_COMPLETED.value,
        summary=summary,
    )


def apply_discovery_outcome(
    record: MissionRecord, intent: ShoppingIntent, outcome: DiscoveryOutcome
) -> str:
    """
    Persist a discovery step onto the mission and return the sentence the user should get.

    The state write goes through the lifecycle guard, so a mission that has already paid or ordered
    keeps its status while the products themselves are refreshed.
    """
    _apply_conversational_state(record, intent, outcome.status)
    record.shopping_intent = intent.model_dump(mode="json")
    message = outcome.summary if outcome.succeeded else outcome.failure_message
    record.resolution_summary = message
    return message


def _intent_from_record(record: MissionRecord) -> ShoppingIntent | None:
    """The mission's persisted intent, or None for a mission that never had one."""
    data = record.shopping_intent
    if not data:
        return None
    return ShoppingIntent(**data)


def _set_conversational_status(record: MissionRecord, candidate: str) -> None:
    """Write a status a conversation is allowed to write — never a step backwards out of it."""
    record.status = guarded_shopping_status(record.status, candidate)


async def _get_mission_or_none(db: AsyncSession, mission_id: str) -> MissionRecord | None:
    return await db.get(MissionRecord, mission_id)


async def agent_mission_rows(
    db: AsyncSession, record: MissionRecord
) -> tuple[
    ResolveOrderRecord | None,
    str | None,
    OrderIssueRecord | None,
    ResolutionAssessmentRecord | None,
    ResolutionRecord | None,
    list[OrderIssueRecord],
    dict[str, dict[str, ResolutionAssessmentRecord | ResolutionRecord | None]],
]:
    """
    The mission's own post-purchase rows, as the agent sees them, plus the order's derived
    merchant-catalog state.

    'The issue this turn is about' is the newest one a reviewer or the buyer is still engaged with —
    an issue already verified, confirmed, escalated or deliberately closed is history, not the
    current thread. Nothing here reads across missions. The derived catalog state is computed here,
    in the layer that owns that vocabulary, because the agent's context builder is deliberately not
    given any.

    The seventh element carries every issue's own assessment and resolution, keyed by issue id, so
    the context can show each older issue's outcome (CORE-7 Phase 4) without the new-per-issue
    queries the loop would otherwise pay for.
    """
    order = await _load_order(db, record.mission_id)
    linkage = _merchant_order_linkage(order) if order is not None else None
    issues = await _load_issues(db, record.mission_id)
    settled = {
        ResolutionState.VERIFIED.value,
        ResolutionState.CONFIRMED.value,
        ResolutionState.ESCALATED.value,
        ResolutionState.NO_ACTION.value,
    }
    issue: OrderIssueRecord | None = None
    for candidate in reversed(issues):
        resolution_row = await _load_resolution_row(db, candidate.issue_id)
        if resolution_row is None or resolution_row.resolution_state not in settled:
            issue = candidate
            break
    if issue is None and issues:
        issue = issues[-1]
    assessment = await _load_assessment(db, issue.issue_id) if issue is not None else None
    resolution = await _load_resolution_row(db, issue.issue_id) if issue is not None else None

    assessments_result = await db.execute(
        select(ResolutionAssessmentRecord).where(
            ResolutionAssessmentRecord.mission_id == record.mission_id
        )
    )
    assessments_by_issue = {a.issue_id: a for a in assessments_result.scalars().all()}
    resolutions_result = await db.execute(
        select(ResolutionRecord).where(ResolutionRecord.mission_id == record.mission_id)
    )
    resolutions_by_issue = {r.issue_id: r for r in resolutions_result.scalars().all()}
    issue_outcomes = {
        row.issue_id: {
            "assessment": assessments_by_issue.get(row.issue_id),
            "resolution": resolutions_by_issue.get(row.issue_id),
        }
        for row in issues
    }
    return order, linkage, issue, assessment, resolution, issues, issue_outcomes


async def _process_new_shopping_mission(
    prompt: str,
    effective_user_id: str,
    db: AsyncSession,
) -> dict:
    reasoning = get_reasoning_provider(settings.reasoning_provider, settings.reasoning_api_key)
    agent_message: str | None = None

    # Reasoning provider callouts (Gemini/Groq) can be unreachable or rate-limited.
    # An outage must never surface as a 500: park the mission in a safe, retryable
    # state so the user's request is persisted, not lost.
    try:
        parsed_dict = await reasoning.interpret_shopping_intent(prompt)
        shopping_intent = ShoppingIntent(**parsed_dict)
        decision = await reasoning.decide_shopping_action(shopping_intent.model_dump(mode="json"))
    except Exception as exc:
        shopping_intent = ShoppingIntent(
            raw_prompt=prompt,
            state="needs_clarification",
            clarification_prompt="I couldn't reach the reasoning service just now. Please try again in a moment.",
        )
        decision = {}
        agent_message = f"Intent could not be structured yet - reasoning service temporarily unavailable: {exc}"

    if decision.get("action") == "search":
        tool_call = decision.get("tool_call") or {}
        outcome = await run_agent_discovery(
            reasoning,
            shopping_intent,
            query_override=tool_call.get("query") or prompt,
            limit=tool_call.get("limit", DISCOVERY_LIMIT),
        )
        status = MissionStatus(outcome.status)
        agent_message = outcome.summary if outcome.succeeded else outcome.failure_message
    else:
        status = MissionStatus.NEEDS_CLARIFICATION
        shopping_intent.state = "needs_clarification"
        if agent_message is None:
            agent_message = (
                decision.get("clarification_prompt") or shopping_intent.clarification_prompt
            )

    mandate = RecoveryMandate(
        mission_id="pending",
        objective=Objective(
            description=f"Shopping: {prompt}",
            success_criteria=shopping_intent.preferences,
            max_total_cost=shopping_intent.budget,
        ),
        authority=Authority(
            allowed_actions=[],
            recovery_budget=Decimal("0"),
        ),
    )
    transaction = TransactionRef(
        merchant_name="Pending Discovery",
        order_id="pending",
        amount_paid=Decimal("0"),
    )
    mission = Mission(
        user_id=effective_user_id,
        mission_type="shopping",
        mandate=mandate,
        transaction=transaction,
        status=status,
        shopping_intent=shopping_intent,
        resolution_summary=agent_message,
    )
    mission.mandate.mission_id = mission.mission_id

    record = MissionRecord(
        mission_id=mission.mission_id,
        user_id=mission.user_id,
        mission_type="shopping",
        status=mission.status.value,
        mandate=mission.mandate.model_dump(mode="json"),
        transaction=mission.transaction.model_dump(mode="json"),
        shopping_intent=mission.shopping_intent.model_dump(mode="json"),
        resolution_summary=agent_message,
        evidence=[],
    )
    db.add(record)
    await db.commit()

    return mission.to_intent_receipt()


@router.post("/shopping", response_model=dict)
async def create_shopping_mission(
    payload: CreateShoppingMissionRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    effective_user_id = current_user.id if current_user else "demo-user"
    return await _process_new_shopping_mission(payload.prompt, effective_user_id, db)


class SelectProductRequest(BaseModel):
    """
    The client sends ONLY the product identifier. Product details (price, merchant, ...)
    are never accepted from the client — the canonical ProductResult is resolved
    server-side from the mission's own persisted discovered_products.
    """

    product_id: str = Field(..., min_length=1)


@router.post("/{mission_id}/shopping/select-product", response_model=dict)
async def select_shopping_product(
    mission_id: str,
    payload: SelectProductRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Persist the user's choice of one discovered product on a shopping mission.

    Transitions the mission discovery_completed -> product_selected. This performs NO
    checkout, payment, or order creation — it only records the selection.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)

    if record.mission_type != "shopping":
        raise HTTPException(
            status_code=400, detail="Product selection is only available for shopping missions"
        )

    intent_data = dict(record.shopping_intent or {})
    discovered = intent_data.get("discovered_products") or []
    if not discovered:
        raise HTTPException(
            status_code=400,
            detail="No discovered products on this mission yet — run product discovery first",
        )

    canonical_data = next(
        (p for p in discovered if p.get("product_id") == payload.product_id), None
    )
    if canonical_data is None:
        raise HTTPException(
            status_code=400,
            detail="Product is not part of this mission's discovered results",
        )

    selected = ProductResult(**canonical_data)
    # A Shopify-sourced selection is only usable if its authoritative identity survived the round
    # trip through storage intact — a GID that got lost, truncated, or never verified is a reason
    # to stop, not a reason to proceed on the title alone.
    identity_problem = shopify_identity_problem(selected)
    if identity_problem is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "That product cannot be selected because its Shopify identity is unresolved: "
                f"{identity_problem}. Nothing is substituted for a variant Shopify did not name."
            ),
        )
    # Whether this call changes anything: re-selecting the product that is already selected while
    # the mission already sits in product_selected is a replay, and a replay writes no lifecycle
    # event. Captured BEFORE the mutation below.
    previous_selection = intent_data.get("selected_product") or {}
    selection_unchanged = (
        record.status == MissionStatus.PRODUCT_SELECTED.value
        and previous_selection.get("product_id") == payload.product_id
    )
    intent_data["selected_product"] = selected.model_dump(mode="json")
    intent_data["state"] = "product_selected"
    record.shopping_intent = intent_data
    record.status = MissionStatus.PRODUCT_SELECTED.value
    # The buyer chose this from this mission's own discovery; the price recorded is the provider's,
    # never a value the client sent. Inserted before the commit so the observation and the state
    # change are one atomic fact.
    await audit.append_event(
        db,
        mission_id=mission_id,
        actor="user",
        action="select_product",
        recorded=True,
        reason=(
            f"product {selected.product_id} ({selected.title}"
            f"{f' from {selected.merchant}' if selected.merchant else ''}) selected by the user "
            f"from this mission's discovered results at "
            f"{selected.extracted_price} {selected.currency or 'INR'}; no checkout, payment or "
            "order was created by this action"
        ),
    )
    lifecycle_row = None
    if not selection_unchanged:
        lifecycle_row = await append_lifecycle_event(
            db,
            mission_id=mission_id,
            user_id=record.user_id,
            state=MissionStatus.PRODUCT_SELECTED.value,
            detail=f"Product selected: {selected.title}.",
            extra_payload={"product_id": selected.product_id},
        )
    await db.commit()

    merchant_suffix = f" from {selected.merchant}" if selected.merchant else ""
    agent_message = f"Selected: {selected.title}{merchant_suffix}."

    # A replay of an unchanged selection appended no row and therefore announces nothing:
    # everything the stream can carry exists first as a committed conversation row.
    if lifecycle_row is not None:
        await broadcast_event_row(lifecycle_row)

    return {
        "mission_id": mission_id,
        "selected_product": selected.model_dump(mode="json"),
        "mission_status": record.status,
        "shopping_intent": record.shopping_intent,
        "agent_message": agent_message,
        "total_recovery_spend": "0",
    }


# A clarification re-read is about one product's variants, not a fresh shopping list, so it asks for
# fewer results than the original discovery and never more.
DISCOVERY_CLARIFICATION_LIMIT = 5


def _variant_option_summary(result: ProductResult) -> str:
    """
    Shopify's own option values for an unresolved variant, as one readable line.

    Every value here came from `selectedOptions` on a variant Shopify reported as available. When
    Shopify published nothing to choose between, it says so instead of inventing a fallback list.
    """
    clarification = result.variant_clarification
    if clarification is None or not clarification.options:
        return "no available Shopify variant options for this product"
    return ", ".join(f"{option.name}: {option.value}" for option in clarification.options)


class ClarifyVariantRequest(BaseModel):
    """
    The buyer's answer to a variant question, and which discovered product it answers (Task 14C.7E).

    Deliberately absent: any Shopify identifier, variant id, price, currency, merchant or title.
    A variant is not something a client may name — only the Shopify provider, reading Shopify's own
    catalog, can say which `ProductVariant` a value picks. `answer` is free text from the buyer.
    """

    model_config = ConfigDict(extra="ignore")

    product_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1, max_length=120)


@router.post("/{mission_id}/shopping/clarify-variant", response_model=dict)
async def clarify_shopping_variant(
    mission_id: str,
    payload: ClarifyVariantRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Re-ask the configured discovery provider about one ambiguous product, now with the buyer's answer.

    This is the round-trip for a Shopify result whose `requires_variant_clarification` is set: the
    answer becomes the intent's `size_or_variant`, the SAME provider seam that produced the list is
    called again, and Shopify's own catalog decides whether that value names exactly one variant.

    What it does not do: accept a variant id from the browser, let the reasoning model assign one,
    pick a variant "most like" the answer, or substitute a different variant when the answer matches
    none. If Shopify's catalog still does not single out one variant, the product stays
    unselectable and says why — an unanswered question is not a resolved one.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    intent_data = dict(record.shopping_intent or {})

    if intent_data.get("selected_product"):
        raise HTTPException(
            status_code=400,
            detail="A product is already selected on this mission, so there is no variant question left to answer.",
        )

    discovered = intent_data.get("discovered_products") or []
    if not discovered:
        raise HTTPException(
            status_code=400,
            detail="No discovered products on this mission yet — run product discovery first",
        )

    canonical = next(
        (p for p in discovered if p.get("product_id") == payload.product_id), None
    )
    if canonical is None:
        raise HTTPException(
            status_code=400,
            detail="Product is not part of this mission's discovered results",
        )

    pending = ProductResult(**canonical)
    if pending.source != SHOPIFY_SOURCE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Variant clarification is only available for Shopify-catalog products, and this "
                f"result came from {pending.source}. Nothing about it changed."
            ),
        )
    if not pending.requires_variant_clarification:
        raise HTTPException(
            status_code=400,
            detail=(
                "Shopify has already identified one variant for this product, so there is nothing "
                "to clarify — select it instead."
            ),
        )

    # Re-run the SAME discovery path the mission used the first time, with the buyer's answer as the
    # variant selector. The query, brand, preferences, budget and currency all come from the
    # persisted intent, so the only thing that changes is what the buyer just told RESOLVE.
    search_query = (
        intent_data.get("discovery_query")
        or intent_data.get("category")
        or intent_data.get("raw_prompt")
        or payload.product_id
    )
    budget = intent_data.get("budget")
    preferences = intent_data.get("preferences") or []
    brand = intent_data.get("brand")
    currency = intent_data.get("currency") or "INR"

    try:
        products = await get_product_discovery_tool().search(
            query=search_query,
            budget=Decimal(str(budget)) if budget is not None else None,
            currency=currency,
            preferences=preferences,
            brand=brand,
            size_or_variant=payload.answer,
            limit=DISCOVERY_CLARIFICATION_LIMIT,
        )
    except ProductSearchError as exc:
        # The provider could not be asked. The buyer's question stays open; nothing is invented.
        raise HTTPException(status_code=exc.status_code, detail=str(exc.message))

    intent_data["size_or_variant"] = payload.answer
    refreshed: ProductResult | None = None
    if products:
        # An empty re-run means the narrowed catalog question matched nothing. Replacing the buyer's
        # whole list with that would destroy information they never asked to lose, so the previous
        # results stay on the mission and the answer is recorded against them.
        intent_data["discovered_products"] = [
            p.model_dump(mode="json") for p in products
        ]
        intent_data["state"] = "discovery_completed"
        record.status = MissionStatus.DISCOVERY_COMPLETED.value
        refreshed = next(
            (p for p in products if p.product_id == pending.product_id), None
        )

    record.shopping_intent = intent_data
    await db.commit()

    resolved = refreshed is not None and not refreshed.requires_variant_clarification
    if resolved:
        agent_message = (
            f"Shopify identified exactly one variant of {refreshed.title} from "
            f"{payload.answer!r}. It can be selected now."
        )
    elif refreshed is None:
        agent_message = (
            "Shopify returned no catalog result for that clarification, so the products on this "
            f"mission are unchanged. Your wording was recorded as the requested variant for "
            f"{pending.title}, whose Shopify options remain: {_variant_option_summary(pending)}."
        )
    else:
        agent_message = (
            f"Shopify still cannot single out one variant of {refreshed.title} from "
            f"{payload.answer!r}. The options Shopify does publish are: "
            f"{_variant_option_summary(refreshed)}. Nothing was substituted for a variant Shopify "
            "did not name."
        )

    return {
        "mission_id": mission_id,
        "product_id": payload.product_id,
        "requires_variant_clarification": (
            refreshed.requires_variant_clarification if refreshed is not None else True
        ),
        "variant_clarification": (
            refreshed.variant_clarification.model_dump(mode="json")
            if refreshed is not None and refreshed.variant_clarification is not None
            else None
        ),
        "resolved": resolved,
        "mission_status": record.status,
        "shopping_intent": record.shopping_intent,
        "agent_message": agent_message,
        "total_recovery_spend": "0",
    }


# Task 06: quantity is fixed at exactly 1; quantity modification is a future task.
CHECKOUT_QUANTITY = 1

# The only currency this deployment's payment rail is configured to charge in (Task 14C.7D). A
# Shopify-native checkout carries the STORE's currency, which may be another one entirely; the
# payment step refuses instead of converting, because RESOLVE has no authority to invent an
# exchange rate and no business reason to charge an amount nobody quoted.
PAYMENT_CURRENCY = "INR"


def _derive_checkout_summary(selected: ProductResult) -> dict:
    """
    Build the checkout review entirely from the persisted, server-side selected_product.

    The client never supplies authoritative checkout values (title, price, merchant,
    currency, subtotal) — anything it sends is ignored and these canonical values are
    derived here instead.
    """
    return {
        "product": selected.model_dump(mode="json"),
        "quantity": CHECKOUT_QUANTITY,
        "subtotal": str(selected.extracted_price) if selected.extracted_price is not None else None,
        "currency": selected.currency or "INR",
        "payment_provider": "razorpay_test_mode",
        "payment_note": "Payment will be handled by Razorpay (test mode) in a future step. Nothing is charged at this stage.",
    }


async def _load_mission_for_shopping_action(
    mission_id: str,
    current_user: UserRecord | None,
    db: AsyncSession,
) -> MissionRecord:
    """Shared guard for shopping sub-actions: mission exists (404), caller owns it (404),
    and it is a shopping mission (400)."""
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)
    if record.mission_type != "shopping":
        raise HTTPException(
            status_code=400, detail="Checkout is only available for shopping missions"
        )
    return record


def _require_selected_product(record: MissionRecord) -> tuple[dict, ProductResult]:
    """Checkout requires a persisted server-side selection; the canonical ProductResult is
    rebuilt from what the backend stored, never from client input."""
    intent_data = dict(record.shopping_intent or {})
    selected_data = intent_data.get("selected_product")
    if not selected_data:
        raise HTTPException(
            status_code=400,
            detail="Select a product before starting checkout",
        )
    return intent_data, ProductResult(**selected_data)


@router.get("/{mission_id}/shopping/checkout", response_model=dict)
async def get_shopping_checkout_review(
    mission_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """
    Return the server-derived checkout review for the mission's selected product.

    Opening the review is a real, persisted stage: a mission in product_selected advances
    to checkout_review so a page refresh restores the review. The transition is idempotent
    (repeated GETs are no-ops) and invokes no payment provider, order API, or merchant call.

    ``request`` is the HTTP transport identity, injected over HTTP; in-process callers (the
    agent tools) leave it None, and a None request is by definition never a service call.
    The machine-to-machine service identity (the scratch purchase runner's read-only
    preflight) may read this payload with none of the user path's side effects — see
    _service_checkout_review.
    """
    if request is not None and is_service_principal(request):
        return await _service_checkout_review(mission_id, db)
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    intent_data, selected = _require_selected_product(record)

    if record.status == MissionStatus.PRODUCT_SELECTED.value:
        intent_data["state"] = "checkout_review"
        record.shopping_intent = intent_data
        record.status = MissionStatus.CHECKOUT_REVIEW.value
        await db.commit()

    return {
        "mission_id": mission_id,
        "checkout": _derive_checkout_summary(selected),
        "confirmed": (record.shopping_intent or {}).get("checkout") is not None,
        "mission_status": record.status,
        "shopping_intent": record.shopping_intent,
        "total_recovery_spend": "0",
    }


async def _service_checkout_review(mission_id: str, db: AsyncSession) -> dict:
    """
    The machine read of the checkout review: the same served payload with none of the user
    path's side effects — no product_selected -> checkout_review transition, no broadcast,
    no write of any kind. A preflight read must not advance anyone's checkout.

    Association is derived server-side, never from the caller: the machine reads a mission
    only when the mission's persisted selection belongs to this deployment's configured
    store. An unknown mission, a missing selection, or a selection from any other store
    (a SerpApi-sourced one has none) is one identical 404, so the machine cannot enumerate
    missions or read outside the store it fulfills for.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    intent_data = dict(record.shopping_intent or {})
    selected_data = intent_data.get("selected_product")
    if not isinstance(selected_data, dict) or not selected_data:
        raise HTTPException(status_code=404, detail="Mission not found")
    if selected_data.get("shopify_store_domain") != settings.shopify_store_domain:
        raise HTTPException(status_code=404, detail="Mission not found")
    return {
        "mission_id": mission_id,
        "checkout": _derive_checkout_summary(ProductResult(**selected_data)),
        "confirmed": (record.shopping_intent or {}).get("checkout") is not None,
        "mission_status": record.status,
        "shopping_intent": record.shopping_intent,
        "total_recovery_spend": "0",
    }


class ConfirmCheckoutRequest(BaseModel):
    """
    The confirmation body carries NO authoritative data. Product, price, merchant, quantity
    and subtotal are all derived server-side from the persisted selected_product; any
    client-supplied copies of those values are ignored, never trusted.
    """

    model_config = ConfigDict(extra="ignore")


@router.post("/{mission_id}/shopping/checkout/confirm", response_model=dict)
async def confirm_shopping_checkout(
    mission_id: str,
    payload: ConfirmCheckoutRequest | None = None,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Record the user's explicit confirmation of the checkout review.

    Transitions checkout_review -> checkout_confirmed and persists the derived confirmation
    so the state survives reloads and restarts. This is the payment boundary: NO Razorpay
    call, NO order creation, NO charge. The future payment task starts only from
    checkout_confirmed. Re-confirming is idempotent.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    intent_data, selected = _require_selected_product(record)

    # The persisted confirmation is the durable fact of "the user confirmed": if it exists the
    # call is a replay no matter how far the mission has moved on since. Keying the guard on
    # record.status instead would let a re-confirm from a later state re-execute the whole
    # transition — rewinding the mission and duplicating its evidence.
    already_confirmed = intent_data.get("checkout") is not None
    if not already_confirmed:
        confirmation = CheckoutConfirmation(
            quantity=CHECKOUT_QUANTITY,
            subtotal=selected.extracted_price,
            currency=selected.currency or "INR",
        )
        intent_data["checkout"] = confirmation.model_dump(mode="json")
        intent_data["state"] = "checkout_confirmed"
        record.shopping_intent = intent_data
        record.status = MissionStatus.CHECKOUT_CONFIRMED.value
        # The human confirmation boundary, recorded as it happened: who confirmed, what amount was
        # on the screen, and that nothing was charged by this call.
        await audit.append_event(
            db,
            mission_id=mission_id,
            actor="user",
            action="checkout_confirmed",
            recorded=True,
            reason=(
                f"checkout confirmed by the user for product {selected.product_id} "
                f"({selected.title}): quantity {CHECKOUT_QUANTITY}, subtotal "
                f"{confirmation.subtotal} {confirmation.currency}; this is the payment boundary — "
                "no Razorpay call, no order creation, no charge"
            ),
        )
        confirmation_row = await append_lifecycle_event(
            db,
            mission_id=mission_id,
            user_id=record.user_id,
            state=MissionStatus.CHECKOUT_CONFIRMED.value,
            detail=(
                f"Checkout confirmed for {selected.title} — {confirmation.subtotal} "
                f"{confirmation.currency}. Nothing has been charged."
            ),
            extra_payload={"status": "confirmed", "product_id": selected.product_id},
        )
        await db.commit()
        await broadcast_event_row(confirmation_row)

    merchant_suffix = f" from {selected.merchant}" if selected.merchant else ""
    agent_message = (
        f"Checkout confirmed for {selected.title}{merchant_suffix}. "
        "Ready for payment — the payment step itself will come in a future task. "
        "No payment has been made and no order has been created."
    )

    return {
        "mission_id": mission_id,
        "checkout": _derive_checkout_summary(selected),
        "confirmation": (record.shopping_intent or {}).get("checkout"),
        "mission_status": record.status,
        "shopping_intent": record.shopping_intent,
        "agent_message": agent_message,
        "total_recovery_spend": "0",
    }


# --------------------------------------------------------------------------- #
# Task 07: Razorpay TEST-MODE payment — order creation + server-side verification.
# TEST MODE only: real money is never involved.
# --------------------------------------------------------------------------- #


def _derive_payment_amount(checkout: dict, selected: ProductResult) -> Decimal:
    """The payable amount is exactly what the user confirmed at checkout — the persisted
    checkout confirmation subtotal — falling back to the selected product's server-side
    extracted price. The client never influences this value."""
    subtotal = checkout.get("subtotal")
    if subtotal is not None:
        return Decimal(str(subtotal))
    if selected.extracted_price is not None:
        return selected.extracted_price
    raise HTTPException(
        status_code=400, detail="No payable amount is available for this mission"
    )


def _payment_response(
    mission_id: str,
    record: MissionRecord,
    intent_data: dict,
    agent_message: str | None = None,
) -> dict:
    """Safe client-facing payment payload. Includes only the publishable key id checkout.js
    needs — the key secret and webhook secret never leave the backend."""
    return {
        "mission_id": mission_id,
        "payment": {
            **(intent_data.get("payment") or {}),
            "provider": "razorpay_test_mode",
            "test_mode": True,
            "key_id": settings.razorpay_key_id,
        },
        "mission_status": record.status,
        "shopping_intent": record.shopping_intent,
        "agent_message": agent_message,
        "total_recovery_spend": "0",
    }


@router.post("/{mission_id}/shopping/payment/create-order", response_model=dict)
async def create_shopping_payment_order(
    mission_id: str,
    payload: ConfirmCheckoutRequest | None = None,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Create (or reuse) the Razorpay TEST order for a checkout_confirmed shopping mission.

    The amount is derived exclusively from the persisted checkout confirmation and converted
    to paise with Decimal math (₹499 -> 49900). Duplicate protection is server-side: a mission
    already holding a pending order reuses it without a second Razorpay call; a verified
    payment refuses to create another order; a failed payment may begin one fresh attempt
    (the minimum failure handling). The client chooses nothing — not amount, currency, or
    order id.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    intent_data, selected = _require_selected_product(record)

    checkout = intent_data.get("checkout")
    if not checkout:
        raise HTTPException(status_code=400, detail="Confirm checkout before starting payment")

    payment_data = intent_data.get("payment")

    # Idempotency: an existing valid pending order is reused, never duplicated.
    if (
        payment_data
        and payment_data.get("status") == "pending"
        and payment_data.get("razorpay_order_id")
    ):
        return _payment_response(mission_id, record, intent_data)

    if payment_data and payment_data.get("status") == "verified":
        raise HTTPException(status_code=400, detail="Payment already verified for this mission")

    if record.status not in (
        MissionStatus.CHECKOUT_CONFIRMED.value,
        MissionStatus.PAYMENT_FAILED.value,
    ):
        raise HTTPException(
            status_code=400,
            detail="Payment can only start from checkout_confirmed",
        )

    amount = _derive_payment_amount(checkout, selected)
    currency = (checkout.get("currency") or selected.currency or "INR").upper()
    # Task 14C.7D boundary: a Shopify-native price arrives in the STORE's currency, and this
    # deployment's payment rail, authority ceilings and refund limits are all stated in INR.
    # Converting one to the other would be RESOLVE inventing an exchange rate, and charging an
    # amount nobody quoted. So the currency is checked, not coerced: discovery, selection and
    # checkout keep working in whatever the store sells in, and payment refuses here until a
    # deployment is actually configured for that currency.
    if currency != PAYMENT_CURRENCY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Payment cannot proceed: this checkout is priced in {currency} and the payment "
                f"rail is configured for {PAYMENT_CURRENCY}. RESOLVE does not convert a merchant's "
                "price into another currency, and does not charge an amount it has not been "
                "quoted. Nothing was created and no payment was attempted."
            ),
        )
    amount_paise = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    order = create_test_order(
        amount_paise=amount_paise,
        currency=currency,
        receipt=f"resolve_{mission_id}_payment",
        notes={"mission_id": mission_id, "flow": "shopping_test_mode"},
    )

    payment = ShoppingPayment(
        razorpay_order_id=order["id"],
        amount=amount,
        amount_paise=amount_paise,
        currency=currency,
        status="pending",
    )
    intent_data["payment"] = payment.model_dump(mode="json")
    intent_data["state"] = "payment_pending"
    record.shopping_intent = intent_data
    record.status = MissionStatus.PAYMENT_PENDING.value
    # TEST MODE only, and the amount is the server's own derived figure — the client named neither.
    await audit.append_event(
        db,
        mission_id=mission_id,
        actor="user",
        action="payment_order_created",
        recorded=True,
        reason=(
            f"Razorpay TEST order {order['id']} created for {amount_paise} paise "
            f"{currency} (derived server-side from the confirmed checkout, not from the request "
            "body); payment is pending and is not verified by this call"
        ),
    )
    pending_row = await append_lifecycle_event(
        db,
        mission_id=mission_id,
        user_id=record.user_id,
        state=MissionStatus.PAYMENT_PENDING.value,
        detail=(
            f"Razorpay TEST order created — payment pending for {amount} {currency}. "
            "Not verified yet."
        ),
        extra_payload={"status": "pending"},
    )
    await db.commit()
    # The frame is the committed row's own envelope: the conversation row is deliberately
    # narrow, and the Razorpay order id and money figures live in the audit log and the payment
    # response — never in a conversation row.
    await broadcast_event_row(pending_row)
    return _payment_response(mission_id, record, intent_data)


class VerifyPaymentRequest(BaseModel):
    """
    Razorpay checkout.js handler payload, submitted for SERVER-SIDE verification. These values
    are credentials to be checked, not proof of payment. The order id must equal the order this
    mission's backend created — a client cannot verify an arbitrary order/payment pair — and a
    valid HMAC signature is required before the mission may become payment_verified.
    """

    model_config = ConfigDict(extra="ignore")

    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/{mission_id}/shopping/payment/verify", response_model=dict)
async def verify_shopping_payment(
    mission_id: str,
    payload: VerifyPaymentRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Verify a Razorpay TEST payment and settle the mission state.

    Transitions payment_pending -> payment_verified only when the checkout signature
    (HMAC of order_id|payment_id with the backend-only secret) validates against this
    mission's persisted order. Order creation alone, a frontend callback, or a client
    "success" flag never produce payment_verified. An invalid signature persists
    payment_pending -> payment_failed and returns 400.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    intent_data, selected = _require_selected_product(record)

    payment_data = intent_data.get("payment")
    if not payment_data or payment_data.get("status") != "pending":
        raise HTTPException(
            status_code=400, detail="No pending payment to verify on this mission"
        )

    if payload.razorpay_order_id != payment_data.get("razorpay_order_id"):
        raise HTTPException(
            status_code=400,
            detail="Order id does not match this mission's payment",
        )

    if not verify_payment_signature(
        payload.razorpay_order_id,
        payload.razorpay_payment_id,
        payload.razorpay_signature,
    ):
        payment_data["status"] = "failed"
        payment_data["failure_reason"] = "signature_verification_failed"
        intent_data["payment"] = payment_data
        intent_data["state"] = "payment_failed"
        record.shopping_intent = intent_data
        record.status = MissionStatus.PAYMENT_FAILED.value
        # A refused verification is as auditable as an accepted one: the signature failed, the
        # mission moved to payment_failed, and no payment is claimed anywhere.
        await audit.append_event(
            db,
            mission_id=mission_id,
            actor="user",
            action="payment_verification_refused",
            recorded=False,
            reason=(
                f"signature verification refused for Razorpay TEST order "
                f"{payload.razorpay_order_id} with payment {payload.razorpay_payment_id}: "
                "the HMAC over order_id|payment_id did not match this deployment's secret; "
                "payment marked failed, nothing was charged or verified"
            ),
        )
        failed_row = await append_lifecycle_event(
            db,
            mission_id=mission_id,
            user_id=record.user_id,
            state=MissionStatus.PAYMENT_FAILED.value,
            detail=(
                "Payment failed — the signature did not verify. Nothing was charged or "
                "verified, and a fresh attempt may be started."
            ),
            extra_payload={"status": "failed", "reason": "signature_verification_failed"},
        )
        await db.commit()
        await broadcast_event_row(failed_row)
        raise HTTPException(
            status_code=400, detail="Payment signature verification failed"
        )

    payment_data["status"] = "verified"
    payment_data["razorpay_payment_id"] = payload.razorpay_payment_id
    payment_data["verified_at"] = datetime.utcnow().isoformat()
    intent_data["payment"] = payment_data
    intent_data["state"] = "payment_verified"
    record.shopping_intent = intent_data
    record.status = MissionStatus.PAYMENT_VERIFIED.value
    await audit.append_event(
        db,
        mission_id=mission_id,
        actor="user",
        action="payment_verified",
        recorded=True,
        reason=(
            f"payment {payload.razorpay_payment_id} on Razorpay TEST order "
            f"{payment_data['razorpay_order_id']} verified by server-side HMAC signature check "
            f"for {payment_data.get('amount')} {payment_data.get('currency')}; TEST MODE, no real "
            "money moved, and this is not merchant fulfillment"
        ),
    )
    verified_row = await append_lifecycle_event(
        db,
        mission_id=mission_id,
        user_id=record.user_id,
        state=MissionStatus.PAYMENT_VERIFIED.value,
        detail=(
            f"Payment verified in Razorpay TEST mode for {selected.title} — "
            f"{payment_data.get('amount')} {payment_data.get('currency')}. No real money moved."
        ),
        extra_payload={"status": "verified"},
    )
    await db.commit()
    # Provider references (order id, payment id) stay in the audit log and the payment record;
    # the conversation row carries only the state fact.
    await broadcast_event_row(verified_row)

    merchant_suffix = f" from {selected.merchant}" if selected.merchant else ""
    agent_message = (
        f"Payment verified in Razorpay TEST MODE for {selected.title}{merchant_suffix}. "
        "This was a test-mode transaction — no real money moved."
    )
    return _payment_response(mission_id, record, intent_data, agent_message=agent_message)


# --------------------------------------------------------------------------- #
# Task 08: Post-payment RESOLVE order lifecycle (order_created -> order_confirmed).
#
# Razorpay TEST payment verification is NOT merchant fulfillment — no merchant-order
# API exists, so these endpoints record RESOLVE's own order/payment lifecycle context
# and never claim shipping, delivery, or merchant confirmation. Every authoritative
# value (amount, currency, product identity, Razorpay ids, ownership) is copied from
# the mission's ALREADY-PERSISTED payment / selected_product state.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Merchant-order linkage is a FACT about the row, not a status: `provider_order_reference` is the
# single canonical source (14C.5) and is written by exactly one server-side function. Surfacing it
# here as a derived three-way state is therefore the smallest honest change — a stored duplicate of
# "is it linked yet?" would be a second copy of that fact, free to drift out of agreement with the
# column that decides it, and a return must never be aimed by a stale copy.
#
#   linked            a merchant order is paired, and Shopify confirmed it (plus, for a
#                     Shopify-native purchase, its own lines sell what RESOLVE sold)
#   awaiting_linkage  RESOLVE bought this from a merchant catalog it can name, so a merchant order
#                     is expected — and has not arrived. Payment is verified; that is not fulfillment
#   unlinked          no merchant order is paired, and RESOLVE holds no catalog identity for the
#                     purchase, so it cannot say one is coming either. Not a promise in either
#                     direction: only a signed, verified pairing changes this row
# --------------------------------------------------------------------------- #
MERCHANT_ORDER_LINKED = "linked"
MERCHANT_ORDER_AWAITING_LINKAGE = "awaiting_linkage"
MERCHANT_ORDER_UNLINKED = "unlinked"


def _merchant_order_linkage(order: ResolveOrderRecord) -> str:
    """Which of the three linkage states this persisted row is in. Reads no provider, writes nothing."""
    if order.provider_order_reference:
        return MERCHANT_ORDER_LINKED
    if order.shopify_product_gid and order.shopify_variant_gid and order.shopify_store_domain:
        return MERCHANT_ORDER_AWAITING_LINKAGE
    return MERCHANT_ORDER_UNLINKED


def _fulfillment_note(order: ResolveOrderRecord) -> str:
    """
    The one sentence that must never overstate. A verified test payment is not a merchant order, a
    merchant order is not a shipment, and a shipment is not a return — so each case says only itself.
    """
    if order.delivery_mode == DEMO_DELIVERY_MODE:
        return (
            "Delivered in the demo environment — RESOLVE's own simulated delivery persisted on "
            "reload. Not a carrier- or merchant-confirmed delivery, and no merchant order exists "
            "on record."
        )
    if order.provider_order_reference:
        return (
            "RESOLVE's order record after verified Razorpay TEST payment, paired with a merchant "
            "order this store confirmed. RESOLVE has not asserted shipping, delivery, or a return."
        )
    return (
        "This is RESOLVE's order record after verified Razorpay TEST payment — "
        "no merchant order, shipping, or delivery exists."
    )


DEMO_DELIVERY_MODE = "demo_simulated"


def _order_response(order: ResolveOrderRecord, agent_message: str | None = None) -> dict:
    """Canonical client-facing order payload — every value read from the persisted row."""
    return {
        "order_id": order.order_id,
        "mission_id": order.mission_id,
        "status": order.status,
        "product": {
            "product_id": order.product_id,
            "title": order.title,
            "merchant": order.merchant,
        },
        "amount": str(order.amount),
        "currency": order.currency,
        "razorpay_order_id": order.razorpay_order_id,
        "razorpay_payment_id": order.razorpay_payment_id,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "delivery_mode": order.delivery_mode,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        # Derived from the persisted linkage column, and nothing else. The merchant-side order id
        # itself is deliberately NOT sent: the client has no use for it and no way to authorise it,
        # and a value the browser holds is a value the browser can try to hand back.
        "merchant_order_linked": order.provider_order_reference is not None,
        "merchant_order_linkage": _merchant_order_linkage(order),
        "test_mode": True,
        "fulfillment_note": _fulfillment_note(order),
        "agent_message": agent_message,
    }


async def _load_order(db: AsyncSession, mission_id: str) -> ResolveOrderRecord | None:
    result = await db.execute(
        select(ResolveOrderRecord).where(ResolveOrderRecord.mission_id == mission_id)
    )
    return result.scalar_one_or_none()


@router.get("/{mission_id}/order", response_model=dict)
async def get_resolve_order(
    mission_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """
    Return the mission's persisted RESOLVE order record, or 404 if none exists.

    ``request`` is the HTTP transport identity, injected over HTTP; in-process callers leave
    it None, and a None request is by definition never a service call. The
    machine-to-machine service identity (the scratch purchase runner's read-only preflight)
    may read this payload — see _service_resolve_order.
    """
    if request is not None and is_service_principal(request):
        return await _service_resolve_order(mission_id, db)
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    if (
        settings.shopping_demo_delivery_enabled
        and order.status == MissionStatus.ORDER_CONFIRMED.value
    ):
        order = await _persist_demo_delivery(record, order, db)
    return _order_response(order)


async def _persist_demo_delivery(
    record: MissionRecord, order: ResolveOrderRecord, db: AsyncSession
) -> ResolveOrderRecord:
    """
    The demo-environment delivery transition (M22 §1): a customer's reload of a confirmed order
    persists `delivered`. Guards, in order — an unverified/unpaid order never changes status, and
    an order on the merchant path is never demo-delivered:

      - the persisted payment must still be verified against this order's OWN Razorpay references
        (the same pairing proof the purchase gate uses);
      - no merchant-purchase authorization claim may exist for the order, whatever its status —
        any claim means the merchant channel owns this order's outcome, and a simulated delivery
        must never race or overwrite a real pairing.

    The write itself is a conditional UPDATE gated on the row still being order_confirmed, so two
    concurrent reloads persist exactly one transition, one audit event and one lifecycle event;
    the loser re-reads the winner's committed row. The mode label travels with the row forever,
    and the delivery state stays out of the conversational lifecycle ladder (lifecycle.py), so
    only this endpoint can ever produce it.
    """
    intent_data = dict(record.shopping_intent or {})
    payment = intent_data.get("payment") or {}
    if not (
        payment.get("status") == "verified"
        and payment.get("razorpay_order_id") == order.razorpay_order_id
        and payment.get("razorpay_payment_id") == order.razorpay_payment_id
    ):
        return order
    if await _load_purchase_authorization(db, order.order_id) is not None:
        return order

    now = datetime.utcnow()
    result = await db.execute(
        update(ResolveOrderRecord)
        .where(
            ResolveOrderRecord.mission_id == order.mission_id,
            ResolveOrderRecord.status == MissionStatus.ORDER_CONFIRMED.value,
        )
        .values(
            status=MissionStatus.DELIVERED.value,
            delivered_at=now,
            delivery_mode=DEMO_DELIVERY_MODE,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        # A concurrent reload won the transition. Discard this transaction's failed UPDATE and
        # re-read the winner's committed row; append nothing.
        await db.rollback()
        await db.refresh(order)
        return order

    await db.refresh(order)
    intent_data["state"] = MissionStatus.DELIVERED.value
    record.shopping_intent = intent_data
    record.status = MissionStatus.DELIVERED.value
    await audit.append_event(
        db,
        mission_id=order.mission_id,
        actor="system",
        action="order_delivered",
        recorded=True,
        reason=(
            f"Demo-environment delivery persisted for RESOLVE order {order.order_id} on the "
            f"customer's reload (delivery_mode={DEMO_DELIVERY_MODE}); RESOLVE's own simulated "
            "transition, not a carrier or merchant confirmation"
        ),
    )
    delivered_row = await append_lifecycle_event(
        db,
        mission_id=order.mission_id,
        user_id=record.user_id,
        state=MissionStatus.DELIVERED.value,
        detail=f"Order {order.order_id} delivered (demo simulated).",
        extra_payload={
            "status": MissionStatus.DELIVERED.value,
            "order_id": order.order_id,
            "delivery_mode": DEMO_DELIVERY_MODE,
        },
    )
    await db.commit()
    await broadcast_event_row(delivered_row)
    return order


async def _service_resolve_order(mission_id: str, db: AsyncSession) -> dict:
    """
    The machine read of the canonical order row, associated server-side: the order is loaded
    BY mission id (the association the runner must prove, not one it may assert) and served
    only when the row's own Shopify store identity matches this deployment's configured
    store — a value only the application writes when it persists the selection's catalog
    identity. An unknown mission, a missing order, or an order without that identity (a
    SerpApi-sourced order has none) is one identical 404.
    """
    order = await _load_order(db, mission_id)
    if order is None or order.shopify_store_domain != settings.shopify_store_domain:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    return _order_response(order)


@router.post("/{mission_id}/order", response_model=dict)
async def create_resolve_order(
    mission_id: str,
    payload: dict | None = None,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Create the persistent RESOLVE order record for a payment_verified shopping mission.

    Deterministic and idempotent: exactly one order row exists per mission (mission_id
    is unique), so the first call creates it and every later call — browser repeat,
    page refresh, backend restart — returns the same persisted order without a new
    write. The transition requires payment_verified and copies the canonical amount,
    currency, product identity, and Razorpay references from the already-persisted
    payment/selection state. The request body (anything the client sends, e.g. a
    tampered amount or razorpay ids) is ignored — nothing client-supplied is
    authoritative here.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    intent_data, selected = _require_selected_product(record)

    existing = await _load_order(db, mission_id)
    if existing is not None:
        return _order_response(existing)

    payment_data = intent_data.get("payment") or {}
    if (
        record.status != MissionStatus.PAYMENT_VERIFIED.value
        or payment_data.get("status") != "verified"
    ):
        raise HTTPException(
            status_code=400,
            detail="Order can only be created after payment is verified",
        )

    order = ResolveOrderRecord(
        mission_id=mission_id,
        user_id=record.user_id,
        product_id=selected.product_id,
        title=selected.title,
        merchant=selected.merchant,
        product=selected.model_dump(mode="json"),
        amount=Decimal(str(payment_data["amount"])),
        currency=(payment_data.get("currency") or selected.currency or "INR").upper(),
        razorpay_order_id=payment_data["razorpay_order_id"],
        razorpay_payment_id=payment_data["razorpay_payment_id"],
        # Shopify catalog identity, read off the canonical selected result the SERVER materialised
        # from its own persisted discovery. Not from `payload`, not from the reasoning model, and
        # not re-derived from the title or price below.
        shopify_product_gid=selected.shopify_product_gid,
        shopify_variant_gid=selected.shopify_variant_gid,
        shopify_store_domain=selected.shopify_store_domain,
        status="order_created",
    )
    db.add(order)
    await db.flush()

    intent_data["state"] = "order_created"
    record.shopping_intent = intent_data
    record.status = MissionStatus.ORDER_CREATED.value
    # The order row exists because a verified TEST payment exists; every field above was copied
    # from persisted state, so the audit row says where each value came from.
    await audit.append_event(
        db,
        mission_id=mission_id,
        actor="user",
        action="order_created",
        recorded=True,
        reason=(
            f"RESOLVE order {order.order_id} created for product {selected.product_id} at "
            f"{order.amount} {order.currency} against verified Razorpay TEST payment "
            f"{order.razorpay_payment_id}; RESOLVE's own order record, not a merchant order"
        ),
    )
    created_row = await append_lifecycle_event(
        db,
        mission_id=mission_id,
        user_id=record.user_id,
        state=MissionStatus.ORDER_CREATED.value,
        detail=f"Order created for {selected.title} — {order.amount} {order.currency}.",
        extra_payload={"status": "order_created", "order_id": order.order_id},
    )
    await db.commit()

    await broadcast_event_row(created_row)

    merchant_suffix = f" from {order.merchant}" if order.merchant else ""
    agent_message = (
        f"RESOLVE order record {order.order_id} created for {order.title}{merchant_suffix} "
        f"— {order.amount} {order.currency}, paid via Razorpay TEST MODE. "
        "This is RESOLVE's order context after the verified test payment, not a merchant order."
    )
    return _order_response(order, agent_message=agent_message)


@router.post("/{mission_id}/order/confirm", response_model=dict)
async def confirm_resolve_order(
    mission_id: str,
    payload: dict | None = None,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Confirm the RESOLVE order record: order_created -> order_confirmed.

    This is the user's explicit confirmation of RESOLVE's own order record — the final
    state of this foundation. It implies no merchant fulfillment: no merchant order,
    shipping, delivery, or tracking exists or is claimed. Idempotent: confirming an
    already-confirmed order returns the same order unchanged.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(
            status_code=400, detail="Create the order before confirming it"
        )
    if order.status == "order_confirmed":
        return _order_response(order)
    if order.status != "order_created":
        raise HTTPException(
            status_code=400, detail="Order cannot be confirmed from its current state"
        )

    order.status = "order_confirmed"
    intent_data = dict(record.shopping_intent or {})
    intent_data["state"] = "order_confirmed"
    record.shopping_intent = intent_data
    record.status = MissionStatus.ORDER_CONFIRMED.value
    await audit.append_event(
        db,
        mission_id=mission_id,
        actor="user",
        action="order_confirmed",
        recorded=True,
        reason=(
            f"RESOLVE order {order.order_id} confirmed by the user at {order.amount} "
            f"{order.currency}; no merchant order, shipping, delivery or tracking exists or is "
            "claimed by this confirmation"
        ),
    )
    confirmed_row = await append_lifecycle_event(
        db,
        mission_id=mission_id,
        user_id=record.user_id,
        state=MissionStatus.ORDER_CONFIRMED.value,
        detail=f"Order {order.order_id} confirmed.",
        extra_payload={"status": "order_confirmed", "order_id": order.order_id},
    )
    await db.commit()

    await broadcast_event_row(confirmed_row)

    agent_message = (
        f"RESOLVE order {order.order_id} confirmed. This completes RESOLVE's order record "
        "for the verified test payment — no merchant fulfillment exists yet."
    )
    return _order_response(order, agent_message=agent_message)


# --------------------------------------------------------------------------- #
# Buyer-flow Phase 3 — merchant purchase authorization.
#
# The user's own POST is the ONLY purchase authorization boundary in the system:
# place_merchant_order is deliberately not part of AllowedAction, so the reasoning model
# can neither propose nor authorize a purchase, and this endpoint is the only caller of
# the deterministic purchase gate (evaluate_purchase). The authorization claim is
# persisted BEFORE the trigger is called; the standalone trigger's answer is recorded as
# an untrusted report beside the authoritative linkage facts that live on the order row.
# --------------------------------------------------------------------------- #

# The only order states a purchase may be authorized against. order_confirmed is the
# user's explicit confirmation of the order record; an unconfirmed order_created has not
# passed that boundary yet.
PURCHASE_ELIGIBLE_ORDER_STATES = frozenset({"order_confirmed"})


async def _load_purchase_authorization(
    db: AsyncSession, order_id: str
) -> PurchaseAuthorizationRecord | None:
    result = await db.execute(
        select(PurchaseAuthorizationRecord)
        .where(PurchaseAuthorizationRecord.order_id == order_id)
        # Concurrent submissions mutate this row through other sessions, so a re-load
        # must never answer from stale identity-map attributes.
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def _purchase_facts(
    record: MissionRecord,
    order: ResolveOrderRecord,
    existing: PurchaseAuthorizationRecord | None,
) -> PurchaseFacts:
    """Every gate input, derived from persisted rows only — never from a request body."""
    intent_data = dict(record.shopping_intent or {})
    payment = intent_data.get("payment") or {}
    payment_verified = bool(
        payment.get("status") == "verified"
        and payment.get("razorpay_order_id") == order.razorpay_order_id
        and payment.get("razorpay_payment_id") == order.razorpay_payment_id
    )
    payment_amount = payment.get("amount")
    amount_matches = bool(
        payment_verified
        and payment_amount is not None
        and Decimal(str(payment_amount)) == Decimal(str(order.amount))
    )
    return PurchaseFacts(
        purchase_authority_enabled=settings.shopping_purchase_authority_enabled,
        test_mode=is_test_mode(),
        order_belongs_to_mission=order.mission_id == record.mission_id,
        order_state_eligible=order.status in PURCHASE_ELIGIBLE_ORDER_STATES,
        merchant_identity_complete=bool(
            order.shopify_product_gid
            and order.shopify_variant_gid
            and order.shopify_store_domain
        ),
        already_linked=order.provider_order_reference is not None,
        canonical_amount=Decimal(str(order.amount)),
        currency=order.currency,
        payment_evidence_verified=payment_verified,
        payment_amount_matches_order=amount_matches,
        prior_authorization_state=(
            existing.status if existing is not None else None
        ),
    )


# Submission-lease states (Phase 4.4): 'idle' admits exactly one owner; 'in_flight' means
# one accepted attempt may be running; 'settled' means a real report was applied or crash
# recovery closed the claim.
SUBMISSION_IDLE = "idle"
SUBMISSION_IN_FLIGHT = "in_flight"
SUBMISSION_SETTLED = "settled"

# An in_flight lease older than the trigger client's own submit timeout (30s) plus margin
# was abandoned by a crashed backend. From persisted state alone this backend cannot prove
# the attempt was never transmitted, so recovery treats it as potentially sent and closes
# the claim as purchase_unknown (terminal) — it never resets the lease to idle and no
# submission is ever started again behind it.
_LEASE_STALE_AFTER_SECONDS = 90.0

# The evidence-bearing classifications and the one terminal status each justifies. NOT_SENT
# is deliberately absent: it carries no evidence and changes no status.
_REPORT_STATUS_BY_CLASSIFICATION = {
    CONFIRMED_LINKED: PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value,
    REFUSED_BEFORE_CREATE: PurchaseAuthorizationStatus.PURCHASE_FAILED.value,
    REFUSED_AFTER_CREATE: PurchaseAuthorizationStatus.LINKAGE_REFUSED.value,
    UNKNOWN: PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value,
}


async def _acquire_submission_lease(
    db: AsyncSession, authorization_id: str
) -> str | None:
    """Atomically take the submission lease and count the accepted attempt.

    One conditional UPDATE against the committed row decides the single owner: the
    database row lock arbitrates concurrent requests (and replicas), so at most one
    caller ever transitions idle -> in_flight. The attempt_count increment is part of
    the same statement — an accepted attempt is counted exactly once, by its owner, and
    a lost race never inflates the count. Commits before returning so ownership is
    visible to every other request while the trigger runs. Returns the lease token, or
    None when another owner holds it.
    """
    token = uuid.uuid4().hex
    result = await db.execute(
        update(PurchaseAuthorizationRecord)
        .where(
            PurchaseAuthorizationRecord.authorization_id == authorization_id,
            PurchaseAuthorizationRecord.status
            == PurchaseAuthorizationStatus.AUTHORIZED.value,
            PurchaseAuthorizationRecord.submission_state == SUBMISSION_IDLE,
        )
        .values(
            submission_state=SUBMISSION_IN_FLIGHT,
            submission_owner=token,
            attempt_count=PurchaseAuthorizationRecord.attempt_count + 1,
            last_attempt_at=datetime.utcnow(),
        )
    )
    await db.commit()
    if result.rowcount != 1:
        return None
    return token


async def _recover_abandoned_submission(
    db: AsyncSession, authorization: PurchaseAuthorizationRecord
) -> bool:
    """Close a stale in_flight lease as purchase_unknown — fail-closed and terminal.

    A lease abandoned by a crashed backend is potentially sent: persisted state cannot
    prove the attempt never reached the trigger, so the claim closes as purchase_unknown —
    never idle, never purchase_failed — and no second submission is ever started for it.
    The one atomic UPDATE matches only the in_flight row the recorded owner still holds,
    so a live owner that already applied its report can never be overwritten here.
    Returns True when this call performed the recovery.
    """
    if (
        authorization.submission_state != SUBMISSION_IN_FLIGHT
        or authorization.last_attempt_at is None
        or datetime.utcnow() - authorization.last_attempt_at
        < timedelta(seconds=_LEASE_STALE_AFTER_SECONDS)
    ):
        return False
    result = await db.execute(
        update(PurchaseAuthorizationRecord)
        .where(
            PurchaseAuthorizationRecord.authorization_id
            == authorization.authorization_id,
            PurchaseAuthorizationRecord.status
            == PurchaseAuthorizationStatus.AUTHORIZED.value,
            PurchaseAuthorizationRecord.submission_state == SUBMISSION_IN_FLIGHT,
            PurchaseAuthorizationRecord.submission_owner
            == authorization.submission_owner,
        )
        .values(
            status=PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value,
            submission_state=SUBMISSION_SETTLED,
            submission_owner=None,
            error_note=(
                "a prior submission attempt was in flight when its backend stopped "
                "responding; whether it reached the trigger is unknown, so recovery "
                "closed the claim as unknown — it was never automatically retried"
            ),
        )
    )
    await db.commit()
    await db.refresh(authorization)
    return result.rowcount == 1


async def _release_submission_lease(
    db: AsyncSession, authorization_id: str, token: str, *, note: str | None = None
) -> None:
    """Release the lease after a submission that definitively never ran (owner only).

    Only the live owner can know the attempt never started — the trigger client never
    raises for a submission-level failure, so an exception here happened before anything
    was sent. The conditional UPDATE matches only the in_flight row this token still
    owns, so a stale owner can never release anything. Does not commit: the caller's
    transaction carries the release and its note together.
    """
    values: dict[str, object] = {
        "submission_state": SUBMISSION_IDLE,
        "submission_owner": None,
    }
    if note is not None:
        values["error_note"] = note
    await db.execute(
        update(PurchaseAuthorizationRecord)
        .where(
            PurchaseAuthorizationRecord.authorization_id == authorization_id,
            PurchaseAuthorizationRecord.submission_owner == token,
            PurchaseAuthorizationRecord.submission_state == SUBMISSION_IN_FLIGHT,
        )
        .values(**values)
    )


async def _apply_trigger_report(
    db: AsyncSession,
    authorization: PurchaseAuthorizationRecord,
    lease_token: str,
    report: TriggerReport,
) -> bool:
    """Apply the trigger's untrusted report under verified lease ownership.

    Monotonic by construction: the UPDATE only matches the row this request still owns
    (same lease token, still in_flight, still authorized), so a late or stale report can
    never overwrite a settled outcome and a terminal status can never be downgraded.
    NOT_SENT carries no evidence: it writes only error_note and releases the lease to
    idle, leaving the claim resubmittable and any prior report untouched. Real outcomes
    write every trigger_reported_* field verbatim, clear error_note (a stale not_sent
    note must not survive a real outcome), stamp last_submission_at, and settle the
    lease. Returns whether this call's report was applied; either way the instance is
    refreshed from the row. Does not commit: the caller's transaction carries the apply,
    its audit event and its lifecycle event together.
    """
    status_value = _REPORT_STATUS_BY_CLASSIFICATION.get(report.classification)
    still_owned = (
        PurchaseAuthorizationRecord.authorization_id == authorization.authorization_id,
        PurchaseAuthorizationRecord.submission_owner == lease_token,
        PurchaseAuthorizationRecord.submission_state == SUBMISSION_IN_FLIGHT,
        PurchaseAuthorizationRecord.status
        == PurchaseAuthorizationStatus.AUTHORIZED.value,
    )
    if status_value is None:  # NOT_SENT — no evidence, no status change
        result = await db.execute(
            update(PurchaseAuthorizationRecord)
            .where(*still_owned)
            .values(
                submission_state=SUBMISSION_IDLE,
                submission_owner=None,
                error_note=report.reason,
            )
        )
    else:
        result = await db.execute(
            update(PurchaseAuthorizationRecord)
            .where(*still_owned)
            .values(
                status=status_value,
                submission_state=SUBMISSION_SETTLED,
                submission_owner=None,
                trigger_reported_outcome_class=report.outcome_class,
                trigger_reported_outcome_status=report.outcome_status,
                trigger_reported_correlation_id=report.correlation_id,
                trigger_reported_external_order_id=report.external_order_id,
                trigger_reported_event_id=report.event_id,
                trigger_reported_reason=report.reason,
                last_submission_at=datetime.utcnow(),
                error_note=None,
            )
        )
    applied = result.rowcount == 1
    await db.refresh(authorization)
    return applied


def _purchase_authorization_response(
    authorization: PurchaseAuthorizationRecord, order: ResolveOrderRecord
) -> dict:
    trigger_report = None
    if (
        authorization.trigger_reported_outcome_class
        or authorization.error_note
        or authorization.trigger_reported_reason
    ):
        trigger_report = {
            "outcome_class": authorization.trigger_reported_outcome_class,
            "outcome_status": authorization.trigger_reported_outcome_status,
            "correlation_id": authorization.trigger_reported_correlation_id,
            "external_order_id": authorization.trigger_reported_external_order_id,
            "event_id": authorization.trigger_reported_event_id,
            "reason": authorization.trigger_reported_reason,
            "note": (
                "Untrusted report from the merchant purchase trigger — never proof of a "
                "merchant order. The authoritative linkage is merchant_order_linkage below."
            ),
        }
    return {
        "authorization_id": authorization.authorization_id,
        "order_id": authorization.order_id,
        "mission_id": authorization.mission_id,
        "status": authorization.status,
        "authority_decision": authorization.authority_decision,
        "decision_reason": authorization.decision_reason,
        "attempt_count": authorization.attempt_count,
        "submission_state": authorization.submission_state,
        "created_at": (
            authorization.created_at.isoformat() if authorization.created_at else None
        ),
        "updated_at": (
            authorization.updated_at.isoformat() if authorization.updated_at else None
        ),
        "last_attempt_at": (
            authorization.last_attempt_at.isoformat()
            if authorization.last_attempt_at
            else None
        ),
        "last_submission_at": (
            authorization.last_submission_at.isoformat()
            if authorization.last_submission_at
            else None
        ),
        "error_note": authorization.error_note,
        "trigger_report": trigger_report,
        # Authoritative merchant linkage, derived from the order row and nothing else.
        "merchant_order_linked": order.provider_order_reference is not None,
        "merchant_order_linkage": _merchant_order_linkage(order),
        "fulfillment_note": _fulfillment_note(order),
    }


def _purchase_submission_note(authorization: PurchaseAuthorizationRecord) -> str:
    status = authorization.status
    if status == PurchaseAuthorizationStatus.PURCHASE_REPORTED_LINKED.value:
        return (
            "Purchase submitted — the trigger reported a confirmed linkage. That is a "
            "report, not proof: the order is linked only when the authoritative linkage "
            "record on the order says so."
        )
    if status == PurchaseAuthorizationStatus.PURCHASE_FAILED.value:
        return (
            "Purchase refused before anything was created — no merchant order exists "
            "because of this submission."
        )
    if status == PurchaseAuthorizationStatus.LINKAGE_REFUSED.value:
        return (
            "The trigger reports a merchant order may exist while the linkage was "
            "refused. This is never retried and never marked failed."
        )
    if status == PurchaseAuthorizationStatus.PURCHASE_UNKNOWN.value:
        return (
            "The purchase outcome is unknown — the submission may or may not have "
            "reached the merchant. It is recorded as unknown and never retried "
            "automatically."
        )
    return (
        "The purchase submission did not run, so the authorization stays pending your "
        "next explicit request."
    )


@router.post("/{mission_id}/order/purchase", response_model=dict)
async def purchase_mission_order(
    mission_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Authorize and submit ONE merchant purchase for this mission's confirmed order.

    The user's explicit POST is the authorization boundary: the deterministic purchase
    gate is the final authority, the claim is persisted before the trigger is called, and
    exactly one authorization exists per order (unique order_id). An existing
    authorization in a terminal state is returned as-is — never resubmitted, never
    duplicated. A persisted submission lease (submission_state / submission_owner) makes
    "one submission in flight" a database invariant: one atomic conditional UPDATE
    decides which request submits, every other concurrent request returns the persisted
    record without touching the trigger, and that same UPDATE increments attempt_count —
    exactly once per accepted attempt. Only a bare claim (authorized) whose submission
    demonstrably did not run may be submitted again by another explicit user POST;
    unknown outcomes are never retried and there is no reconciliation or reclaim path.
    """
    # Phase 4.6.1: a protected purchase write runs only when startup verification proved
    # the database is in the separated topology; otherwise this endpoint fails closed.
    require_protected_ops()
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")

    existing = await _load_purchase_authorization(db, order.order_id)
    if (
        existing is not None
        and existing.status in PurchaseAuthorizationStatus.terminal_states()
    ):
        return _purchase_authorization_response(existing, order)

    decision = evaluate_purchase(_purchase_facts(record, order, existing))
    await audit.append(db, mission_id, "user", decision)
    if not decision.approved:
        await db.commit()
        raise HTTPException(status_code=403, detail=decision.reason)

    if not settings.merchant_trigger_url.strip() or not settings.merchant_trigger_token:
        # Fail closed BEFORE persisting a claim: a misconfigured deployment never leaves
        # a purchase authorization behind and never contacts anything.
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail="Merchant purchase is not configured for this deployment",
        )

    # Phase 4.6.2 (the 4.5.9 outage policy): a NEW submission may leave the database
    # only when the external evidence ledger is exactly current — the WORM anchor at
    # the database high-water, chain-verified, with the pin's recorded identity matching
    # the live object version. "Skipped" means the check does not apply at all (no
    # store configured — the pre-ledger deployment posture, where the authority flag
    # remains the master gate); a CONFIGURED store with an uninstalled or non-current
    # ledger is not-current and refuses with its reason before any claim is persisted.
    currency = await submission_currency_for_app()
    if currency.state == CURRENCY_NOT_CURRENT:
        reason = f"Purchase submission refused: {currency.reason}"
        if existing is not None:
            existing.error_note = reason
        await db.commit()
        logger.warning(
            "purchase submission refused for mission %s: %s", mission_id, currency.reason
        )
        raise HTTPException(status_code=503, detail=reason)

    # Phase 4.4: the submission lease. Whichever request holds the lease submits; every
    # other concurrent request returns the same persisted record without touching the
    # trigger. The loop is bounded: each iteration either returns, wins the lease, or
    # makes progress against a row another request has committed.
    authorization = existing
    lease_token: str | None = None
    for _ in range(3):
        if authorization is None:
            # First claim for this order. The INSERT establishes the claim with
            # attempt_count 0 — the lease take below is what counts an accepted attempt.
            # The unique order_id keeps a second claim impossible at the storage level.
            authorization = PurchaseAuthorizationRecord(
                order_id=order.order_id,
                mission_id=mission_id,
                user_id=record.user_id,
                authority_decision=decision.decision.value,
                decision_reason=decision.reason,
                status=PurchaseAuthorizationStatus.AUTHORIZED.value,
                attempt_count=0,
            )
            db.add(authorization)
            try:
                await db.commit()
            except IntegrityError:
                # A concurrent authorization won the unique order_id: its row is now the
                # one this request arbitrates against through the lease.
                await db.rollback()
                await db.refresh(order)  # the rollback expired the order row's attributes
                authorization = await _load_purchase_authorization(db, order.order_id)
                if authorization is None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A concurrent purchase authorization could not be resolved"
                        ),
                    )
                continue
            authorization = await _load_purchase_authorization(db, order.order_id)
            continue

        if authorization.status in PurchaseAuthorizationStatus.terminal_states():
            return _purchase_authorization_response(authorization, order)

        if authorization.submission_state == SUBMISSION_IN_FLIGHT:
            # Someone else owns the submission. If that owner crashed, the abandoned
            # lease closes as purchase_unknown; otherwise the honest answer is the
            # persisted record as it stands — never a second submission behind it.
            recovered = await _recover_abandoned_submission(db, authorization)
            if recovered:
                event_row = await append_lifecycle_event(
                    db,
                    mission_id=mission_id,
                    user_id=record.user_id,
                    state=record.status,
                    detail=_purchase_submission_note(authorization),
                    extra_payload={
                        "status": record.status,
                        "order_id": order.order_id,
                        "purchase_authorization_status": authorization.status,
                    },
                )
                await db.commit()
                await broadcast_event_row(event_row)
            else:
                await db.refresh(authorization)
            return _purchase_authorization_response(authorization, order)

        lease_token = await _acquire_submission_lease(db, authorization.authorization_id)
        if lease_token is None:
            authorization = await _load_purchase_authorization(db, order.order_id)
            continue
        await db.refresh(authorization)  # the lease take rewrote attempt_count and state
        break

    if lease_token is None:
        # The bounded arbitration could not settle a stable state under pathological
        # contention. Return the persisted record without submitting anything.
        if authorization is None:
            raise HTTPException(
                status_code=409,
                detail="A concurrent purchase authorization could not be resolved",
            )
        return _purchase_authorization_response(authorization, order)

    # The lease is held: this request owns the ONE submission for this claim.
    try:
        report = await submit_purchase(mission_id, order.order_id)
    except Exception as exc:
        # The trigger client never raises for a submission-level failure, so an exception
        # here happened before anything was sent: the lease releases to idle, the claim
        # stays resubmittable, and the note is honest.
        await _release_submission_lease(
            db,
            authorization.authorization_id,
            lease_token,
            note=(
                "the purchase submission failed before an outcome was recorded: "
                f"{_provider_error_text(exc)}"
            ),
        )
        await db.commit()
        await db.refresh(authorization)
        return _purchase_authorization_response(authorization, order)

    await _apply_trigger_report(db, authorization, lease_token, report)
    await audit.append_event(
        db,
        mission_id=mission_id,
        actor="user",
        action="merchant_purchase_submission",
        recorded=report.classification != NOT_SENT,
        reason=(
            f"purchase authorization {authorization.authorization_id} for order "
            f"{order.order_id} submitted (attempt {authorization.attempt_count}); "
            f"classified '{report.classification}' from trigger outcome_class "
            f"{report.outcome_class!r}; the report is untrusted and the authoritative "
            "linkage remains the order row's provider_order_reference"
        ),
    )
    event_row = await append_lifecycle_event(
        db,
        mission_id=mission_id,
        user_id=record.user_id,
        state=record.status,
        detail=_purchase_submission_note(authorization),
        extra_payload={
            "status": record.status,
            "order_id": order.order_id,
            "purchase_authorization_status": authorization.status,
        },
    )
    await db.commit()
    await broadcast_event_row(event_row)
    return _purchase_authorization_response(authorization, order)


@router.get("/{mission_id}/purchase", response_model=dict)
async def get_purchase_authorization(
    mission_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the mission's persisted purchase authorization alongside the AUTHORITATIVE
    merchant linkage facts. The trigger-reported fields are shown as the untrusted report
    they are; linked / awaiting / unlinked is derived from the order row alone, so a
    trigger's claimed success never reads as a confirmed merchant order.
    """
    await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    authorization = await _load_purchase_authorization(db, order.order_id)
    if authorization is None:
        raise HTTPException(
            status_code=404, detail="No purchase authorization exists for this order"
        )
    return _purchase_authorization_response(authorization, order)


# --------------------------------------------------------------------------- #
# Task 09 — post-purchase order context & issue intake.
#
# Issue intake on the confirmed RESOLVE order. The client supplies ONLY
# issue_type, description, requested_resolution, and optional evidence metadata.
# The order/mission/user identity is resolved server-side from the authenticated
# mission; requested_resolution is stored as a user PREFERENCE, never an
# authorization. No refund/replacement/merchant action exists or is claimed.
# --------------------------------------------------------------------------- #


class ReportOrderIssueRequest(BaseModel):
    """User-provided issue intake only — nothing authoritative about identity or decisions."""

    issue_type: str
    description: str
    requested_resolution: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @field_validator("issue_type")
    @classmethod
    def _issue_type_allowed(cls, v: str) -> str:
        allowed = {t.value for t in IssueType}
        if v not in allowed:
            raise ValueError(f"issue_type must be one of {sorted(allowed)}")
        return v

    @field_validator("requested_resolution")
    @classmethod
    def _resolution_allowed(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"none", "refund", "replacement"}
        if v not in allowed:
            raise ValueError(f"requested_resolution must be one of {sorted(allowed)}")
        return v

    @field_validator("description")
    @classmethod
    def _description_valid(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("description is required")
        if len(stripped) > 2000:
            raise ValueError("description must be at most 2000 characters")
        return stripped

    @field_validator("evidence")
    @classmethod
    def _evidence_capped(cls, v: list[EvidenceItem]) -> list[EvidenceItem]:
        if len(v) > 10:
            raise ValueError("at most 10 evidence items are allowed")
        return v


def _remedy_authority(record: MissionRecord, order: ResolveOrderRecord) -> dict:
    """
    Live per-remedy Authority verdicts for the client, computed by the same engine the
    executors re-run at execution time. Advisory display only: this lets the buyer see
    BOTH remedies they may choose between (the assessment's proposal stays a
    recommendation), while the executor endpoints remain the only path that acts — and
    they re-evaluate authority on every click.
    """
    authority_data = (record.mandate or {}).get("authority")
    authority = Authority(**(authority_data or {"allowed_actions": [], "recovery_budget": "0"}))
    payment_data = (record.shopping_intent or {}).get("payment") or {}
    if payment_data.get("status") != "verified" or not payment_data.get("razorpay_payment_id"):
        refund_entry = {
            "decision": "blocked",
            "reason": "A refund requires a verified Razorpay TEST payment on this mission",
        }
    else:
        amount, _, _ = _canonical_refund_amount(payment_data, order)
        gate = evaluate(
            authority, ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=amount)
        )
        refund_entry = {
            "decision": "allow" if gate.approved else "blocked",
            "reason": gate.reason,
        }
    replacement_gate = evaluate(authority, ProposedAction(action=AllowedAction.REQUEST_REPLACEMENT))
    return {
        "request_refund": refund_entry,
        "request_replacement": {
            "decision": "allow" if replacement_gate.approved else "blocked",
            "reason": replacement_gate.reason,
        },
    }


def _issue_response(
    issue: OrderIssueRecord,
    agent_message: str | None = None,
    already_existed: bool = False,
    assessment: ResolutionAssessmentRecord | None = None,
    refund: RefundRecord | None = None,
    replacement: ReplacementRecord | None = None,
    return_record: ReturnRecord | None = None,
    resolution: ResolutionRecord | None = None,
    remedy_authority: dict | None = None,
) -> dict:
    """Canonical client-facing issue payload — every value read from the persisted row."""
    return {
        "issue_id": issue.issue_id,
        "order_id": issue.order_id,
        "mission_id": issue.mission_id,
        "user_id": issue.user_id,
        "issue_type": issue.issue_type,
        "description": issue.description,
        "requested_resolution": issue.requested_resolution,
        "status": issue.status,
        "evidence": issue.evidence or [],
        "questions": questions_payload(issue.issue_type),
        "answers": dict(issue.answers or {}),
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "already_existed": already_existed,
        "assessment": _assessment_response(assessment) if assessment is not None else None,
        "refund": _refund_response(refund) if refund is not None else None,
        "replacement": (
            _replacement_response(replacement) if replacement is not None else None
        ),
        "return": _return_response(return_record) if return_record is not None else None,
        "resolution": _resolution_response(resolution) if resolution is not None else None,
        "remedy_authority": remedy_authority,
        "manual_review": {
            "requested": bool(issue.manual_review_requested),
            "requested_at": (
                issue.manual_review_requested_at.isoformat()
                if issue.manual_review_requested_at
                else None
            ),
            "blocks_autonomous_resolution": bool(issue.manual_review_requested),
        },
        "resolution_note": (
            "Requested resolution is a user preference only — no refund, replacement, "
            "or merchant action has been initiated."
        ),
        "agent_message": agent_message,
    }


async def _load_issues(db: AsyncSession, mission_id: str) -> list[OrderIssueRecord]:
    result = await db.execute(
        select(OrderIssueRecord)
        .where(OrderIssueRecord.mission_id == mission_id)
        .order_by(OrderIssueRecord.created_at.asc())
    )
    return list(result.scalars().all())


@router.get("/{mission_id}/order/issues", response_model=dict)
async def list_order_issues(
    mission_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """The mission's persisted post-purchase issues, oldest first, each with its
    assessment (if any) attached (owner only)."""
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    remedies = _remedy_authority(record, order)
    issues = await _load_issues(db, mission_id)
    assessments_result = await db.execute(
        select(ResolutionAssessmentRecord).where(
            ResolutionAssessmentRecord.mission_id == mission_id
        )
    )
    by_issue = {a.issue_id: a for a in assessments_result.scalars().all()}
    refunds_result = await db.execute(
        select(RefundRecord).where(RefundRecord.mission_id == mission_id)
    )
    refunds_by_issue = {r.issue_id: r for r in refunds_result.scalars().all()}
    replacements_result = await db.execute(
        select(ReplacementRecord).where(ReplacementRecord.mission_id == mission_id)
    )
    replacements_by_issue = {r.issue_id: r for r in replacements_result.scalars().all()}
    returns_result = await db.execute(
        select(ReturnRecord).where(ReturnRecord.mission_id == mission_id)
    )
    returns_by_issue = {r.issue_id: r for r in returns_result.scalars().all()}
    resolutions_result = await db.execute(
        select(ResolutionRecord).where(ResolutionRecord.mission_id == mission_id)
    )
    resolutions_by_issue = {r.issue_id: r for r in resolutions_result.scalars().all()}
    return {
        "mission_id": mission_id,
        "order_id": order.order_id,
        "issues": [
            _issue_response(
                issue,
                assessment=by_issue.get(issue.issue_id),
                refund=refunds_by_issue.get(issue.issue_id),
                replacement=replacements_by_issue.get(issue.issue_id),
                return_record=returns_by_issue.get(issue.issue_id),
                resolution=resolutions_by_issue.get(issue.issue_id),
                remedy_authority=remedies,
            )
            for issue in issues
        ],
    }


@router.post("/{mission_id}/order/issues", response_model=dict)
async def report_order_issue(
    mission_id: str,
    body: ReportOrderIssueRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Record a post-purchase issue against the mission's confirmed RESOLVE order.

    Deterministic intake: the order is resolved from the authenticated mission
    (never from client ids), the issue starts in `reported`, and the first issue
    moves the order into `awaiting_post_purchase_context`. Duplicate behavior: an
    exact repeat (same issue_type + description + requested_resolution on the same
    order) returns the already-recorded issue without creating a copy; any
    genuinely different report creates a new issue row. The requested resolution
    is stored as the user's stated preference — it authorizes nothing.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    allowed_order_states = {
        MissionStatus.ORDER_CONFIRMED.value,
        MissionStatus.DELIVERED.value,
        MissionStatus.AWAITING_POST_PURCHASE_CONTEXT.value,
    }
    if order.status not in allowed_order_states:
        raise HTTPException(
            status_code=400,
            detail="Issues can only be reported after the RESOLVE order is confirmed",
        )

    resolution = body.requested_resolution or "none"
    existing_issues = await _load_issues(db, mission_id)
    for existing in existing_issues:
        if (
            existing.issue_type == body.issue_type
            and existing.description == body.description
            and existing.requested_resolution == resolution
        ):
            agent_message = (
                f"Issue {existing.issue_id} was already recorded on order {order.order_id} "
                "with the same details — nothing new was created."
            )
            return _issue_response(
                existing,
                agent_message=agent_message,
                already_existed=True,
                remedy_authority=_remedy_authority(record, order),
            )

    issue = OrderIssueRecord(
        order_id=order.order_id,
        mission_id=mission_id,
        user_id=record.user_id,
        issue_type=body.issue_type,
        description=body.description,
        requested_resolution=resolution,
        status=IssueStatus.REPORTED.value,
        evidence=[item.model_dump(mode="json") for item in body.evidence],
    )
    db.add(issue)
    await db.flush()

    if order.status in {
        MissionStatus.ORDER_CONFIRMED.value,
        MissionStatus.DELIVERED.value,
    }:
        order.status = MissionStatus.AWAITING_POST_PURCHASE_CONTEXT.value
        intent_data = dict(record.shopping_intent or {})
        intent_data["state"] = MissionStatus.AWAITING_POST_PURCHASE_CONTEXT.value
        record.shopping_intent = intent_data
        record.status = MissionStatus.AWAITING_POST_PURCHASE_CONTEXT.value
    # Intake is a fact worth keeping even though no authority was involved: the requested
    # resolution is recorded as a preference, and the reason says so, so a later reader cannot
    # mistake this row for a permission.
    await audit.append_event(
        db,
        mission_id=mission_id,
        actor="user",
        action="issue_reported",
        recorded=True,
        reason=(
            f"issue {issue.issue_id} reported by the buyer on RESOLVE order {order.order_id} "
            f"({issue.issue_type}); requested resolution {resolution} is a stated preference and "
            "authorizes nothing — assessment and the Authority Engine decide"
        ),
    )
    issue_row = await append_lifecycle_event(
        db,
        mission_id=mission_id,
        user_id=record.user_id,
        state="issue_reported",
        detail=f"Issue reported on order {order.order_id}: {issue.issue_type}.",
        extra_payload={"issue_id": issue.issue_id, "issue_type": issue.issue_type},
    )
    await db.commit()

    await broadcast_event_row(issue_row)

    agent_message = (
        f"Issue {issue.issue_id} recorded on RESOLVE order {order.order_id}: {issue.issue_type}. "
        f"Requested resolution: {issue.requested_resolution} — noted as your preference only; "
        "no refund, replacement, or merchant action has been initiated. "
        "I've recorded the issue and will review the available resolution options."
    )
    return _issue_response(
        issue, agent_message=agent_message, remedy_authority=_remedy_authority(record, order)
    )


# --------------------------------------------------------------------------- #
# M22 §2 — per-category issue questions and the buyer's answers.
#
# A reported issue carries only the buyer's free-text description. These surfaces
# expose the deterministic question set for the issue's category (a pure constant,
# never model- or client-authored) and persist the buyer's answers as issue context.
# Answers authorize nothing and change nothing — they are what a later assessment
# reads alongside the backend's own DB facts.
# --------------------------------------------------------------------------- #


class SubmitIssueAnswersRequest(BaseModel):
    """The buyer's answers to the category's deterministic intake questions."""

    answers: dict[str, str]


def _issue_questions_response(issue: OrderIssueRecord) -> dict:
    """The category's question set plus whatever the buyer has answered so far."""
    return {
        "issue_id": issue.issue_id,
        "issue_type": issue.issue_type,
        "questions": questions_payload(issue.issue_type),
        "answers": dict(issue.answers or {}),
    }


@router.get("/{mission_id}/order/issues/{issue_id}/questions", response_model=dict)
async def get_issue_questions(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """The deterministic per-category question set for an issue, with any answers already
    recorded. Owner-scoped like every issue surface; the client supplies nothing."""
    await _load_mission_for_shopping_action(mission_id, current_user, db)
    issue = await _load_issue(db, mission_id, issue_id)
    return _issue_questions_response(issue)


@router.post("/{mission_id}/order/issues/{issue_id}/answers", response_model=dict)
async def submit_issue_answers(
    mission_id: str,
    issue_id: str,
    body: SubmitIssueAnswersRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Store the buyer's answers to the category's intake questions on the issue row.

    Validation is deterministic (`normalize_answers`): only this category's question ids,
    non-empty bounded text — a rejected map replaces nothing. Answers are accepted only
    while the issue is still `reported`: once an assessment has consumed the record, the
    evidence it saw cannot be rewritten afterwards. Nothing else changes — no status
    transition, no authority, no execution of anything.
    """
    await _load_mission_for_shopping_action(mission_id, current_user, db)
    issue = await _load_issue(db, mission_id, issue_id)
    if issue.status != IssueStatus.REPORTED.value:
        raise HTTPException(
            status_code=400,
            detail=(
                "Answers can only be recorded while the issue is 'reported' "
                f"(this one is '{issue.status}')"
            ),
        )
    normalized, error = normalize_answers(issue.issue_type, body.answers)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)

    issue.answers = normalized
    await audit.append_event(
        db,
        mission_id=mission_id,
        actor="user",
        action="issue_answers_recorded",
        recorded=True,
        reason=(
            f"buyer answered {len(normalized)} of the {issue.issue_type} intake questions "
            f"on issue {issue.issue_id}; recorded as assessment context, authorizes nothing"
        ),
    )
    await db.commit()
    return _issue_questions_response(issue)


# --------------------------------------------------------------------------- #
# Task 10 — resolution assessment & authority proposal.
#
# USER ISSUE -> REASONING MODEL -> STRUCTURED PROPOSAL -> AUTHORITY ENGINE -> DECISION.
#
# The model proposes; only this deterministic code disposes, and even the "decision"
# here is a gating outcome, not an execution: no refund, replacement, return, merchant
# contact, or payment change exists or happens in this system. A malformed or
# out-of-vocabulary model response can never cause an action — it is replaced by a
# deterministic escalate-to-human fallback. Any amount attached to a refund proposal is
# ALWAYS the canonical persisted order amount; the model's number is never trusted.
# --------------------------------------------------------------------------- #


async def _load_issue(
    db: AsyncSession, mission_id: str, issue_id: str
) -> OrderIssueRecord:
    """Load an issue by id and prove it belongs to this mission — cross-mission issue ids
    are indistinguishable from missing ones (404), never an authorization bypass."""
    issue = await db.get(OrderIssueRecord, issue_id)
    if issue is None or issue.mission_id != mission_id:
        raise HTTPException(status_code=404, detail="Issue not found for this mission")
    return issue


async def _load_assessment(
    db: AsyncSession, issue_id: str
) -> ResolutionAssessmentRecord | None:
    result = await db.execute(
        select(ResolutionAssessmentRecord).where(
            ResolutionAssessmentRecord.issue_id == issue_id
        )
    )
    return result.scalar_one_or_none()


def _build_resolution_context(
    record: MissionRecord,
    order: ResolveOrderRecord,
    issue: OrderIssueRecord,
    selected: ProductResult,
) -> dict:
    """
    Canonical, server-built resolution context. Every value comes from persisted
    mission/order/issue/payment state — the client supplies nothing authoritative here,
    and no secret (API keys, webhook secrets, payment credentials) is included.
    Razorpay order/payment ids are references the backend already holds, not secrets,
    and the reasoning model needs them only to talk about the right transaction.
    """
    intent_data = record.shopping_intent or {}
    payment_data = intent_data.get("payment") or {}
    mandate_data = record.mandate or {}
    authority_data = mandate_data.get("authority") or {}
    return {
        "mission_id": record.mission_id,
        "mission_status": record.status,
        "order": {
            "order_id": order.order_id,
            "status": order.status,
            "amount": str(order.amount),
            "currency": order.currency,
            "merchant": order.merchant,
            "product_id": order.product_id,
            "title": order.title,
        },
        "selected_product": {
            "title": selected.title,
            "price": str(selected.extracted_price) if selected.extracted_price is not None else None,
            "currency": selected.currency or order.currency,
            "merchant": selected.merchant,
        },
        "payment": {
            "status": payment_data.get("status"),
            "amount": str(payment_data.get("amount")) if payment_data.get("amount") is not None else None,
            "currency": payment_data.get("currency"),
            "razorpay_order_id": payment_data.get("razorpay_order_id"),
            "razorpay_payment_id": payment_data.get("razorpay_payment_id"),
            "test_mode": True,
        },
        "issue": {
            "issue_id": issue.issue_id,
            "issue_type": issue.issue_type,
            "description": issue.description,
            "requested_resolution": issue.requested_resolution,
            "status": issue.status,
            "evidence": issue.evidence or [],
            "category_questions": questions_payload(issue.issue_type),
            "answers": dict(issue.answers or {}),
        },
        "authority_summary": {
            "allowed_actions": authority_data.get("allowed_actions", []),
            "recovery_budget": str(authority_data.get("recovery_budget", "0")),
            "spent_so_far": str(authority_data.get("spent_so_far", "0")),
        },
        "notes": (
            "This is Razorpay TEST-MODE shopping context. You propose only: no refund, "
            "replacement, return, or merchant action can be executed by you."
        ),
    }


def _safe_fallback_proposal(reason: str) -> ResolutionProposal:
    """The deterministic floor: an unparseable model response escalates to a human. It can
    never propose money movement, and it is labelled so the record shows where it came from."""
    return ResolutionProposal(
        action=AllowedAction.ESCALATE_TO_HUMAN,
        issue_assessment=(
            "The resolution model's assessment could not be used, so this issue is routed "
            "to a human reviewer by deterministic fallback."
        ),
        reasoning_summary=f"Deterministic safe fallback: {reason}",
        requested_amount=None,
        currency=None,
        confidence=None,
        needs_escalation=True,
        source="safe_fallback",
    )


def _validate_resolution_proposal(raw: object, order: ResolveOrderRecord) -> ResolutionProposal:
    """
    Parse the raw, untrusted provider response into the strict ResolutionProposal schema.

    Anything malformed, non-dict, out-of-enum, or outside the controlled resolution
    vocabulary becomes the deterministic safe-fallback escalation — never an action.
    Free-form model text (assessment/summary) is kept only as bounded description strings.
    A refund's amount is ALWAYS the canonical persisted order amount: the model's number
    is discarded. Non-refund proposals carry no amount at all.
    """
    if not isinstance(raw, dict):
        return _safe_fallback_proposal("model response was not a JSON object")

    action_str = raw.get("proposed_action")
    try:
        action = AllowedAction(action_str)
    except ValueError:
        return _safe_fallback_proposal(f"proposed_action {action_str!r} is not a known action")
    if action not in RESOLUTION_ACTIONS:
        return _safe_fallback_proposal(
            f"proposed_action {action.value!r} is outside the resolution vocabulary"
        )

    def _text(value: object, limit: int = 2000) -> str:
        return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""

    issue_assessment = _text(raw.get("issue_assessment")) or "No assessment text was provided."
    reasoning_summary = _text(raw.get("reasoning_summary")) or "No reasoning summary was provided."

    requested_amount: Decimal | None = None
    currency: str | None = None
    if action is AllowedAction.REQUEST_REFUND:
        # Canonical override: refund proposals always reference the exact amount the
        # user actually paid on this RESOLVE order — never the model's number.
        requested_amount = Decimal(str(order.amount))
        currency = order.currency

    confidence: float | None = None
    raw_confidence = raw.get("confidence")
    if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool):
        confidence = min(1.0, max(0.0, float(raw_confidence)))

    return ResolutionProposal(
        action=action,
        issue_assessment=issue_assessment,
        reasoning_summary=reasoning_summary,
        requested_amount=requested_amount,
        currency=currency,
        confidence=confidence,
        needs_escalation=bool(raw.get("needs_escalation", False)),
        source="reasoning_model",
    )


def _assessment_response(
    assessment: ResolutionAssessmentRecord,
    agent_message: str | None = None,
    already_existed: bool = False,
) -> dict:
    """Canonical client-facing assessment payload — every value read from the persisted row."""
    return {
        "assessment_id": assessment.assessment_id,
        "issue_id": assessment.issue_id,
        "mission_id": assessment.mission_id,
        "order_id": assessment.order_id,
        "user_id": assessment.user_id,
        "proposed_action": assessment.proposed_action,
        "issue_assessment": assessment.issue_assessment,
        "reasoning_summary": assessment.reasoning_summary,
        "requested_amount": str(assessment.requested_amount)
        if assessment.requested_amount is not None
        else None,
        "currency": assessment.currency,
        "confidence": assessment.confidence,
        "needs_escalation": assessment.needs_escalation,
        "proposal_source": assessment.proposal_source,
        "authority_decision": assessment.authority_decision,
        "decision_reason": assessment.decision_reason,
        "created_at": assessment.created_at.isoformat() if assessment.created_at else None,
        "updated_at": assessment.updated_at.isoformat() if assessment.updated_at else None,
        "already_existed": already_existed,
        "execution_note": (
            "This is a proposal plus an authority gating outcome only. Action has NOT "
            "been executed — no refund, replacement, return, or merchant contact has "
            "occurred. 'allow' means the proposal passed the Authority Engine, nothing more."
        ),
        "agent_message": agent_message,
    }


def _decision_agent_message(assessment: ResolutionAssessmentRecord) -> str:
    action_label = assessment.proposed_action.replace("_", " ")
    if assessment.authority_decision == "allow":
        return (
            f"Assessment {assessment.assessment_id}: proposed resolution ({action_label}) "
            "passed the authority gate. Action has not been executed — no refund, "
            "replacement, or merchant contact has occurred."
        )
    if assessment.authority_decision == "escalate":
        return (
            f"Assessment {assessment.assessment_id}: this issue is escalated for human review. "
            "Action has not been executed."
        )
    return (
        f"Assessment {assessment.assessment_id}: the proposed resolution ({action_label}) was "
        f"blocked by the Authority Engine — {assessment.decision_reason} "
        "Action has not been executed."
    )


@router.post("/{mission_id}/order/issues/{issue_id}/assess", response_model=dict)
async def assess_order_issue(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Assess a reported post-purchase issue: build the canonical context, ask the reasoning
    model for a STRICT structured proposal, and gate that proposal through the existing
    Authority Engine. Persist the assessment and the decision; change nothing else.

    There is deliberately no request body — the client supplies no amount, action, or
    authority value it could tamper with. Idempotency: one assessment per issue, enforced
    by the unique issue_id column; a repeat returns the existing record without re-calling
    the model. A provider outage returns 503 and writes NOTHING (order, payment, issue,
    and authority all stay untouched), so an assessment can be retried once the provider
    is back. `allow` means the proposal passed the gate — execution does not exist here.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    issue = await _load_issue(db, mission_id, issue_id)

    existing = await _load_assessment(db, issue_id)
    if existing is not None:
        agent_message = (
            f"Issue {issue_id} was already assessed as {existing.assessment_id} "
            f"(decision: {existing.authority_decision}) — nothing new was created."
        )
        return _assessment_response(existing, agent_message=agent_message, already_existed=True)

    if issue.status != IssueStatus.REPORTED.value:
        raise HTTPException(
            status_code=400,
            detail=f"Only issues in status 'reported' can be assessed (this one is '{issue.status}')",
        )

    return await _assess_issue_row(record, order, issue, db, existing=None)


async def _assess_issue_row(
    record: MissionRecord,
    order: ResolveOrderRecord,
    issue: OrderIssueRecord,
    db: AsyncSession,
    *,
    existing: ResolutionAssessmentRecord | None,
) -> dict:
    """
    The assessment itself, in one place: canonical server-built context -> the model's strict
    proposal -> the same `evaluate()` call -> a persisted decision and an audit row.

    `/assess` reaches it with no stored proposal and writes a new assessment row; the agent's
    resume-after-more-information path reaches it with the issue's existing row and updates that one.
    Two entry points, one gate — which is the whole reason the extraction exists.
    """
    _, selected = _require_selected_product(record)
    context = _build_resolution_context(record, order, issue, selected)

    reasoning = get_reasoning_provider(settings.reasoning_provider, settings.reasoning_api_key)
    try:
        raw_proposal = await reasoning.assess_resolution(context)
    except Exception as exc:
        # Outage path: fail completely clean. No assessment row, no issue status change,
        # no order/payment mutation, no audit entry — purely a readable 503.
        raise HTTPException(
            status_code=503,
            detail=(
                "Resolution assessment is temporarily unavailable — the reasoning service "
                "could not be reached. Nothing was recorded and nothing was changed; "
                "please try again."
            ),
        ) from exc

    proposal = _validate_resolution_proposal(raw_proposal, order)

    authority_data = (record.mandate or {}).get("authority")
    authority = Authority(**(authority_data or {"allowed_actions": [], "recovery_budget": "0"}))
    engine_proposal = ProposedAction(action=proposal.action, amount=proposal.requested_amount)
    gate = evaluate(authority, engine_proposal)

    # The engine verdict and the recorded decision are deliberately different things. The
    # engine says whether the action may run; the decision says how the case should proceed.
    # A block the owner could cure by delegating (never delegated / expired / above ceiling /
    # over budget) is recorded as an escalation with the delegation offered — per the refund
    # workflow's authority rules — while a deployment-policy refusal or a malformed proposal
    # is recorded as the honest denial it is. The engine itself is never re-implemented here:
    # classify_block re-derives the same first-failure-wins outcome from the same inputs.
    if gate.approved:
        decision = "escalate" if proposal.action is AllowedAction.ESCALATE_TO_HUMAN else "allow"
        decision_reason = gate.reason
    else:
        category = classify_block(authority, engine_proposal, gate)
        if category is BlockCategory.AUTHORITY_GAP:
            decision = "escalate"
            decision_reason = (
                f"{gate.reason} That is an authority gap rather than a refusal: the mission's "
                f"owner can delegate '{proposal.action.value}' — bounded by this deployment's "
                "ceiling — and the issue can then be reassessed, or it can be routed to manual "
                "review. Nothing has been executed."
            )
        elif category is BlockCategory.DEPLOYMENT_POLICY:
            decision = "deny"
            decision_reason = (
                f"{gate.reason} Enabling that switch is an operator decision no delegation can "
                "make; the issue can still be routed to manual review. Nothing has been executed."
            )
        else:
            decision = "deny"
            decision_reason = gate.reason

    assessment = existing or ResolutionAssessmentRecord(
        issue_id=issue.issue_id,
        mission_id=record.mission_id,
        order_id=order.order_id,
        user_id=record.user_id,
    )
    assessment.proposed_action = proposal.action.value
    assessment.issue_assessment = proposal.issue_assessment
    assessment.reasoning_summary = proposal.reasoning_summary
    assessment.requested_amount = proposal.requested_amount
    assessment.currency = proposal.currency
    assessment.confidence = proposal.confidence
    assessment.needs_escalation = proposal.needs_escalation or decision == "escalate"
    assessment.proposal_source = proposal.source
    assessment.authority_decision = decision
    assessment.decision_reason = decision_reason
    if existing is None:
        db.add(assessment)

    # Backend code — never the model — advances the deterministic issue lifecycle.
    issue.status = IssueStatus.UNDER_REVIEW.value

    await audit.append(db, record.mission_id, "buyer_agent", gate)
    await db.commit()

    return _assessment_response(assessment, agent_message=_decision_agent_message(assessment))


def record_more_information_request(
    record: MissionRecord, issue: OrderIssueRecord | None, question: str
) -> None:
    """
    Park the question the agent asked, so the next message can resume from it (Phase 13).

    One outstanding question per issue, on the issue row: the answer is what the buyer's next message
    carries, and the assessment path needs to know which question it answers. The mission's summary
    mirrors it because a reader of the case file should see what is waiting, not only what happened.
    """
    text = question.strip()[:1000]
    if issue is not None:
        issue.more_information_question = text
    record.resolution_summary = f"Waiting on the buyer: {text}"[:2000]


def record_escalation_without_issue(record: MissionRecord, reason: str | None) -> None:
    """
    Record a request for human attention when there is no issue to attach it to.

    This is deliberately the weakest of the escalation paths: a sentence on the mission and a
    conversation row. No issue row is invented, no reviewer is contacted, and no status is claimed —
    because an escalation without something to escalate is not a case yet.
    """
    record.resolution_summary = (
        f"Requested human review: {(reason or 'the buyer asked for a person').strip()[:600]}"
    )[:2000]


async def attach_more_information_answer(
    issue: OrderIssueRecord, answer: str, db: AsyncSession
) -> OrderIssueRecord:
    """
    Fold the buyer's answer into the issue record (Task CORE-2, Phase 13).

    The answer is the user's own words, taken from the server-side request that carried them and
    appended to the issue's description — not something the model supplies. The pending question
    clears, and the issue returns to `reported` so the assessment path will accept it again.
    """
    question = issue.more_information_question or ""
    issue.description = f"{issue.description}\n\nFollow-up after '{question[:200]}': {answer[:1000]}"[:2000]
    issue.more_information_question = None
    issue.status = IssueStatus.REPORTED.value
    await db.flush()
    return issue


async def reassess_issue(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None,
    db: AsyncSession,
) -> dict:
    """
    Assess an issue again, because something the last assessment could only see has changed.

    Two situations reach it, and they are the only two: the buyer answered a more-information
    question (Phase 13), or the last outcome was a refusal and the mandate may since have been
    delegated (Phase 10). In both, the honest description of the mission is "this needs re-examining",
    and the alternative is a dead end — a proposal that was right once and can now never be revisited.

    What is unchanged is the part that matters: the same canonical server-built context, the same
    model call, the same strict proposal validation, the same `evaluate()` against the mandate as it
    reads now, one audit row per decision. The existing assessment row is updated in place, so the
    one-proposal-per-issue invariant holds, and a refused or stranded resolution row is moved back to
    `pending` — which is precisely what it now is: recorded, not executed.

    It is deliberately not an HTTP route. `/assess` keeps its own one-shot contract for every other
    caller, so this cannot become a way to farm fresh decisions.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    issue = await _load_issue(db, mission_id, issue_id)
    existing = await _load_assessment(db, issue_id)
    if existing is None:
        raise HTTPException(
            status_code=400,
            detail="This issue has never been assessed, so there is nothing to reassess",
        )
    if getattr(issue, "more_information_question", None):
        raise HTTPException(
            status_code=400,
            detail="The question on this issue has not been answered yet",
        )
    if issue.manual_review_requested:
        raise HTTPException(
            status_code=400,
            detail="Manual review is active on this issue, so it is not being assessed automatically",
        )

    resolution = await _load_resolution_row(db, issue_id)
    if resolution is not None and resolution.resolution_state in {
        ResolutionState.MORE_INFORMATION_REQUIRED.value,
        ResolutionState.DENIED.value,
    }:
        was = resolution.resolution_state
        resolution.resolution_state = ResolutionState.PENDING.value
        resolution.provider_attempted = False
        resolution.message = (
            "The issue was examined again after "
            f"{was}; nothing has been executed."
        )
        await db.flush()

    return await _assess_issue_row(record, order, issue, db, existing=existing)


@router.post("/{mission_id}/order/issues/{issue_id}/reassess", response_model=dict)
async def reassess_order_issue(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Re-run an issue's assessment against the mandate as it reads NOW (M22).

    `/assess` is deliberately one-shot per issue; reassessment is the exception for the one
    situation it cannot serve — an issue whose proposal was blocked by an authority gap,
    where the remedy is a delegation the owner makes afterwards. The guards are reassess_issue's
    own: the issue must already be assessed, must not be waiting on an unanswered question,
    and must not be under active manual review. The same engine, the same model call, the same
    audit discipline — and still nothing is executed here.
    """
    return await reassess_issue(mission_id, issue_id, current_user, db)


# --------------------------------------------------------------------------- #
# Task 11A — controlled refund execution.
#
# The ONLY executable action in the system, and it is fenced on every side:
#   - the reasoning model may PROPOSE request_refund but never calls Razorpay;
#   - the client sends no body — amount, currency, and the Razorpay payment
#     reference are all read from the canonical persisted payment;
#   - execution requires a Task 10 assessment that proposed request_refund,
#     an ALLOWed authority decision, AND a fresh re-run through the same
#     Authority Engine at execution time (it may have changed since);
#   - DENY / ESCALATE can never reach the provider;
#   - Razorpay TEST MODE only — enforced by the rzp_test_ key prefix;
#   - one refund per issue at the DB level (unique issue_id), with pending
#     refunds re-checked at the provider instead of blindly re-created;
#   - 'verified' is reached only after the provider itself reports 'processed'.
# --------------------------------------------------------------------------- #


async def _load_refund(db: AsyncSession, issue_id: str) -> RefundRecord | None:
    result = await db.execute(
        select(RefundRecord).where(RefundRecord.issue_id == issue_id)
    )
    return result.scalar_one_or_none()


def _canonical_refund_amount(payment_data: dict, order: ResolveOrderRecord) -> tuple[Decimal, int, str]:
    """
    Refund amount safety: everything comes from the canonical persisted records.

    Rupees as Decimal, minor units as integer paise — never floats. The refund can never
    exceed the canonical order amount, whichever of the two persisted records is smaller.
    """
    amount = Decimal(str(payment_data["amount"]))
    raw_paise = payment_data.get("amount_paise")
    if raw_paise is None:
        paise = int((amount * 100).quantize(Decimal("1"), ROUND_HALF_UP))
    else:
        paise = int(raw_paise)
    order_paise = int(
        (Decimal(str(order.amount)) * 100).quantize(Decimal("1"), ROUND_HALF_UP)
    )
    paise = min(paise, order_paise)  # canonical cross-check: never refund more than paid
    amount = (Decimal(paise) / 100).quantize(Decimal("0.01"))
    currency = str(payment_data.get("currency") or order.currency or "INR").upper()
    return amount, paise, currency


def _provider_error_text(exc: Exception) -> str:
    """Bounded, secret-free description of a provider failure for persistence."""
    text = f"{type(exc).__name__}: {exc}"[:300]
    secret = settings.razorpay_key_secret
    if secret and secret in text:
        text = text.replace(secret, "[redacted]")
    return text


def _settle_refund_from_provider(refund: RefundRecord, provider_status: str | None) -> None:
    """Map an authoritative Razorpay refund status onto our lifecycle. Only 'processed'
    verifies; 'failed' fails; anything else stays pending — never a false success."""
    refund.provider_status = provider_status or None
    if provider_status == "processed":
        refund.status = RefundStatus.VERIFIED.value
        refund.verified_at = datetime.utcnow()
        refund.error_reason = None
    elif provider_status == "failed":
        refund.status = RefundStatus.FAILED.value
        refund.error_reason = "Razorpay reported this refund as failed"
        refund.verified_at = None
    else:
        refund.status = RefundStatus.PENDING.value
        refund.verified_at = None


def _refund_response(
    refund: RefundRecord, agent_message: str | None = None, already_existed: bool = False
) -> dict:
    """Canonical client-facing refund payload — every value read from the persisted row."""
    return {
        "refund_id": refund.refund_id,
        "issue_id": refund.issue_id,
        "mission_id": refund.mission_id,
        "order_id": refund.order_id,
        "user_id": refund.user_id,
        "razorpay_payment_id": refund.razorpay_payment_id,
        "razorpay_refund_id": refund.razorpay_refund_id,
        "amount": str(refund.amount),
        "amount_paise": refund.amount_paise,
        "currency": refund.currency,
        "status": refund.status,
        "provider_status": refund.provider_status,
        "error_reason": refund.error_reason,
        "verified_at": refund.verified_at.isoformat() if refund.verified_at else None,
        "created_at": refund.created_at.isoformat() if refund.created_at else None,
        "updated_at": refund.updated_at.isoformat() if refund.updated_at else None,
        "already_existed": already_existed,
        "test_mode": True,
        "refund_note": (
            "Razorpay TEST-MODE refund only. 'verified' means the provider itself reported "
            "the refund processed; this is not merchant fulfillment or real-world money movement."
            if refund.status == RefundStatus.VERIFIED.value
            else "Refund not yet verified by the provider — this is not a completed refund."
        ),
        "agent_message": agent_message,
    }


def _refund_agent_message(refund: RefundRecord) -> str:
    if refund.status == RefundStatus.VERIFIED.value:
        return (
            f"TEST-MODE refund {refund.refund_id} verified: Razorpay refund "
            f"{refund.razorpay_refund_id} reported status 'processed' for "
            f"₹{refund.amount} {refund.currency}. This is a TEST-mode provider refund only "
            "— no merchant fulfillment or real-world money movement is claimed."
        )
    if refund.status == RefundStatus.FAILED.value:
        return (
            f"Refund {refund.refund_id} failed ({refund.error_reason or 'provider failure'}). "
            "No verified refund exists; retrying goes through the same record — never a "
            "second uncontrolled refund."
        )
    return (
        f"Refund {refund.refund_id} is pending provider verification. It is NOT a completed "
        "refund until Razorpay reports it processed."
    )


@router.post("/{mission_id}/order/issues/{issue_id}/resolve", response_model=dict)
async def resolve_order_issue(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Run RESOLVE's autonomous resolution flow for one issue (Task 13). There is NO request body:
    the mission, order, issue, persisted Task 10 proposal and mandate are all read from the
    database, the Authority Engine is re-evaluated right now, and the permitted action is handed
    to the existing controlled executor for that action.

    The reasoning model is never called here, the client never chooses the action, and being
    allowed is never reported as being done.
    """
    return await resolve_issue(mission_id, issue_id, current_user, db)


@router.post("/{mission_id}/order/issues/{issue_id}/manual-review", response_model=dict)
async def request_manual_review(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Opt an issue out of automated resolution decisions (Task 14B.5A). There is NO request body
    and nothing to choose: the authenticated owner's issue is flagged for manual review, the
    request is persisted with a timestamp, and the autonomous /resolve path is blocked for that
    issue while the flag is active.

    Honest boundaries: this records the CUSTOMER'S REQUEST only. No human action is claimed or
    simulated, no external support ticket is created and no ticket number exists, no provider
    is contacted, and the Authority Engine's delegation rules are unchanged. Idempotent: a
    repeat returns the already-flagged issue without new audits or events.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    issue = await _load_issue(db, mission_id, issue_id)

    if issue.manual_review_requested:
        already = True
        agent_message = (
            f"Issue {issue_id} is already flagged for manual review "
            f"(requested {issue.manual_review_requested_at.isoformat()}) — nothing new happened."
        )
    else:
        already = False
        issue.manual_review_requested = True
        issue.manual_review_requested_at = datetime.utcnow()
        await audit.append(
            db, mission_id, "customer",
            AuthorityDecision(
                decision=Decision.APPROVED,
                reason=(
                    "Customer requested manual review for this issue — automated resolution "
                    "decisions are paused for it (Task 14B.5A opt-out)."
                ),
                action=AllowedAction.ESCALATE_TO_HUMAN,
                requested_amount=None,
            ),
            note=(
                f"manual_review_requested {issue_id}: persisted opt-out; autonomous resolution "
                "blocked while active; no provider contacted; no external ticket created"
            ),
        )
        await db.commit()
        agent_message = (
            f"Manual review requested for issue {issue_id}. The autonomous resolution path "
            "will not execute actions for this issue while manual review is active. No human "
            "action has been simulated and no external ticket exists."
        )

    return {
        "issue_id": issue_id,
        "mission_id": mission_id,
        "manual_review": {
            "requested": issue.manual_review_requested,
            "requested_at": (
                issue.manual_review_requested_at.isoformat()
                if issue.manual_review_requested_at
                else None
            ),
            "blocks_autonomous_resolution": True,
        },
        "already_existed": already,
        "agent_message": agent_message,
        "note": (
            "This is the customer's opt-out from automated decisions, recorded server-side. "
            "It is not a claim that a human has reviewed anything, and no ticket number exists."
        ),
    }


@router.post("/{mission_id}/order/issues/{issue_id}/refund", response_model=dict)
async def execute_issue_refund(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    "Execute the controlled Razorpay TEST-MODE refund via the shared executor function."
    return await run_refund_executor(mission_id, issue_id, current_user, db)


async def run_refund_executor(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None,
    db: AsyncSession,
):
    """
    Execute the Razorpay TEST-MODE refund for an assessed issue — deterministically, and
    only through every gate below. There is NO request body: amount, currency, and the
    Razorpay payment reference come from the canonical persisted records.

    Idempotency (all server-side): an already verified refund is returned untouched with
    no provider call; a pending refund is re-checked at the provider instead of re-created;
    a failed refund is retried through the SAME row; and unique(issue_id) makes a second
    refund row for one issue impossible at the storage level.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    issue = await _load_issue(db, mission_id, issue_id)

    intent_data, _selected = _require_selected_product(record)
    payment_data = intent_data.get("payment") or {}
    if payment_data.get("status") != "verified" or not payment_data.get("razorpay_payment_id"):
        raise HTTPException(
            status_code=400,
            detail="A refund requires a verified Razorpay TEST payment on this mission",
        )

    assessment = await _load_assessment(db, issue_id)
    if assessment is None or assessment.issue_id != issue_id or assessment.mission_id != mission_id:
        raise HTTPException(
            status_code=400,
            detail="The issue must be assessed (Task 10) before a refund can be executed",
        )

    # The assessment's proposed_action is a recommendation, not a permission: the buyer may
    # choose either remedy for an eligible issue. Authorization is the Authority Engine
    # alone, re-run at execution time below — the single authorization source.
    # A return already accepted by a provider resolves the issue in kind, exactly like a
    # replacement (Task 12B extends the same mutual-exclusion rule).
    accepted_return = await _load_return(db, issue_id)
    _refuse_for_unreconciled_return(accepted_return, issue_id, "refund")
    if accepted_return is not None and accepted_return.status in {
        ReturnStatus.REQUESTED.value,
        ReturnStatus.CONFIRMED.value,
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Issue {issue_id} already has a return "
                f"('{accepted_return.status}', {accepted_return.return_id}) — a refund cannot "
                "also be executed for it"
            ),
        )

    # A replacement already taken by a provider resolves the issue in kind; refunding it as well
    # would resolve one problem twice (Task 12A mutual exclusion, enforced server-side).
    replacement = await _load_replacement(db, issue_id)
    if replacement is not None and replacement.status in {
        ReplacementStatus.REQUESTED.value,
        ReplacementStatus.CONFIRMED.value,
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Issue {issue_id} already has a replacement "
                f"('{replacement.status}', {replacement.replacement_id}) — a refund cannot also "
                "be executed for it"
            ),
        )

    amount, amount_paise, currency = _canonical_refund_amount(payment_data, order)

    # The same Authority Engine, re-run at execution time — the single authorization source.
    authority_data = (record.mandate or {}).get("authority")
    authority = Authority(**(authority_data or {"allowed_actions": [], "recovery_budget": "0"}))
    gate = evaluate(
        authority, ProposedAction(action=AllowedAction.REQUEST_REFUND, amount=amount)
    )
    if not gate.approved:
        await audit.append(
            db, mission_id, "buyer_agent", gate,
            note=f"refund execution refused at the gate for issue {issue_id}",
        )
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                f"The Authority Engine refused refund execution at execution time: {gate.reason}"
            ),
        )

    existing = await _load_refund(db, issue_id)
    if existing is not None and existing.status == RefundStatus.VERIFIED.value:
        return _refund_response(
            existing,
            agent_message=(
                f"Refund {existing.refund_id} was already verified "
                f"({existing.razorpay_refund_id}) — no new refund was created."
            ),
            already_existed=True,
        )

    refund = existing
    newly_claimed = False
    if refund is None:
        # Persist the claim BEFORE touching the provider: unique(issue_id) then makes any
        # concurrent/repeated attempt resolve to this row instead of issuing a second refund.
        refund = RefundRecord(
            issue_id=issue_id,
            mission_id=mission_id,
            order_id=order.order_id,
            user_id=record.user_id,
            razorpay_payment_id=payment_data["razorpay_payment_id"],
            amount=amount,
            amount_paise=amount_paise,
            currency=currency,
            status=RefundStatus.PENDING.value,
        )
        db.add(refund)
        await db.commit()
        newly_claimed = True

    status_changed_event: str | None = None

    if refund.status == RefundStatus.PENDING.value and refund.razorpay_refund_id:
        # Case C: already submitted — check the provider, never re-create.
        try:
            before = refund.status
            provider_view = fetch_refund(refund.razorpay_payment_id, refund.razorpay_refund_id)
            _settle_refund_from_provider(refund, provider_view.get("status"))
            if refund.status != before:
                status_changed_event = refund.status
        except Exception as exc:
            # Provider unreachable for a status check keeps it pending — never assumed done.
            refund.error_reason = _provider_error_text(exc)
    else:
        # Case A (first attempt) or D (retry of a failed / ambiguously-created refund).
        if not is_test_mode():
            refund.status = RefundStatus.FAILED.value
            refund.error_reason = "Refund execution is restricted to Razorpay TEST mode"
            status_changed_event = RefundStatus.FAILED.value
            await audit.append(
                db, mission_id, "buyer_agent", gate,
                note=f"refund {refund.refund_id} blocked: non-test Razorpay credentials",
            )
            await db.commit()
            raise HTTPException(status_code=400, detail=refund.error_reason)

        if refund.razorpay_refund_id is None and not newly_claimed:
            # A PREVIOUS attempt may have created the refund without its id being persisted.
            # Our claim is carried in the refund's notes — adopt it rather than issue a
            # second refund. (Best-effort: listing failures fall through to a create, which
            # then either succeeds visibly for us or fails on the provider's side.)
            try:
                for item in list_refunds_for_payment(refund.razorpay_payment_id):
                    if (item.get("notes") or {}).get("resolve_refund_id") == refund.refund_id:
                        refund.razorpay_refund_id = str(item.get("id") or "") or None
                        refund.provider_status = str(item.get("status") or "") or None
                        break
            except Exception:
                pass

        if refund.razorpay_refund_id is None:
            try:
                created = create_payment_refund(
                    refund.razorpay_payment_id, refund.amount_paise, refund.refund_id
                )
                refund.razorpay_refund_id = str(created.get("id") or "") or None
                refund.provider_status = str(created.get("status") or "") or None
                if refund.razorpay_refund_id is None:
                    raise RuntimeError("provider returned no refund id")
            except Exception as exc:
                refund.status = RefundStatus.FAILED.value
                refund.error_reason = _provider_error_text(exc)
                status_changed_event = RefundStatus.FAILED.value
                await audit.append(
                    db, mission_id, "buyer_agent", gate,
                    note=f"refund {refund.refund_id} provider call failed: {refund.error_reason}",
                )
                await db.commit()
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Razorpay refund could not be created; the failure was recorded on "
                        f"refund {refund.refund_id}. No verified refund exists."
                    ),
                )

        # Verify with the provider — the create response alone is never proof.
        try:
            provider_view = fetch_refund(refund.razorpay_payment_id, refund.razorpay_refund_id)
            _settle_refund_from_provider(refund, provider_view.get("status"))
            status_changed_event = refund.status
        except Exception as exc:
            refund.error_reason = _provider_error_text(exc)  # stays pending, never verified

    # Success advances the issue lifecycle deterministically (backend code only).
    if refund.status == RefundStatus.VERIFIED.value:
        issue.status = IssueStatus.RESOLUTION_PENDING.value

    await audit.append(
        db, mission_id, "buyer_agent", gate,
        note=(
            f"refund {refund.refund_id}: razorpay_refund_id={refund.razorpay_refund_id} "
            f"amount={refund.amount} {refund.currency} status={refund.status} "
            f"provider_status={refund.provider_status} refund_ceiling={refund_ceiling(authority)}"
        ),
    )
    await db.commit()

    return _refund_response(refund, agent_message=_refund_agent_message(refund))


# --------------------------------------------------------------------------- #
# Task 12A: controlled REPLACEMENT execution
#
# A replacement asks a merchant to send another unit of the canonical ordered product. It is a
# different capability from a refund: Razorpay moves money and is NOT a fulfilment provider, so
# nothing here may imply merchant acceptance. Execution is possible only through the same single
# Authority Engine gate as every other action, the product identity comes from the persisted order
# snapshot, and `requested`/`confirmed` require a real provider response.
# --------------------------------------------------------------------------- #
async def _load_replacement(db: AsyncSession, issue_id: str) -> ReplacementRecord | None:
    result = await db.execute(
        select(ReplacementRecord).where(ReplacementRecord.issue_id == issue_id)
    )
    return result.scalar_one_or_none()


def _replacement_response(
    replacement: ReplacementRecord,
    agent_message: str | None = None,
    already_existed: bool = False,
) -> dict:
    """Canonical client-facing replacement payload — every value read from the persisted row."""
    return {
        "replacement_id": replacement.replacement_id,
        "issue_id": replacement.issue_id,
        "mission_id": replacement.mission_id,
        "order_id": replacement.order_id,
        "user_id": replacement.user_id,
        "original_product_id": replacement.original_product_id,
        "original_title": replacement.original_title,
        "original_merchant": replacement.original_merchant,
        "replacement_product_id": replacement.replacement_product_id,
        "quantity": replacement.quantity,
        "value": str(replacement.value),
        "currency": replacement.currency,
        "provider": replacement.provider,
        "provider_reference": replacement.provider_reference,
        "provider_outcome": replacement.provider_outcome,
        "status": replacement.status,
        "error_reason": replacement.error_reason,
        "confirmed_at": replacement.confirmed_at.isoformat() if replacement.confirmed_at else None,
        "created_at": replacement.created_at.isoformat() if replacement.created_at else None,
        "updated_at": replacement.updated_at.isoformat() if replacement.updated_at else None,
        "already_existed": already_existed,
        "replacement_note": _replacement_note(replacement),
        "agent_message": agent_message,
    }


def _replacement_note(replacement: ReplacementRecord) -> str:
    """Plain-language meaning of the persisted status. Nothing here ever claims merchant
    acceptance, shipment, or delivery that a provider did not itself report."""
    if replacement.status == ReplacementStatus.CONFIRMED.value:
        return (
            "The connected replacement provider confirmed this replacement. That is the "
            "provider's own statement — it is not a claim about delivery."
        )
    if replacement.status == ReplacementStatus.REQUESTED.value:
        return (
            "A connected replacement provider accepted this replacement request. This is NOT "
            "shipment, delivery, or a completed resolution."
        )
    if replacement.status == ReplacementStatus.FAILED.value:
        return (
            f"The replacement provider failed this request"
            f"{' (' + replacement.error_reason + ')' if replacement.error_reason else ''}. "
            "No replacement exists."
        )
    return (
        "RESOLVE recorded the replacement intent, but no merchant replacement order exists: "
        + (replacement.error_reason or "no replacement provider has accepted it.")
    )


def _replacement_agent_message(replacement: ReplacementRecord) -> str:
    if replacement.status == ReplacementStatus.CONFIRMED.value:
        return (
            f"Replacement {replacement.replacement_id} confirmed by provider "
            f"{replacement.provider} (reference {replacement.provider_reference})."
        )
    if replacement.status == ReplacementStatus.REQUESTED.value:
        return (
            f"Replacement {replacement.replacement_id} was accepted by provider "
            f"{replacement.provider} as {replacement.provider_reference}. Awaiting the "
            "provider's own confirmation — nothing has been shipped as far as RESOLVE knows."
        )
    if replacement.status == ReplacementStatus.FAILED.value:
        return (
            f"Replacement {replacement.replacement_id} failed "
            f"({replacement.error_reason or 'provider failure'}). No replacement exists; "
            "retrying goes through the same record — never a second uncontrolled request."
        )
    return (
        f"Replacement {replacement.replacement_id} is recorded as RESOLVE's intent and stays "
        f"pending: {replacement.error_reason or 'no provider has accepted it'}. This is not a "
        "replacement having happened."
    )


def _replacement_request(order: ResolveOrderRecord, replacement: ReplacementRecord) -> ReplacementRequest:
    """The provider's input, assembled entirely from the canonical persisted order snapshot."""
    product = order.product or {}
    return ReplacementRequest(
        mission_id=replacement.mission_id,
        issue_id=replacement.issue_id,
        order_id=order.order_id,
        replacement_id=replacement.replacement_id,
        product_id=product.get("product_id") or order.product_id,
        title=order.title,
        merchant=order.merchant,
        currency=replacement.currency,
        value=Decimal(str(replacement.value)),
        quantity=replacement.quantity,
    )


@router.post("/{mission_id}/order/issues/{issue_id}/replacement", response_model=dict)
async def execute_issue_replacement(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    "Execute the controlled replacement via the shared executor function."
    return await run_replacement_executor(mission_id, issue_id, current_user, db)


async def run_replacement_executor(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None,
    db: AsyncSession,
):
    """
    Execute a controlled replacement for an assessed issue. There is NO request body: the product
    to replace, its merchant, quantity and value all come from the canonical persisted order, and
    authorization comes only from the Authority Engine.

    Idempotency: unique(issue_id) permits one replacement row per issue; a confirmed replacement is
    returned without any provider call; an already-requested one is read back from the provider
    instead of being re-submitted; a failed one is retried through the same row.

    With no merchant replacement provider connected (the current reality) the request cannot
    succeed: the row stays `pending`, the reason is stated, HTTP 503 is returned, and no
    replacement is claimed anywhere.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    issue = await _load_issue(db, mission_id, issue_id)

    assessment = await _load_assessment(db, issue_id)
    if assessment is None or assessment.issue_id != issue_id or assessment.mission_id != mission_id:
        raise HTTPException(
            status_code=400,
            detail="The issue must be assessed (Task 10) before a replacement can be executed",
        )

    # The assessment's proposed_action is a recommendation, not a permission: the buyer may
    # choose either remedy for an eligible issue. Authorization is the Authority Engine
    # alone, re-run at execution time below — the single authorization source.
    # A return already accepted by a provider occupies this issue the same way a replacement
    # does, so the two fulfilment paths cannot both run (Task 12B).
    accepted_return = await _load_return(db, issue_id)
    _refuse_for_unreconciled_return(accepted_return, issue_id, "replacement")
    if accepted_return is not None and accepted_return.status in {
        ReturnStatus.REQUESTED.value,
        ReturnStatus.CONFIRMED.value,
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Issue {issue_id} already has a return "
                f"('{accepted_return.status}', {accepted_return.return_id}) — a replacement "
                "cannot also be executed for it"
            ),
        )

    # Refund and replacement are mutually exclusive outcomes for one issue: a verified refund
    # has already reversed the payment, so asking a merchant to replace the same unit would
    # resolve it twice.
    refund = await _load_refund(db, issue_id)
    if refund is not None and refund.status == RefundStatus.VERIFIED.value:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Issue {issue_id} already has a verified refund ({refund.refund_id}) — a "
                "replacement cannot also be executed for it"
            ),
        )

    # The same Authority Engine, re-run at execution time: one authorization source only.
    authority_data = (record.mandate or {}).get("authority")
    authority = Authority(**(authority_data or {"allowed_actions": [], "recovery_budget": "0"}))
    gate = evaluate(authority, ProposedAction(action=AllowedAction.REQUEST_REPLACEMENT))
    if not gate.approved:
        await audit.append(
            db, mission_id, "buyer_agent", gate,
            note=f"replacement execution refused at the gate for issue {issue_id}",
        )
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                f"The Authority Engine refused replacement execution at execution time: "
                f"{gate.reason}"
            ),
        )

    existing = await _load_replacement(db, issue_id)
    if existing is not None and existing.status == ReplacementStatus.CONFIRMED.value:
        return _replacement_response(
            existing,
            agent_message=(
                f"Replacement {existing.replacement_id} was already confirmed by provider "
                f"{existing.provider} — no new request was made."
            ),
            already_existed=True,
        )

    provider = get_replacement_provider()
    replacement = existing
    pre_existing = existing is not None
    status_changed_event: str | None = None

    if replacement is None:
        # Persist the claim BEFORE touching any provider: unique(issue_id) then makes any
        # concurrent/repeated attempt resolve to this row instead of a second request.
        replacement = ReplacementRecord(
            issue_id=issue_id,
            mission_id=mission_id,
            order_id=order.order_id,
            user_id=record.user_id,
            original_product_id=(order.product or {}).get("product_id") or order.product_id,
            original_title=order.title,
            original_merchant=order.merchant,
            replacement_product_id=(order.product or {}).get("product_id") or order.product_id,
            quantity=1,
            value=Decimal(str(order.amount)),
            currency=order.currency or "INR",
            provider=provider.name,
            status=ReplacementStatus.PENDING.value,
        )
        db.add(replacement)
        await db.commit()

    if replacement.status == ReplacementStatus.REQUESTED.value and replacement.provider_reference:
        # Case B: a real provider already accepted this — read its state back, never re-ask.
        try:
            info = await provider.get_replacement_status(replacement.provider_reference)
            before = replacement.status
            replacement.provider_outcome = info.outcome
            if info.outcome in {"completed", "confirmed"}:
                replacement.status = ReplacementStatus.CONFIRMED.value
                replacement.confirmed_at = datetime.utcnow()
                replacement.error_reason = None
            elif info.outcome in {"declined", "cancelled", "failed"}:
                replacement.status = ReplacementStatus.FAILED.value
                replacement.error_reason = f"provider reported '{info.outcome}'"
            if replacement.status != before:
                status_changed_event = replacement.status
        except ReplacementError as exc:
            # Unreachable provider keeps the request exactly as it was — never assumed worse,
            # never assumed better.
            replacement.error_reason = _provider_error_text(exc)
    else:
        try:
            ack = await provider.request_replacement(_replacement_request(order, replacement))
        except ReplacementProviderUnavailable as exc:
            # The honest outcome when nothing is connected: intent recorded, nothing claimed.
            replacement.provider = provider.name
            replacement.error_reason = exc.message
            await audit.append(
                db, mission_id, "buyer_agent", gate,
                note=(
                    f"replacement {replacement.replacement_id} not submitted: no provider "
                    f"connected (provider={provider.name}, product="
                    f"{replacement.replacement_product_id}, value={replacement.value} "
                    f"{replacement.currency}, status={replacement.status})"
                ),
            )
            await db.commit()
            # The failure IS the outcome: return the persisted record alongside the reason,
            # with the provider-unavailable status code, rather than a bare error string.
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    **_replacement_response(
                        replacement,
                        agent_message=_replacement_agent_message(replacement),
                        already_existed=pre_existing,
                    ),
                    "detail": exc.message,
                },
            )
        except ReplacementError as exc:
            replacement.status = ReplacementStatus.FAILED.value
            replacement.error_reason = _provider_error_text(exc)
            status_changed_event = ReplacementStatus.FAILED.value
            await audit.append(
                db, mission_id, "buyer_agent", gate,
                note=(
                    f"replacement {replacement.replacement_id} provider call failed: "
                    f"{replacement.error_reason} (provider={provider.name}, "
                    f"status={replacement.status})"
                ),
            )
            await db.commit()
            detail = (
                "The replacement provider could not accept this request; the failure was "
                f"recorded on replacement {replacement.replacement_id}. No replacement exists."
            )
            return JSONResponse(
                status_code=502,
                content={
                    **_replacement_response(
                        replacement,
                        agent_message=_replacement_agent_message(replacement),
                        already_existed=pre_existing,
                    ),
                    "detail": detail,
                },
            )

        if not ack.accepted or not ack.provider_reference:
            replacement.status = ReplacementStatus.FAILED.value
            replacement.error_reason = "provider did not accept the replacement request"
            status_changed_event = ReplacementStatus.FAILED.value
        else:
            replacement.status = ReplacementStatus.REQUESTED.value
            replacement.provider = provider.name
            replacement.provider_reference = ack.provider_reference
            replacement.provider_outcome = "accepted"
            replacement.error_reason = None
            replacement.confirmed_at = None
            # Backend code alone advances the issue lifecycle, and only to what the provider said.
            issue.status = IssueStatus.REPLACEMENT_REQUESTED.value
            status_changed_event = ReplacementStatus.REQUESTED.value

    await audit.append(
        db, mission_id, "buyer_agent", gate,
        note=(
            f"replacement {replacement.replacement_id}: provider={replacement.provider} "
            f"provider_reference={replacement.provider_reference} "
            f"product={replacement.replacement_product_id} quantity={replacement.quantity} "
            f"value={replacement.value} {replacement.currency} status={replacement.status} "
            f"provider_outcome={replacement.provider_outcome}"
        ),
    )
    await db.commit()

    return _replacement_response(
        replacement,
        agent_message=_replacement_agent_message(replacement),
        already_existed=pre_existing,
    )


# --------------------------------------------------------------------------- #
# Task 12B: controlled RETURN execution
#
# A return asks a merchant to take the delivered goods back. Like replacement it moves no money
# through RESOLVE, so there is no amount ceiling — the bound is the deployment's own return switch
# plus the mandate having delegated request_return. Razorpay is not a return provider and is never
# consulted here. With no merchant return provider connected, the record stays `pending` and the
# response says so plainly: no label, no approval, no completed return.
# --------------------------------------------------------------------------- #
async def _load_return(db: AsyncSession, issue_id: str) -> ReturnRecord | None:
    result = await db.execute(
        select(ReturnRecord).where(ReturnRecord.issue_id == issue_id)
    )
    return result.scalar_one_or_none()


def _refuse_for_unreconciled_return(
    return_record: ReturnRecord | None, issue_id: str, action: str
) -> None:
    """
    Hard conflict: a return whose outcome is unknown occupies this issue until a read-only
    reconciliation decides what the merchant actually holds.

    `outcome_unknown` means a submission left RESOLVE and its answer was lost, so a return may or
    may not exist. Refunding or replacing while that is unresolved would compensate the same
    problem twice on a state we cannot see. The refusal happens before any provider is touched,
    so it makes no network call and writes nothing. Other return statuses keep their existing
    behaviour: `requested`/`confirmed` are the accepted-return exclusion above, and
    `pending`/`failed` mean no provider return exists and stay unaffected.
    """
    if return_record is not None and return_record.status == ReturnStatus.OUTCOME_UNKNOWN.value:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Issue {issue_id} has a return ({return_record.return_id}) whose outcome is "
                f"unknown — {action} execution is refused until the return provider has been "
                "read back and that outcome reconciled"
            ),
        )


def _return_note(return_record: ReturnRecord) -> str:
    """Plain-language meaning of the persisted status. Never claims approval, a label, shipment,
    or a completed return that a provider did not itself report."""
    if return_record.status == ReturnStatus.CONFIRMED.value:
        return (
            "The connected return provider confirmed this return. That is the provider's own "
            "statement — RESOLVE does not claim the goods have moved."
        )
    if return_record.status == ReturnStatus.REQUESTED.value:
        return (
            "A connected return provider accepted this return request. This is NOT an approved, "
            "labelled, shipped, or completed return."
        )
    if return_record.status == ReturnStatus.FAILED.value:
        return (
            "The return provider failed this request"
            + (f" ({return_record.failure_reason})" if return_record.failure_reason else "")
            + ". No return exists."
        )
    if return_record.status == ReturnStatus.OUTCOME_UNKNOWN.value:
        return (
            "RESOLVE submitted this return and never received an answer it can trust, so it does "
            "not know whether the merchant holds a return for it. It will find out by reading the "
            "merchant back — never by sending the same request a second time."
            + (f" ({return_record.failure_reason})" if return_record.failure_reason else "")
        )
    return (
        "RESOLVE recorded the return intent, but no merchant return exists: "
        + (return_record.failure_reason or "no provider has accepted it.")
    )


def _return_response(
    return_record: ReturnRecord,
    agent_message: str | None = None,
    already_existed: bool = False,
) -> dict:
    """Canonical client-facing return payload — every value read from the persisted row."""
    return {
        "return_id": return_record.return_id,
        "issue_id": return_record.issue_id,
        "mission_id": return_record.mission_id,
        "order_id": return_record.order_id,
        "user_id": return_record.user_id,
        "original_product_id": return_record.original_product_id,
        "original_title": return_record.original_title,
        "provider": return_record.provider,
        "provider_reference": return_record.provider_reference,
        "provider_outcome": return_record.provider_outcome,
        "status": return_record.status,
        "failure_reason": return_record.failure_reason,
        "requested_at": (
            return_record.requested_at.isoformat() if return_record.requested_at else None
        ),
        "confirmed_at": (
            return_record.confirmed_at.isoformat() if return_record.confirmed_at else None
        ),
        "created_at": return_record.created_at.isoformat() if return_record.created_at else None,
        "updated_at": return_record.updated_at.isoformat() if return_record.updated_at else None,
        "already_existed": already_existed,
        "return_note": _return_note(return_record),
        "agent_message": agent_message,
    }


def _return_agent_message(return_record: ReturnRecord) -> str:
    if return_record.status == ReturnStatus.CONFIRMED.value:
        return (
            f"Return {return_record.return_id} confirmed by provider "
            f"{return_record.provider} (reference {return_record.provider_reference})."
        )
    if return_record.status == ReturnStatus.REQUESTED.value:
        return (
            f"Return {return_record.return_id} was accepted by provider "
            f"{return_record.provider} as {return_record.provider_reference}. Awaiting the "
            "provider's own confirmation — nothing has been shipped as far as RESOLVE knows."
        )
    if return_record.status == ReturnStatus.FAILED.value:
        return (
            f"Return {return_record.return_id} failed "
            f"({return_record.failure_reason or 'provider failure'}). No return exists; "
            "retrying goes through the same record — never a second uncontrolled request."
        )
    if return_record.status == ReturnStatus.OUTCOME_UNKNOWN.value:
        return (
            f"Return {return_record.return_id} was submitted to provider "
            f"{return_record.provider} and its answer was lost, so RESOLVE cannot say whether the "
            "merchant has it. The next attempt reads the provider back instead of submitting "
            "again; nothing is retried blind."
        )
    return (
        f"Return {return_record.return_id} is recorded as RESOLVE's intent and stays pending: "
        f"{return_record.failure_reason or 'no provider has accepted it'}. This is not a return "
        "having happened."
    )


def _return_request(
    order: ResolveOrderRecord, issue: OrderIssueRecord, return_record: ReturnRecord
) -> ReturnRequest:
    """
    The provider's input, assembled entirely from canonical persisted rows.

    `provider_order_reference` is read from the persisted order, never from the request: a client
    has no way to name a merchant-side order, and an executor that cannot see one must refuse
    rather than look for a likely-looking substitute.

    The two product/variant references (Task 14C.7E) come from the same row's Shopify identity
    columns, which only 14C.7D's server-side selection path ever writes. A Google Shopping order has
    nothing there, and a provider that receives nothing must narrow nothing — the absence is
    carried through rather than being filled in from the title.
    """
    product = order.product or {}
    return ReturnRequest(
        mission_id=return_record.mission_id,
        issue_id=return_record.issue_id,
        order_id=order.order_id,
        return_id=return_record.return_id,
        product_id=product.get("product_id") or order.product_id,
        title=order.title,
        merchant=order.merchant,
        reason=issue.description,
        issue_type=issue.issue_type,
        provider_order_reference=order.provider_order_reference,
        provider_product_reference=order.shopify_product_gid,
        provider_variant_reference=order.shopify_variant_gid,
    )


def _return_submission(return_record: ReturnRecord) -> ReturnSubmission:
    """What this row says RESOLVE sent (or may have sent) — the input to a read-only reconcile."""
    return ReturnSubmission(
        return_id=return_record.return_id,
        issue_id=return_record.issue_id,
        provider_order_reference=return_record.provider_order_reference,
        provider_line_reference=return_record.provider_line_reference,
        provider_quantity=return_record.provider_quantity,
        provider_reason=return_record.provider_reason,
        submitted_at=return_record.submission_claimed_at,
    )


async def _claim_return_submission(
    db: AsyncSession, return_record: ReturnRecord, target: ReturnTarget | None
) -> bool:
    """
    Atomically record what is about to be sent, and take ownership of the one attempt to send it.

    The UPDATE is conditional on the row still being claimable, so two concurrent requests cannot
    both decide to submit: only the one whose statement matched a row holds the claim. When no
    target exists (no merchant channel needs one) the row is still stamped, so the attempt has an
    identity and a time of its own. Returns False when this caller must not submit.
    """
    nonce = uuid.uuid4().hex
    now = datetime.utcnow()
    values: dict = {"submission_claim": nonce, "submission_claimed_at": now}
    if target is not None:
        values.update(
            provider_order_reference=target.provider_order_reference,
            provider_line_reference=target.provider_line_reference,
            provider_quantity=target.provider_quantity,
            provider_reason=target.provider_reason,
        )
    result = await db.execute(
        update(ReturnRecord)
        .where(
            ReturnRecord.return_id == return_record.return_id,
            ReturnRecord.submission_claim.is_(None),
            ReturnRecord.status.in_(
                [ReturnStatus.PENDING.value, ReturnStatus.FAILED.value]
            ),
        )
        .values(**values)
    )
    if result.rowcount != 1:
        return False
    return_record.submission_claim = nonce
    return_record.submission_claimed_at = now
    if target is not None:
        return_record.provider_order_reference = target.provider_order_reference
        return_record.provider_line_reference = target.provider_line_reference
        return_record.provider_quantity = target.provider_quantity
        return_record.provider_reason = target.provider_reason
    await db.commit()
    return True


async def _release_return_claim(return_record: ReturnRecord) -> None:
    """Give up this attempt's claim, so a later deliberate attempt can take it. Never called after
    a submission whose outcome is unknown — that is what keeps a lost answer from being re-sent.
    The caller's own commit persists it."""
    return_record.submission_claim = None
    return_record.submission_claimed_at = None


async def _reconcile_unknown_return(
    db: AsyncSession,
    mission_id: str,
    issue_id: str,
    gate,
    provider,
    return_record: ReturnRecord,
    issue: OrderIssueRecord,
    pre_existing: bool,
):
    """
    Settle a return whose submission answer was lost, using ONLY reads (Task 14C.4).

    `request_return` is never called from here. If the provider can point at exactly one merchant
    return that matches what RESOLVE recorded sending, that return is adopted — reference, status,
    and all — and the record leaves 'outcome_unknown' on the provider's own word. If it cannot,
    the record stays 'outcome_unknown' and the caller is told so plainly. Preferring silence over
    a guess is the point: a duplicate return at a merchant is not something a read can undo.
    """
    status_changed_event: str | None = None
    try:
        result = await provider.reconcile_return(_return_submission(return_record))
        state = str(getattr(result, "state", "") or "").lower()
        reference = getattr(result, "provider_reference", None)
        outcome = getattr(result, "outcome", None)
        detail = getattr(result, "detail", None)
    except ReturnError as exc:
        state, reference, outcome = "", None, None
        detail = _provider_error_text(exc)

    adopted = state == "matched" and bool(reference)
    if adopted:
        return_record.provider = provider.name
        return_record.provider_reference = reference
        return_record.provider_outcome = outcome
        return_record.failure_reason = None
        await _release_return_claim(return_record)
        if outcome in {"confirmed", "completed"}:
            return_record.status = ReturnStatus.CONFIRMED.value
            return_record.confirmed_at = datetime.utcnow()
        elif outcome in {"declined", "cancelled", "failed"}:
            # The merchant itself said no to the return that was found — a read answer, not a
            # re-submission, so 'failed' is exactly right here.
            return_record.status = ReturnStatus.FAILED.value
            return_record.failure_reason = f"provider reported '{outcome}'"
        else:
            return_record.status = ReturnStatus.REQUESTED.value
            return_record.requested_at = return_record.requested_at or datetime.utcnow()
            # Backend code alone advances the issue lifecycle, and only to what the provider said.
            issue.status = IssueStatus.RETURN_REQUESTED.value
        status_changed_event = return_record.status
        note = f"return {return_record.return_id} ADOPTED by reconciliation from {reference}"
    else:
        # Nothing was established, so nothing changes but the reason. The claim is kept: it is the
        # evidence that an attempt exists somewhere whose fate is unknown.
        return_record.failure_reason = detail or (
            "The provider could not identify the submitted return, so its outcome remains unknown."
        )
        note = (
            f"return {return_record.return_id} still outcome UNKNOWN after reconciliation "
            f"({return_record.failure_reason})"
        )

    await audit.append(
        db, mission_id, "buyer_agent", gate,
        note=(
            f"{note} (provider={provider.name}, "
            f"line={return_record.provider_line_reference}, "
            f"quantity={return_record.provider_quantity}, status={return_record.status})"
        ),
    )
    await db.commit()

    if not adopted:
        return JSONResponse(
            status_code=503,
            content={
                **_return_response(
                    return_record,
                    agent_message=_return_agent_message(return_record),
                    already_existed=pre_existing,
                ),
                "detail": (
                    "The outcome of the submitted return is still unknown, so RESOLVE did not "
                    "submit it again. This needs the provider's records — or a human — to settle."
                ),
            },
        )
    return _return_response(
        return_record,
        agent_message=_return_agent_message(return_record),
        already_existed=pre_existing,
    )


@router.post("/{mission_id}/order/issues/{issue_id}/return", response_model=dict)
async def execute_issue_return(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    "Execute the controlled return via the shared executor function."
    return await run_return_executor(mission_id, issue_id, current_user, db)


async def run_return_executor(
    mission_id: str,
    issue_id: str,
    current_user: UserRecord | None,
    db: AsyncSession,
):
    """
    Execute a controlled return for an assessed issue. There is NO request body: the order, the
    product being returned and the reason all come from the canonical persisted rows, and
    authorization comes only from the Authority Engine.

    The order of operations is the safety property (Task 14C.4): authenticate, load the canonical
    rows, re-check the assessment, re-run the Authority Engine, check for a conflicting
    refund/replacement, find the existing record, resolve the merchant-side target from the
    provider's OWN reads, persist that submission and take the claim — and only then submit.
    Nothing is sent before a row exists that says what was sent.

    Idempotency, per persisted state:
      confirmed       : returned as-is, no provider call at all
      requested       : read back from the provider, never re-submitted
      outcome_unknown : the answer to a submission was lost, so this does a READ-ONLY
                        reconciliation and never a second mutation — a duplicate the merchant
                        already accepted cannot be undone by a read
      pending/failed  : claimable, and only one concurrent attempt can take the claim

    With no merchant return provider connected (the current reality) the request cannot succeed:
    the row stays `pending`, the reason is stated, HTTP 503 is returned, and no return is claimed.
    """
    record = await _load_mission_for_shopping_action(mission_id, current_user, db)
    order = await _load_order(db, mission_id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order exists for this mission")
    issue = await _load_issue(db, mission_id, issue_id)

    assessment = await _load_assessment(db, issue_id)
    if assessment is None or assessment.issue_id != issue_id or assessment.mission_id != mission_id:
        raise HTTPException(
            status_code=400,
            detail="The issue must be assessed (Task 10) before a return can be executed",
        )
    if assessment.authority_decision != "allow":
        raise HTTPException(
            status_code=400,
            detail=(
                f"The Authority Engine decision on this assessment is "
                f"'{assessment.authority_decision}' — return execution requires an explicit allow"
            ),
        )
    if assessment.proposed_action != AllowedAction.REQUEST_RETURN.value:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The resolution assessment proposed '{assessment.proposed_action}', not "
                "'request_return' — return execution is not authorized by the assessment"
            ),
        )

    # Outcomes already delivered by another executor are mutually exclusive with a return.
    refund = await _load_refund(db, issue_id)
    if refund is not None and refund.status == RefundStatus.VERIFIED.value:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Issue {issue_id} already has a verified refund ({refund.refund_id}) — a "
                "return cannot also be executed for it"
            ),
        )
    replacement = await _load_replacement(db, issue_id)
    if replacement is not None and replacement.status in {
        ReplacementStatus.REQUESTED.value,
        ReplacementStatus.CONFIRMED.value,
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Issue {issue_id} already has a replacement "
                f"('{replacement.status}', {replacement.replacement_id}) — a return cannot also "
                "be executed for it"
            ),
        )

    # The same Authority Engine, re-run at execution time: one authorization source only.
    authority_data = (record.mandate or {}).get("authority")
    authority = Authority(**(authority_data or {"allowed_actions": [], "recovery_budget": "0"}))
    gate = evaluate(authority, ProposedAction(action=AllowedAction.REQUEST_RETURN))
    if not gate.approved:
        await audit.append(
            db, mission_id, "buyer_agent", gate,
            note=f"return execution refused at the gate for issue {issue_id}",
        )
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                "The Authority Engine refused return execution at execution time: "
                f"{gate.reason}"
            ),
        )

    existing = await _load_return(db, issue_id)
    if existing is not None and existing.status == ReturnStatus.CONFIRMED.value:
        return _return_response(
            existing,
            agent_message=(
                f"Return {existing.return_id} was already confirmed by provider "
                f"{existing.provider} — no new request was made."
            ),
            already_existed=True,
        )

    provider = get_return_provider()
    return_record = existing
    pre_existing = existing is not None
    status_changed_event: str | None = None

    if return_record is None:
        # Persist the claim BEFORE touching any provider: unique(issue_id) then makes any
        # concurrent/repeated attempt resolve to this row instead of a second request.
        return_record = ReturnRecord(
            issue_id=issue_id,
            mission_id=mission_id,
            order_id=order.order_id,
            user_id=record.user_id,
            original_product_id=(order.product or {}).get("product_id") or order.product_id,
            original_title=order.title,
            provider=provider.name,
            status=ReturnStatus.PENDING.value,
        )
        db.add(return_record)
        await db.commit()

    if return_record.status == ReturnStatus.OUTCOME_UNKNOWN.value:
        # A previous submission went out and its answer was lost. This branch reads the provider
        # back and NOTHING ELSE — calling request_return from here is exactly the duplicate a
        # merchant would have no way to tell us about.
        return await _reconcile_unknown_return(
            db, mission_id, issue_id, gate, provider, return_record, issue, pre_existing
        )

    if return_record.status == ReturnStatus.REQUESTED.value and return_record.provider_reference:
        # Case B: a real provider already accepted this — read its state back, never re-ask.
        try:
            info = await provider.get_return_status(return_record.provider_reference)
            before = return_record.status
            return_record.provider_outcome = info.outcome
            if info.outcome in {"confirmed", "completed"}:
                return_record.status = ReturnStatus.CONFIRMED.value
                return_record.confirmed_at = datetime.utcnow()
                return_record.failure_reason = None
            elif info.outcome in {"declined", "cancelled", "failed"}:
                return_record.status = ReturnStatus.FAILED.value
                return_record.failure_reason = f"provider reported '{info.outcome}'"
            if return_record.status != before:
                status_changed_event = return_record.status
            # This read answered, so the submission is accounted for and holds no claim anymore.
            await _release_return_claim(return_record)
        except ReturnError as exc:
            # An unreachable provider keeps the return exactly as it was — never assumed
            # better, never assumed worse.
            return_record.failure_reason = _provider_error_text(exc)
    else:
        base_request = _return_request(order, issue, return_record)

        # Resolve the merchant-side target BEFORE anything is claimed or sent, from the provider's
        # own reads and the persisted linkage — never from the request. A provider that needs no
        # separate target returns None, which changes nothing for it.
        try:
            target = await provider.resolve_return_target(base_request)
        except ReturnError as exc:
            # Nothing was claimed and nothing was submitted, so the record honestly stays where it
            # was with the reason stated. An unresolvable target is never guessed around.
            return_record.provider = provider.name
            return_record.failure_reason = exc.message
            await audit.append(
                db, mission_id, "buyer_agent", gate,
                note=(
                    f"return {return_record.return_id} not submitted: no canonical merchant-side "
                    f"return target could be resolved ({return_record.failure_reason}; "
                    f"provider={provider.name}, status={return_record.status})"
                ),
            )
            await db.commit()
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    **_return_response(
                        return_record,
                        agent_message=_return_agent_message(return_record),
                        already_existed=pre_existing,
                    ),
                    "detail": exc.message,
                },
            )

        # Persist what is about to be sent, and take the single claim that authorizes this attempt
        # to send it. If the row was already claimed, no second submission goes out.
        if not await _claim_return_submission(db, return_record, target):
            exc_message = (
                f"Return {return_record.return_id} already has a submission attempt in progress, "
                "so this request did not submit anything. Its outcome will be read back rather "
                "than re-sent."
            )
            await audit.append(
                db, mission_id, "buyer_agent", gate,
                note=(
                    f"return {return_record.return_id} not submitted: another attempt holds the "
                    f"submission claim (provider={provider.name}, status={return_record.status})"
                ),
            )
            return JSONResponse(
                status_code=503,
                content={
                    **_return_response(
                        return_record,
                        agent_message=_return_agent_message(return_record),
                        already_existed=pre_existing,
                    ),
                    "detail": exc_message,
                },
            )

        try:
            ack = await provider.request_return(base_request, target)
        except ReturnOutcomeUnknown as exc:
            # The submission may have landed. Saying 'failed' here would assert that it did not,
            # which is the one thing RESOLVE cannot know — so the claim is KEPT and the state says
            # unknown, and only a read can move it. No automatic retry, ever, from this path.
            return_record.status = ReturnStatus.OUTCOME_UNKNOWN.value
            return_record.provider = provider.name
            return_record.failure_reason = exc.message
            status_changed_event = ReturnStatus.OUTCOME_UNKNOWN.value
            await audit.append(
                db, mission_id, "buyer_agent", gate,
                note=(
                    f"return {return_record.return_id} outcome UNKNOWN after submission: "
                    f"{return_record.failure_reason} (provider={provider.name}, "
                    f"line={return_record.provider_line_reference}, "
                    f"quantity={return_record.provider_quantity}, claim retained)"
                ),
            )
            await db.commit()
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    **_return_response(
                        return_record,
                        agent_message=_return_agent_message(return_record),
                        already_existed=pre_existing,
                    ),
                    "detail": (
                        "The return was submitted and its answer was lost, so RESOLVE does not "
                        f"claim to know the outcome. Return {return_record.return_id} is recorded "
                        "as outcome-unknown and will be read back from the provider; the request "
                        "will not be re-sent."
                    ),
                },
            )
        except ReturnProviderUnavailable as exc:
            # The provider said, before doing anything, that it could not act: intent recorded,
            # nothing submitted, and the claim is handed back.
            await _release_return_claim(return_record)
            return_record.provider = provider.name
            return_record.failure_reason = exc.message
            await audit.append(
                db, mission_id, "buyer_agent", gate,
                note=(
                    f"return {return_record.return_id} not submitted: no provider connected "
                    f"(provider={provider.name}, product={return_record.original_product_id}, "
                    f"status={return_record.status})"
                ),
            )
            await db.commit()
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    **_return_response(
                        return_record,
                        agent_message=_return_agent_message(return_record),
                        already_existed=pre_existing,
                    ),
                    "detail": exc.message,
                },
            )
        except ReturnError as exc:
            await _release_return_claim(return_record)
            return_record.status = ReturnStatus.FAILED.value
            return_record.failure_reason = _provider_error_text(exc)
            status_changed_event = ReturnStatus.FAILED.value
            await audit.append(
                db, mission_id, "buyer_agent", gate,
                note=(
                    f"return {return_record.return_id} provider call failed: "
                    f"{return_record.failure_reason} (provider={provider.name}, "
                    f"status={return_record.status})"
                ),
            )
            await db.commit()
            detail = (
                "The return provider could not accept this request; the failure was recorded on "
                f"return {return_record.return_id}. No return exists."
            )
            return JSONResponse(
                status_code=502,
                content={
                    **_return_response(
                        return_record,
                        agent_message=_return_agent_message(return_record),
                        already_existed=pre_existing,
                    ),
                    "detail": detail,
                },
            )

        if not ack.accepted or not ack.provider_reference:
            await _release_return_claim(return_record)
            return_record.status = ReturnStatus.FAILED.value
            return_record.failure_reason = "provider did not accept the return request"
            status_changed_event = ReturnStatus.FAILED.value
        else:
            # The answer arrived, so the attempt is accounted for and its claim is handed back;
            # what was sent stays on the row as the record of this submission.
            await _release_return_claim(return_record)
            return_record.status = ReturnStatus.REQUESTED.value
            return_record.provider = provider.name
            return_record.provider_reference = ack.provider_reference
            return_record.provider_outcome = "accepted"
            return_record.failure_reason = None
            return_record.requested_at = datetime.utcnow()
            return_record.confirmed_at = None
            # Backend code alone advances the issue lifecycle, and only to what the provider said.
            issue.status = IssueStatus.RETURN_REQUESTED.value
            status_changed_event = ReturnStatus.REQUESTED.value

    await audit.append(
        db, mission_id, "buyer_agent", gate,
        note=(
            f"return {return_record.return_id}: provider={return_record.provider} "
            f"provider_reference={return_record.provider_reference} "
            f"product={return_record.original_product_id} "
            f"status={return_record.status} provider_outcome={return_record.provider_outcome}"
        ),
    )
    await db.commit()

    return _return_response(
        return_record,
        agent_message=_return_agent_message(return_record),
        already_existed=pre_existing,
    )


@router.post("", response_model=dict)
async def create_mission(
    payload: CreateMissionRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    if current_user:
        # Never trust client-supplied user_id for authenticated requests
        effective_user_id = current_user.id
    else:
        # Legacy unauthenticated demo-user request
        effective_user_id = payload.user_id or "demo-user"

    # Check if this is a shopping mission request
    if payload.mission_type == "shopping" or (payload.prompt and not payload.objective):
        prompt_text = payload.prompt or (payload.objective.description if payload.objective else "")
        return await _process_new_shopping_mission(prompt_text, effective_user_id, db)

    # Recovery mission flow (backwards compatible)
    if not payload.objective or not payload.authority or not payload.transaction:
        raise HTTPException(
            status_code=422,
            detail="Objective, authority, and transaction are required for recovery missions."
        )

    mandate = RecoveryMandate(
        mission_id="pending", objective=payload.objective, authority=payload.authority
    )
    mission = Mission(
        user_id=effective_user_id,
        mission_type="recovery",
        mandate=mandate,
        transaction=payload.transaction,
    )
    mission.mandate.mission_id = mission.mission_id

    record = MissionRecord(
        mission_id=mission.mission_id,
        user_id=mission.user_id,
        mission_type="recovery",
        status=mission.status.value,
        mandate=mission.mandate.model_dump(mode="json"),
        transaction=mission.transaction.model_dump(mode="json"),
        evidence=[],
    )
    db.add(record)
    await db.commit()

    return mission.to_intent_receipt()


@router.get("/{mission_id}", response_model=dict)
async def get_mission(
    mission_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)
    return _record_to_receipt(record)


@router.get("", response_model=list[dict])
async def list_missions(
    user_id: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    The caller's own missions, and only ever the caller's own.

    `user_id` is accepted and ignored. It used to be the whole shape of this route: an authenticated
    request saw its own missions and an unauthenticated one saw the `demo-user` namespace, so a
    visitor with no session could read a stranger's mission list — every id, budget, delegated action
    and spend total in it. There is no anonymous caller left to serve: the workspace this feeds
    redirects to `/login` before it asks. The parameter stays in the signature so nothing that still
    sends it breaks, and it still carries no authority, because the session is the only thing that
    decides what is listed.
    """
    result = await db.execute(
        select(MissionRecord).where(MissionRecord.user_id == current_user.id)
    )
    return [_record_to_receipt(r) for r in result.scalars().all()]


class ReportDeviationRequest(BaseModel):
    deviation: str
    evidence: list[EvidenceItem] = []


@router.post("/{mission_id}/deviation", response_model=dict)
async def report_deviation(
    mission_id: str,
    payload: ReportDeviationRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Entry point for 'something went wrong' — a user uploading a damage photo, or the
    Observation Agent detecting a carrier exception event. Opens the claim, which is the first
    step of the negotiation protocol.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)

    record.status = MissionStatus.DEVIATION_DETECTED.value
    record.evidence = [*record.evidence, *[e.model_dump(mode="json") for e in payload.evidence]]

    reasoning = get_reasoning_provider(settings.reasoning_provider, settings.reasoning_api_key)
    buyer = BuyerAgent(reasoning)
    mission = _record_to_mission(record)

    claim = await buyer.open_claim(mission, payload.deviation)
    record.status = mission.status.value

    msg_record = MessageRecord(
        message_id=claim.message_id,
        mission_id=mission_id,
        sender=claim.sender.value,
        type=claim.type.value,
        payload=claim.model_dump(mode="json"),
    )
    db.add(msg_record)
    await db.commit()

    return _record_to_receipt(record)


class OfferResponseRequest(BaseModel):
    offered_remedy: str
    offered_amount: Decimal | None = None


@router.post("/{mission_id}/offer", response_model=dict)
async def receive_offer(
    mission_id: str,
    payload: OfferResponseRequest,
    current_user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Called when an offer arrives from the Merchant Agent (or is transcribed from a human
    merchant's reply). Runs it through the Buyer Agent's evaluation AND the Authority Engine
    before anything is accepted.

    The buyer who owns the mission has to be the one presenting an offer about it. This route used to
    take no identity at all, which left it outside the rule every other mission route follows: an
    anonymous caller — or any signed-in stranger — who could name a mission id could put an offer in
    front of its mandate, and an accepted offer writes `total_recovery_spend` and moves the mission to
    `executing`. The Authority Engine still decided whether the offer was acceptable, and it still
    does; what it was never designed to decide is whether the person walking up to it is entitled to
    be standing there. Ownership is that question, and one guard answers it everywhere: 401 with no
    session, 404 for somebody else's mission, so a wrong id and a foreign id look identical.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)

    mission = _record_to_mission(record)
    reasoning = get_reasoning_provider(settings.reasoning_provider, settings.reasoning_api_key)
    buyer = BuyerAgent(reasoning)

    verdict, decision = await buyer.evaluate_incoming_offer(
        mission, payload.offered_remedy, payload.offered_amount
    )

    if decision is not None:
        await audit.append(db, mission_id, "buyer_agent", decision)

    if verdict == "accept" and decision and decision.approved:
        from app.authority.engine import apply_spend

        mission.mandate.authority = apply_spend(mission.mandate.authority, payload.offered_amount)
        mission.status = MissionStatus.EXECUTING
        record.total_recovery_spend = float(payload.offered_amount)
        record.status = mission.status.value
        record.mandate = mission.mandate.model_dump(mode="json")
    elif verdict == "escalate":
        mission.status = MissionStatus.ESCALATED
        record.status = mission.status.value
    elif verdict == "counter":
        mission.status = MissionStatus.NEGOTIATING
        record.status = mission.status.value

    await db.commit()
    return _record_to_receipt(record)


@router.post("/{mission_id}/resolve", response_model=dict)
async def mark_resolved(
    mission_id: str,
    summary: str,
    current_user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Called ONLY after the Verification Engine has confirmed the outcome against real system
    state (a Razorpay webhook, a carrier delivery confirmation, etc. — see
    app/routers/webhooks.py). Never called on the strength of an action having been sent.

    Owner-authenticated, because "confirmed" is the sentence that closes a buyer's case. This route
    took no identity and had no callers: anyone who could name a mission id could write
    `resolution_summary` over it and move it to `resolved`, which is how an unresolved dispute would
    stop looking unresolved to everyone who reads the mission afterwards. A machine confirmation has
    always had a better door — a signed integration surface authenticating itself for that purpose,
    the way `POST /integrations/shopify/order-linkage` does — and an open HTTP route is not a
    substitute for one.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)

    record.status = MissionStatus.RESOLVED.value
    record.resolution_summary = summary
    await db.commit()

    return _record_to_receipt(record)


def _apply_conversational_state(record: MissionRecord, intent: ShoppingIntent, candidate: str) -> None:
    """
    Write a state a conversation is allowed to write, to both places a shopping mission keeps it.

    `MissionRecord.status` and `shopping_intent.state` are one fact held in two columns, so they are
    moved together or not at all. A mission that has already selected, confirmed, paid, or ordered
    keeps its status, and its intent state is written to match — a chat message can refine what the
    buyer wants, it cannot put the case file back to the beginning.
    """
    settled = guarded_shopping_status(record.status, candidate)
    intent.state = settled
    record.status = settled


def _carry_forward_shopping_state(intent: ShoppingIntent, previous: dict) -> None:
    """
    Keep what the mission already has when a new message re-parses the intent.

    The intent parser's answer describes what the buyer wants now; it has no idea that a search
    already ran, a product was chosen, a checkout was confirmed, or a payment was verified. Those are
    facts about the mission, held in the persisted intent, and a refinement must never silently
    discard them — a conversation that forgot its own product list could not continue from itself.
    """
    if not intent.discovered_products and previous.get("discovered_products"):
        intent.discovered_products = [
            ProductResult(**product) for product in previous["discovered_products"]
        ]
        intent.discovery_query = previous.get("discovery_query")
    if intent.selected_product is None and previous.get("selected_product"):
        intent.selected_product = ProductResult(**previous["selected_product"])
    if intent.checkout is None and previous.get("checkout"):
        intent.checkout = CheckoutConfirmation(**previous["checkout"])
    if intent.payment is None and previous.get("payment"):
        intent.payment = ShoppingPayment(**previous["payment"])
    # A parse only knows what the buyer asked for; it does not know that a search already answered
    # that question. When results are still on the mission and nothing new is missing, the mission is
    # still at discovery_completed — and saying otherwise would describe the case wrongly to both
    # the model and the UI.
    if intent.discovered_products and intent.state in ("shopping_intent", "ready_for_discovery"):
        intent.state = "discovery_completed"


async def _agent_shopping_turn(
    record: MissionRecord,
    user_input: str,
    current_user: UserRecord | None,
    db: AsyncSession,
    reasoning: object,
) -> dict:
    """
    One bounded agent turn on a shopping mission (Task CORE-2).

    The message first moves the mission's structured intent forward — deterministically. The
    reasoning provider interprets the user's latest message against the canonical current
    intent and proposes structured constraint updates; this server, not the model, decides
    what the intent becomes (CORE-5 Phase 3): a closed field registry, a fixed operation
    vocabulary, ambiguity applying zero updates, and the mission's own facts (products,
    selection, checkout, payment) carried through untouched. A reasoning outage keeps the
    degradation contract the shopping flow has always had: the request is preserved, the
    mission parks in a retryable state, and the caller gets a readable sentence instead of a 500.
    """
    curr_intent_data = record.shopping_intent or {}
    model_input = compose_intent_turn_input(curr_intent_data, user_input)

    try:
        parsed = await reasoning.interpret_shopping_intent(model_input)  # type: ignore[attr-defined]
        merged_data = merge_intent_turn(curr_intent_data, parsed, user_input)
        updated_intent = ShoppingIntent(**merged_data)
        _carry_forward_shopping_state(updated_intent, curr_intent_data)
        # The parse's own state is a proposal about the ladder, not a fact about the mission: the
        # guard decides whether it may be written.
        _apply_conversational_state(record, updated_intent, updated_intent.state)
    except Exception as exc:  # noqa: BLE001 — an unreachable model must never 500 a conversation
        updated_intent = (
            ShoppingIntent(**curr_intent_data)
            if curr_intent_data
            else ShoppingIntent(raw_prompt=user_input)
        )
        response_text = (
            f"Reasoning service temporarily unavailable: {exc}. "
            "I've kept your mission safe, please try again in a moment."
        )
        _apply_conversational_state(record, updated_intent, MissionStatus.NEEDS_CLARIFICATION.value)
        record.shopping_intent = updated_intent.model_dump(mode="json")
        record.resolution_summary = response_text
        await _record_turn_bookends(db, record, user_input, response_text)
        await db.commit()
        return _agent_response(
            record,
            user_input,
            AgentTurnOutcome(
                agent_message=response_text,
                action="clarify_intent",
                amount=str(updated_intent.budget) if updated_intent.budget else None,
                degraded=True,
                stop=True,
            ),
        )

    record.shopping_intent = updated_intent.model_dump(mode="json")
    await db.commit()

    outcome = await run_agent_turn(
        mission_id=record.mission_id,
        user_input=user_input,
        record=record,
        current_user=current_user,
        db=db,
        reasoning=reasoning,
    )
    refreshed = await _get_mission_or_none(db, record.mission_id)
    if refreshed is not None:
        record = refreshed

    return _agent_response(record, user_input, outcome)


async def _record_turn_bookends(
    db: AsyncSession, record: MissionRecord, user_input: str, agent_message: str
) -> None:
    """Both ends of a degraded turn on the record: what was asked, and what was said back."""
    await append_event(
        db,
        mission_id=record.mission_id,
        user_id=record.user_id,
        role="user",
        event_type="user_message",
        content=user_input,
    )
    await append_event(
        db,
        mission_id=record.mission_id,
        user_id=record.user_id,
        role="agent",
        event_type="agent_message",
        content=agent_message,
    )


def _agent_response(record: MissionRecord, user_input: str, outcome: AgentTurnOutcome) -> dict:
    """
    The endpoint's answer, in the shape the client already expects.

    `authority_decision` is present and **null** whenever this turn evaluated no authority — which
    is every non-consequential step, including product discovery. The field used to claim
    `{"approved": true}` for every shopping message without ever asking the Authority Engine; a
    conversation that only searched for products has no authority decision to report, and now it
    reports none.
    """
    intent_data = record.shopping_intent or {}
    return {
        "mission_id": record.mission_id,
        "user_input": user_input,
        "agent_proposal": {"action": outcome.action, "amount": outcome.amount},
        "authority_decision": outcome.authority_decision,
        "mission_status": record.status,
        "shopping_intent": record.shopping_intent,
        "discovered_products": outcome.discovered_products,
        "agent_message": outcome.agent_message,
        "agent_tools": outcome.tool_trace,
        "total_recovery_spend": str(record.total_recovery_spend or 0),
    }


@router.post("/{mission_id}/agent", response_model=dict)
async def agent_respond(
    mission_id: str,
    payload: AgentMessageRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    REAL-TIME AGENT ENDPOINT — one bounded turn of the RESOLVE agent (Task CORE-2).

    A shopping mission runs the loop: the model is shown the mission's canonical context and the
    capabilities it has been declared, it names one of them, the server validates that call and runs
    the guarded operation behind it, and the result is re-read from the database before the next step
    is chosen — up to `AGENT_MAX_STEPS`, ending in a sentence for the user. Every consequence still
    passes the ownership guard, the lifecycle preconditions, the currency and provider boundaries,
    and the Authority Engine, none of which this endpoint re-implements.

    A recovery mission keeps the legacy single-proposal flow it has always had: the planner proposes
    one action, `evaluate()` decides it, and the audit row records that decision.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)

    reasoning = get_reasoning_provider(settings.reasoning_provider, settings.reasoning_api_key)

    if record.mission_type == "shopping":
        return await _agent_shopping_turn(record, payload.user_input, current_user, db, reasoning)

    # Recovery mission path
    mission = _record_to_mission(record)

    # Ask the reasoning model what to do, given the user's message and the current mission state
    context = {
        "mission_objective": mission.mandate.objective.description,
        "remaining_budget": str(mission.mandate.authority.remaining_budget),
        "user_input": payload.user_input,
        "current_status": mission.status.value,
        "allowed_actions": [a.value for a in mission.mandate.authority.allowed_actions],
    }

    # Get the agent's proposed action (in real-time)
    proposed = await reasoning.propose_recovery_plan(context)

    # Check it against the Authority Engine
    decision = evaluate(mission.mandate.authority, proposed)

    # Record the decision in the audit log
    await audit.append(db, mission_id, "buyer_agent", decision)

    # If it was approved AND involves money, update the mission's recorded spend
    if decision.approved and proposed.amount:
        from app.authority.engine import apply_spend

        mission.mandate.authority = apply_spend(mission.mandate.authority, proposed.amount)
        record.total_recovery_spend = float(record.total_recovery_spend or 0) + float(
            proposed.amount
        )
        record.mandate = mission.mandate.model_dump(mode="json")
        record.status = MissionStatus.EXECUTING.value

    await db.commit()

    # Return the full live response: what the agent thought, what it proposed, and whether it was allowed
    return {
        "mission_id": mission_id,
        "user_input": payload.user_input,
        "agent_proposal": {
            "action": proposed.action.value,
            "amount": str(proposed.amount) if proposed.amount else None,
        },
        "authority_decision": {
            "approved": decision.approved,
            "decision": decision.decision.value,
            "reason": decision.reason,
        },
        "mission_status": record.status,
        "total_recovery_spend": str(record.total_recovery_spend or 0),
    }


class DelegateAuthorityRequest(BaseModel):
    """
    A user's explicit, narrow opt-in to let the agent act on their behalf (Task CORE-2, Phase 10).

    Deliberately absent: any action outside the three remedies, any amount this deployment has not
    already capped, and any identity. A mandate can only ever be **narrower** than the policy the
    server holds — that is what `Authority.refund_limit` has always meant, and the ceiling function
    enforces it in one place.
    """

    allowed_actions: list[str] = Field(..., min_length=1)
    recovery_budget: Decimal = Field(default=Decimal("0"), ge=0)
    refund_limit: Decimal | None = Field(default=None, ge=0)
    expires_at: datetime | None = None

    @field_validator("allowed_actions")
    @classmethod
    def _only_remedies(cls, v: list[str]) -> list[str]:
        allowed = {
            AllowedAction.REQUEST_REFUND.value,
            AllowedAction.REQUEST_REPLACEMENT.value,
            AllowedAction.REQUEST_RETURN.value,
        }
        unknown = [action for action in v if action not in allowed]
        if unknown:
            raise ValueError(
                f"Only {sorted(allowed)} may be delegated to the agent; {unknown} may not"
            )
        return v


def _deployment_bounds() -> dict:
    """
    The switches this deployment has, reported honestly and applied by the engine, not here.

    A delegation never overrides one of these: it can only ever be narrowed further by them, which
    is exactly why they are shown to the user rather than checked and hidden.
    """
    return {
        "refund_authority_enabled": settings.shopping_refund_authority_enabled,
        "refund_ceiling_inr": str(settings.shopping_refund_max_inr),
        "replacement_authority_enabled": settings.shopping_replacement_authority_enabled,
        "return_authority_enabled": settings.shopping_return_authority_enabled,
    }


def _mandate_authority(record: MissionRecord) -> tuple[RecoveryMandate, Authority]:
    mandate = RecoveryMandate(**record.mandate)
    return mandate, mandate.authority


@router.post("/{mission_id}/authority/delegate", response_model=dict)
async def delegate_mission_authority(
    mission_id: str,
    payload: DelegateAuthorityRequest,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Let the mission's owner hand the agent a named, bounded set of remedies.

    This is the surface the whole product was missing: discovery, assessment and resolution all
    worked, but a shopping mission's mandate was created with `allowed_actions = []` and nothing ever
    filled it in, so every remedy an agent proposed was refused as "never delegated". The gap is now
    closed by the one party entitled to close it — the user, explicitly, per action.

    What this is not: a permission. It records a delegation on the mandate, and the Authority Engine
    still decides every proposal against it, still applies this deployment's switches and ceiling,
    and still refuses anything the mandate does not name. Nothing here contacts a provider, moves
    money, or changes the mission's lifecycle state. Idempotent by content: re-delegating the same
    set produces the same mandate and one new audit row.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)

    mandate, authority = _mandate_authority(record)
    requested = [AllowedAction(value) for value in payload.allowed_actions]

    if payload.refund_limit is not None:
        ceiling = settings.shopping_refund_max_inr
        if payload.refund_limit > ceiling:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"A mandate may narrow this deployment's ₹{ceiling} refund ceiling, never raise "
                    "it. Nothing was changed."
                ),
            )

    # The UI delegates remedies without amounts (amounts are never client-supplied), but the
    # engine requires an explicit refund ceiling before any refund (Task 11B). When the owner
    # delegates request_refund and neither the request nor the mandate carries a ceiling, the
    # server derives one from the verified TEST payment itself — clamped by this deployment's
    # ceiling — so the remedy the buyer just allowed is actually exercisable.
    refund_limit = (
        payload.refund_limit if payload.refund_limit is not None else authority.refund_limit
    )
    if AllowedAction.REQUEST_REFUND in requested and refund_limit is None:
        payment_data = (record.shopping_intent or {}).get("payment") or {}
        if payment_data.get("status") == "verified" and payment_data.get("razorpay_payment_id"):
            order = await _load_order(db, mission_id)
            if order is not None:
                amount, _, _ = _canonical_refund_amount(payment_data, order)
                refund_limit = min(amount, settings.shopping_refund_max_inr)

    if payload.expires_at is not None and payload.expires_at <= datetime.utcnow():
        raise HTTPException(
            status_code=400, detail="An authority window has to end in the future."
        )

    granted = list(dict.fromkeys([*authority.allowed_actions, *requested]))
    updated = authority.model_copy(
        update={
            "allowed_actions": granted,
            "recovery_budget": max(authority.recovery_budget, payload.recovery_budget),
            "refund_limit": refund_limit,
            "expires_at": payload.expires_at or authority.expires_at,
        }
    )
    # Idempotent-replay detection, BEFORE anything is persisted: if the delegation this request
    # computes is indistinguishable from the mandate already on record, nothing changed and no
    # lifecycle event may be appended for it.
    delegation_unchanged = updated == authority
    record.mandate = mandate.model_copy(update={"authority": updated}).model_dump(mode="json")

    decision = AuthorityDecision(
        decision=Decision.APPROVED,
        reason=(
            "The mission's owner delegated "
            f"{', '.join(action.value for action in requested)} for this mission "
            f"(budget ₹{updated.recovery_budget}, refund limit {updated.refund_limit}, "
            f"expires {updated.expires_at}). This is a delegation of scope, not a permission: "
            "the Authority Engine still decides every proposal, and this deployment's switches "
            "still bound it."
        ),
        action=requested[0],
        requested_amount=None,
    )
    await audit.append(
        db,
        mission_id,
        "user",
        decision,
        note=(
            f"authority delegated: {[a.value for a in requested]}; deployment bounds "
            f"{_deployment_bounds()}; no provider contacted; no action executed"
        ),
    )
    delegation_row = None
    if not delegation_unchanged:
        delegation_row = await append_lifecycle_event(
            db,
            mission_id=mission_id,
            user_id=record.user_id,
            state="authority_delegated",
            detail=f"Authority delegated: {', '.join(a.value for a in requested)}.",
            extra_payload={"allowed_actions": [a.value for a in requested]},
        )
    await db.commit()

    if delegation_row is not None:
        await broadcast_event_row(delegation_row)
    return _authority_response(record, decision)


@router.post("/{mission_id}/authority/revoke", response_model=dict)
async def revoke_mission_authority(
    mission_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    Take the delegation back.

    Revocation removes every delegated action and leaves what has already been spent exactly where
    it is: undoing a mandate is not undoing history. Anything already submitted to a provider stays
    with that provider — RESOLVE reads it back rather than pretending it can be un-sent.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)

    mandate, authority = _mandate_authority(record)
    previously = [action.value for action in authority.allowed_actions]
    had_refund_limit = authority.refund_limit is not None
    updated = authority.model_copy(update={"allowed_actions": [], "refund_limit": None})
    record.mandate = mandate.model_copy(update={"authority": updated}).model_dump(mode="json")

    withdrawn = ", ".join(previously) or "none was granted"
    decision = AuthorityDecision(
        decision=Decision.APPROVED,
        reason=(
            f"The mission's owner revoked delegated authority ({withdrawn}). From now on only the "
            "actions that need no delegation can be proposed, and a remedy needs a fresh delegation."
        ),
        action=AllowedAction.NO_ACTION,
        requested_amount=None,
    )
    await audit.append(
        db,
        mission_id,
        "user",
        decision,
        note=(
            f"authority revoked; previously {previously}; spent_so_far unchanged at "
            f"{updated.spent_so_far}; any provider submission already made is unaffected and is "
            "read back, never re-sent"
        ),
    )
    # Revoking an empty mandate changes nothing; only a mandate that actually held delegated
    # actions (or a refund limit) produced a state change worth a lifecycle event.
    revoked_row = None
    if previously or had_refund_limit:
        revoked_row = await append_lifecycle_event(
            db,
            mission_id=mission_id,
            user_id=record.user_id,
            state="authority_revoked",
            detail=f"Authority revoked ({withdrawn}).",
            extra_payload={"previously_delegated": previously},
        )
    await db.commit()

    if revoked_row is not None:
        await broadcast_event_row(revoked_row)
    return _authority_response(record, decision, previously=previously)


def _authority_response(
    record: MissionRecord, decision: AuthorityDecision, *, previously: list[str] | None = None
) -> dict:
    authority = ((record.mandate or {}).get("authority")) or {}
    return {
        "mission_id": record.mission_id,
        "authority": {
            "allowed_actions": authority.get("allowed_actions", []),
            "recovery_budget": str(authority.get("recovery_budget", "0")),
            "spent_so_far": str(authority.get("spent_so_far", "0")),
            "refund_limit": authority.get("refund_limit"),
            "expires_at": authority.get("expires_at"),
        },
        "previously_delegated": previously or [],
        "deployment_bounds": _deployment_bounds(),
        "decision": {"approved": decision.approved, "reason": decision.reason},
        "note": (
            "Delegation widens what the agent may propose for this mission. It never widens what "
            "this deployment allows: a remedy still has to pass the Authority Engine at execution "
            "time, and a switch that is off here refuses whatever the mandate says."
        ),
    }


@router.get("/{mission_id}/agent/conversation", response_model=dict)
async def get_agent_conversation(
    mission_id: str,
    limit: int = 50,
    after_sequence: int | None = None,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    The mission's persisted conversation, oldest first — what a reload reads back (Phase 6).

    Scope is the same guard every other mission route uses: an authenticated caller sees their own
    mission, and a foreign mission id is indistinguishable from a missing one. There is no route to
    read another user's transcript because there is no route that takes a mission id without that
    check.

    M21 Phase 2A cursor mode: pass `after_sequence` to page forward through committed rows with
    sequence strictly greater than it (the same exclusive cursor the WebSocket replay uses). The
    response then carries `has_more` and `latest_sequence` so a client can catch up after a
    disconnect and know when it is fully caught up. Without the cursor the response is the
    historical shape: the most recent `limit` events.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)
    bounded_limit = max(1, min(limit, 200))
    latest = await latest_sequence(db, mission_id)
    if after_sequence is None:
        rows = await load_recent(db, mission_id, limit=bounded_limit)
        return {
            "mission_id": mission_id,
            "mission_status": record.status,
            "events": as_client_transcript(rows),
            "latest_sequence": latest,
            "note": (
                "This is the conversation RESOLVE kept: what was asked, what the agent did, and "
                "what the mission's state became. It is not a model's internal reasoning."
            ),
        }
    rows, has_more = await load_after(
        db, mission_id, after_sequence=after_sequence, limit=bounded_limit
    )
    return {
        "mission_id": mission_id,
        "mission_status": record.status,
        "events": as_client_transcript(rows),
        "latest_sequence": latest,
        "has_more": has_more,
        "note": (
            "This is the conversation RESOLVE kept: what was asked, what the agent did, and "
            "what the mission's state became. It is not a model's internal reasoning."
        ),
    }


@router.get("/{mission_id}/agent/tools", response_model=dict)
async def list_agent_tools(
    mission_id: str,
    current_user: UserRecord | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """
    The declared capability surface this agent has — names, descriptions and argument schemas.

    Read-only, owner-scoped, and useful for exactly one reason: a demo and a review should be able to
    see the whole boundary in one response instead of inferring it from what the agent happens to do.
    A tool that is not on this list does not exist.
    """
    record = await db.get(MissionRecord, mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _verify_mission_access(record, current_user)
    return {
        "mission_id": mission_id,
        "max_steps": settings.agent_max_steps,
        "tools": [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
            for definition in tool_definitions()
        ],
        "not_available": [
            "create_payment",
            "confirm_payment",
            "confirm_checkout",
            "any_http_call",
            "any_python_function",
            "any_sql",
            "any_shopify_mutation",
        ],
    }


def _record_to_mission(record: MissionRecord) -> Mission:
    shopping_intent = None
    if record.shopping_intent:
        shopping_intent = ShoppingIntent(**record.shopping_intent)

    return Mission(
        mission_id=record.mission_id,
        user_id=record.user_id,
        mission_type=record.mission_type or "recovery",
        mandate=RecoveryMandate(**record.mandate),
        transaction=TransactionRef(**record.transaction),
        status=MissionStatus(record.status),
        shopping_intent=shopping_intent,
        evidence=[EvidenceItem(**e) for e in (record.evidence or [])],
        created_at=record.created_at,
        updated_at=record.updated_at,
        total_recovery_spend=Decimal(str(record.total_recovery_spend or 0)),
        resolution_summary=record.resolution_summary,
    )


def _record_to_receipt(record: MissionRecord) -> dict:
    return _record_to_mission(record).to_intent_receipt()
