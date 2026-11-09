"""
The core domain objects: Objective, Authority, Recovery Mandate, and Mission.

These are the enforced types the rest of the system is built around, instead of loose JSON
floating between components. Every one of these is a Pydantic model, so a malformed proposal
from the reasoning layer is rejected at the schema boundary, before it ever reaches the
Authority Engine.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


from app.models.product import ProductResult


class AllowedAction(StrEnum):
    REQUEST_REFUND = "request_refund"
    REQUEST_REPLACEMENT = "request_replacement"
    NEGOTIATE = "negotiate"
    PAY_RECOVERY_COST = "pay_recovery_cost"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    # Post-purchase resolution proposals (Task 10). Proposal vocabulary only — no
    # executor exists; the Authority Engine gates every one of these.
    REQUEST_RETURN = "request_return"
    REQUEST_MORE_INFORMATION = "request_more_information"
    NO_ACTION = "no_action"


# The controlled proposal vocabulary for post-purchase resolution assessment
# (Task 10). Deliberately excludes negotiate/pay_recovery_cost: this stage may
# propose remedies for a human/authority gate to consider, never spending.
RESOLUTION_ACTIONS: frozenset[AllowedAction] = frozenset(
    {
        AllowedAction.REQUEST_REFUND,
        AllowedAction.REQUEST_REPLACEMENT,
        AllowedAction.REQUEST_RETURN,
        AllowedAction.ESCALATE_TO_HUMAN,
        AllowedAction.REQUEST_MORE_INFORMATION,
        AllowedAction.NO_ACTION,
    }
)


class MissionType(StrEnum):
    RECOVERY = "recovery"
    SHOPPING = "shopping"


class MissionStatus(StrEnum):
    # Recovery states
    MONITORING = "monitoring"                # watching the real transaction, nothing wrong yet
    DEVIATION_DETECTED = "deviation_detected"
    INVESTIGATING = "investigating"
    NEGOTIATING = "negotiating"
    AWAITING_HUMAN_APPROVAL = "awaiting_human_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ESCALATED = "escalated"                   # authority exceeded, handed back to the user

    # Shopping states
    SHOPPING_INTENT = "shopping_intent"       # Initial natural language intent captured
    NEEDS_CLARIFICATION = "needs_clarification" # Missing key attributes (budget, variant, etc.)
    READY_FOR_DISCOVERY = "ready_for_discovery" # Constraints clear, ready for product search
    DISCOVERY_COMPLETED = "discovery_completed" # Real products discovered and presented to user
    PRODUCT_SELECTED = "product_selected"     # User picked one discovered product; no checkout implied
    CHECKOUT_REVIEW = "checkout_review"       # User is reviewing the server-derived checkout summary
    CHECKOUT_CONFIRMED = "checkout_confirmed" # User explicitly confirmed checkout details; ready for the future payment step (NO payment made)
    PAYMENT_PENDING = "payment_pending"       # Razorpay TEST order created; awaiting payment outcome (TEST MODE only)
    PAYMENT_VERIFIED = "payment_verified"     # Razorpay checkout signature VERIFIED server-side; test payment settled
    PAYMENT_FAILED = "payment_failed"         # Signature verification failed or the test payment failed; never implies settlement

    # RESOLVE order lifecycle (Task 08) — post-payment record/context, NOT merchant fulfillment
    ORDER_CREATED = "order_created"           # Persistent RESOLVE order record created from the verified payment
    ORDER_CONFIRMED = "order_confirmed"       # User explicitly confirmed the RESOLVE order record (no merchant order implied)

    # Post-purchase context (Task 09) — issue intake on the confirmed order record
    AWAITING_POST_PURCHASE_CONTEXT = "awaiting_post_purchase_context"  # A post-purchase issue has been recorded; resolution options under deterministic review


class IssueType(StrEnum):
    """Controlled set of post-purchase issue categories (Task 09)."""

    DAMAGED = "damaged"
    DEFECTIVE = "defective"
    WRONG_ITEM = "wrong_item"
    MISSING_ITEM = "missing_item"
    NOT_RECEIVED = "not_received"
    QUALITY_ISSUE = "quality_issue"
    OTHER = "other"


class RequestedResolution(StrEnum):
    """
    The user's RESOLUTION PREFERENCE for a reported issue — never an authorization.

    Stored meaning is "the user said they would prefer X". It must not automatically
    trigger X, and no field named like replacement_authorized exists anywhere.
    """

    NONE = "none"
    REFUND = "refund"
    REPLACEMENT = "replacement"


class IssueStatus(StrEnum):
    """
    Minimal deterministic issue lifecycle (Task 09).

    reported -> under_review -> resolution_pending is the forward path; a replacement that a
    real merchant provider has actually accepted additionally reaches
    replacement_requested. Backend code advances these — the LLM never mutates them, and no
    state here claims shipment, delivery, or merchant acceptance that no provider reported.
    """

    REPORTED = "reported"
    UNDER_REVIEW = "under_review"
    RESOLUTION_PENDING = "resolution_pending"
    REPLACEMENT_REQUESTED = "replacement_requested"
    RETURN_REQUESTED = "return_requested"


class RefundStatus(StrEnum):
    """
    Refund execution lifecycle (Task 11A) — the state of one Razorpay TEST-MODE refund.

    pending  : claim persisted and/or submitted to Razorpay, NOT yet verified with the
               provider — never presented as a completed refund
    verified : the provider was asked again and reported the refund as processed
    failed   : the provider reported failure (or the submit call itself failed); a retry
               goes through the same record, never a second uncontrolled refund

    Reaching `verified` requires a provider status check — the initiating API response
    alone is never treated as proof.
    """

    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class ReplacementStatus(StrEnum):
    """
    Replacement execution lifecycle (Task 12A) — the state of one merchant replacement request.

    pending   : RESOLVE persisted the request; no merchant provider has accepted it. When no
                replacement provider is connected this is where the record correctly stays —
                pending is never presented as a replacement having happened
    requested : a real provider accepted/created a replacement order (provider reference held);
                this is NOT shipment or delivery
    confirmed : the provider itself later confirmed the replacement
    failed    : the provider reported failure; a retry goes through the same record

    Only a provider response may move a record past `pending` — never an endpoint call.
    """

    PENDING = "pending"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class ReturnStatus(StrEnum):
    """
    Return execution lifecycle (Task 12B) — the state of one merchant return request.

    pending         : RESOLVE persisted the request; nothing has been submitted to any merchant.
                      With no return provider connected this is where the record correctly stays.
    requested       : a real provider accepted the return and gave RESOLVE a reference — this is an
                      acceptance, NOT an approved refund, shipped label, or completed return
    confirmed       : the provider itself later confirmed the return
    failed          : the provider refused, deterministically, before creating a return; a retry
                      reuses the same record
    outcome_unknown : the submission went out and the answer was lost or unintelligible, so
                      RESOLVE does not know whether the merchant holds a return for it. This is
                      deliberately NOT 'failed': 'failed' asserts nothing happened, and that is
                      exactly the assertion RESOLVE cannot make here. Only a read-only
                      reconciliation may move a record out of this state — never a re-submission.

    Only a provider response may move a record past `pending` — never an endpoint call.
    """

    PENDING = "pending"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class PurchaseAuthorizationStatus(StrEnum):
    """
    Merchant-purchase authorization lifecycle (buyer-flow Phase 3) — the state of the ONE
    authorization record per RESOLVE order.

    authorized              : the claim is persisted and the Authority Engine approved this order
                              for purchase, but no submission outcome is on record yet. The
                              submission may not have run at all (crash, connection refused
                              before send, a pre-execution trigger refusal). This is the ONLY
                              state a fresh user POST may resubmit from — a state-checked
                              resubmission, never an automatic retry.
    purchase_failed         : the trigger definitively refused BEFORE creating anything. Nothing
                              on the merchant side exists because of this path.
    purchase_unknown        : the submission may or may not have reached the merchant (lost
                              answer, unreadable response). Deliberately NOT 'failed': RESOLVE
                              cannot assert nothing happened, and exactly that assertion is what
                              a blind retry would bet a duplicate purchase on. Never retried
                              automatically; never re-claimed by another POST.
    purchase_reported_linked: the trigger REPORTED a confirmed linkage. This is an untrusted
                              report — the authoritative merchant linkage is the order row's
                              provider_order_reference, and only the linkage receiver may write
                              it. The report alone never makes an order linked.
    linkage_refused         : the trigger reported refused_after_create: a merchant order MAY
                              exist, but the linkage was refused. Never marked 'failed' (that
                              would assert no order was created), never retried.
    """

    AUTHORIZED = "authorized"
    PURCHASE_FAILED = "purchase_failed"
    PURCHASE_UNKNOWN = "purchase_unknown"
    PURCHASE_REPORTED_LINKED = "purchase_reported_linked"
    LINKAGE_REFUSED = "linkage_refused"

    @classmethod
    def terminal_states(cls) -> frozenset[str]:
        """States a POST may never resubmit from — everything except the bare claim."""
        return frozenset(
            {
                cls.PURCHASE_FAILED.value,
                cls.PURCHASE_UNKNOWN.value,
                cls.PURCHASE_REPORTED_LINKED.value,
                cls.LINKAGE_REFUSED.value,
            }
        )


class ResolutionState(StrEnum):
    """
    Orchestrator-level outcome of one autonomous resolution attempt (Task 13).

    This is a SUMMARY of what the controlled executors did — it never replaces their own
    state machines, and `allowed` is not a state here precisely because being allowed is not
    being done. Denied attempts are recorded so an audit can show authority was re-evaluated.

    denied                    : current Authority Engine refused — no provider was contacted
    pending                   : the executor recorded intent; no provider accepted it (or none
                                is connected)
    requested                 : a replacement/return provider accepted it — not shipped, not
                                completed
    verified                  : the refund provider itself reported the refund processed
    confirmed                 : the replacement/return provider itself confirmed it
    failed                    : the provider refused or the submission failed
    escalated                 : routed to a human; no ticket number is invented
    more_information_required : the resolution needs something from the user
    no_action                 : deliberately nothing to do
    """

    DENIED = "denied"
    PENDING = "pending"
    REQUESTED = "requested"
    VERIFIED = "verified"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    ESCALATED = "escalated"
    MORE_INFORMATION_REQUIRED = "more_information_required"
    NO_ACTION = "no_action"


class CheckoutConfirmation(BaseModel):
    """
    Record of the user's explicit confirmation of the checkout review (Task 06).

    This is a PAYMENT BOUNDARY marker, not a payment: no provider is invoked, no order is
    created. Every monetary value here is derived server-side from the mission's persisted
    selected_product at confirm time; the client never supplies authoritative data.
    """

    quantity: int = Field(default=1, description="Fixed at 1 for Task 06; quantity editing is a future task")
    subtotal: Decimal | None = Field(
        default=None, description="Server-computed from selected_product.extracted_price; never client-supplied"
    )
    currency: str = Field(default="INR")
    payment_provider: str = Field(
        default="razorpay_test_mode",
        description="Which rail the FUTURE payment task will use. Marker only — nothing is charged here.",
    )
    confirmed_at: datetime = Field(default_factory=datetime.utcnow)


class ShoppingPayment(BaseModel):
    """
    Razorpay TEST-MODE payment attempt for a shopping mission (Task 07).

    A mission reaches PAYMENT_VERIFIED only after the standard-checkout signature
    (HMAC of order_id|payment_id, verified server-side with the backend-only secret)
    validates against THIS mission's persisted order. Order creation alone, a frontend
    callback, or any client-supplied "success" flag never produces payment_verified.
    All monetary values are derived server-side from the checkout confirmation.
    """

    razorpay_order_id: str = Field(description="Razorpay TEST order id; created by the backend, never the client")
    amount: Decimal = Field(description="Server-derived amount (INR) the user confirmed at checkout")
    amount_paise: int = Field(description="Smallest currency unit sent to Razorpay (amount * 100, integer math)")
    currency: str = Field(default="INR")
    status: str = Field(default="pending", description="pending | verified | failed")
    razorpay_payment_id: str | None = Field(default=None)
    failure_reason: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    verified_at: datetime | None = Field(default=None)


class ResultSetRecord(BaseModel):
    """
    One persisted snapshot of a discovery result set (CORE-5 Phase 4).

    A new search never destroys the previous set: it appends a new entry here, and the
    previous one stays exactly as it was produced. A locally filtered set is also an entry,
    marked `origin="filtered"` and linked to the set it was derived from — so the system can
    truthfully say "these are the same discovered products filtered to your new constraint"
    instead of pretending a provider call happened when it did not.

    `constraint_snapshot` holds the mission's structured constraint fields at the moment the
    set was produced; it is the only thing the reuse decision is ever compared against.
    """

    set_id: str = Field(default_factory=lambda: f"rs_{uuid.uuid4().hex[:12]}")
    origin: str = Field(
        default="search",
        description="search (a real provider call) | filtered (derived locally from source_set_id) | imported (a pre-Phase-4 set synthesized from mission state)",
    )
    query: str | None = Field(default=None, description="The query the provider was actually asked, for origin=search; None for filtered sets")
    source_set_id: str | None = Field(default=None, description="The set this one was filtered from; set exactly when origin=filtered")
    constraint_snapshot: dict[str, Any] = Field(default_factory=dict, description="The structured constraint fields this set was produced under")
    products: list[ProductResult] = Field(default_factory=list, description="The full, unmodified ProductResults of this set")
    note: str | None = Field(default=None, description="How this set came to exist, in plain language")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ShoppingIntent(BaseModel):
    """
    Structured representation of a user's natural language shopping request.
    Parsed from text and refined through clarification before discovery.
    """
    raw_prompt: str = Field(..., description="Original user prompt")
    category: str | None = Field(default=None, description="Product category, e.g. shampoo, running shoes")
    budget: Decimal | None = Field(default=None, description="Max budget constraint in INR")
    currency: str = Field(default="INR", description="Currency code")
    preferences: list[str] = Field(default_factory=list, description="Extracted preferences/features that have no dedicated field, e.g. anti-hairfall, organic")
    brand: str | None = Field(default=None, description="Preferred brand if specified")
    size_or_variant: str | None = Field(default=None, description="Size or variant details, e.g. 250ml, size 9")
    color: str | None = Field(default=None, description="Requested color, when the user named one; None is unknown, never a guess from a title")
    material: str | None = Field(default=None, description="Requested material, e.g. cotton; None is unknown")
    gender: str | None = Field(default=None, description="Requested gender line, e.g. women; None is unknown")
    intended_use: str | None = Field(default=None, description="What the product is for, e.g. office, running; None is unknown")
    quantity: int | None = Field(default=None, ge=1, description="How many distinct options the user asked to see; never a pack size invented from a title")
    exclusions: list[str] = Field(default_factory=list, description="Things the user explicitly does not want, e.g. a brand or a material")
    missing_info: list[str] = Field(default_factory=list, description="Identified missing constraints")
    clarification_prompt: str | None = Field(default=None, description="Question asked to clarify missing info")
    state: str = Field(default="shopping_intent", description="Current intent state: shopping_intent, needs_clarification, ready_for_discovery, discovery_completed, product_selected, checkout_review, checkout_confirmed, payment_pending, payment_verified, payment_failed, order_created, order_confirmed, awaiting_post_purchase_context")
    discovered_products: list[ProductResult] = Field(default_factory=list, description="Normalized products discovered from provider")
    # The exact question the provider was asked, persisted (Task 14C.7E) so a variant clarification
    # re-reads the same slice of the catalog instead of inventing a new search. It is a record of
    # what RESOLVE sent, never a result: the products always come back from the provider.
    discovery_query: str | None = Field(default=None, description="The search query the last discovery actually sent to the provider")
    # CORE-5 Phase 4: the history of result sets this mission has seen, newest last, with the
    # active one named. discovered_products above remains the live mirror of the active set —
    # selection, checkout and the frontend read it unchanged — while the history preserves what
    # each earlier search or local filtering produced and under which constraints.
    result_sets: list[ResultSetRecord] = Field(default_factory=list, description="Persisted result-set history; a new search or a local filter appends, nothing is ever destroyed")
    active_result_set_id: str | None = Field(default=None, description="The result set discovered_products currently mirrors")
    selected_product: ProductResult | None = Field(default=None, description="The discovered product the user selected; always a member of discovered_products, resolved server-side")
    checkout: CheckoutConfirmation | None = Field(default=None, description="Set only after the user explicitly confirms the server-derived checkout review; never implies payment occurred")
    payment: ShoppingPayment | None = Field(default=None, description="Razorpay TEST-MODE payment record; status verified only via server-side signature validation")


class Objective(BaseModel):
    """What the user actually wanted — not just what was bought."""

    description: str = Field(..., description="e.g. 'Acquire a genuine, working Sony camera'")
    success_criteria: list[str] = Field(
        default_factory=list,
        description="Concrete, checkable conditions that define the objective as fulfilled.",
    )
    deadline: datetime | None = None
    max_total_cost: Decimal | None = None


class Authority(BaseModel):
    """
    Bounded delegation of trust. This is the object the Authority Engine checks every proposed
    action against. Nothing in the system can act outside these limits without human approval.
    """

    allowed_actions: list[AllowedAction] = Field(default_factory=list)
    recovery_budget: Decimal = Field(
        ..., ge=0, description="Max additional spend RESOLVE may authorize without asking the user."
    )
    spent_so_far: Decimal = Field(default=Decimal("0"), ge=0)
    # Separate from recovery_budget on purpose: that field is money the agent may SPEND
    # externally, while a refund reverses an existing captured payment. None means the mandate
    # sets no per-mandate ceiling of its own (the deployment ceiling then applies); a value here
    # can only narrow that ceiling, never raise it.
    refund_limit: Decimal | None = Field(
        default=None, ge=0, description="Max refund value this mandate delegates, if any."
    )
    expires_at: datetime | None = None

    @property
    def remaining_budget(self) -> Decimal:
        return self.recovery_budget - self.spent_so_far

    def has_action(self, action: AllowedAction) -> bool:
        return action in self.allowed_actions


class EvidenceItem(BaseModel):
    kind: str  # e.g. "delivery_photo", "invoice", "carrier_exception_event", "merchant_email"
    url: str | None = None
    description: str | None = None
    collected_at: datetime = Field(default_factory=datetime.utcnow)


class OrderIssue(BaseModel):
    """
    A post-purchase issue reported against a confirmed RESOLVE order (Task 09).

    The client supplies only issue_type, description, requested_resolution, and
    optional evidence metadata. order_id / mission_id / user_id are resolved by the
    backend from the authenticated mission and its canonical order row; payment ids
    and amounts are never part of issue intake.
    """

    issue_id: str = Field(default_factory=lambda: f"issue_{uuid.uuid4().hex[:12]}")
    order_id: str
    mission_id: str
    user_id: str
    issue_type: IssueType
    description: str = Field(description="User's own description of the issue")
    requested_resolution: RequestedResolution = Field(default=RequestedResolution.NONE)
    status: IssueStatus = Field(default=IssueStatus.REPORTED)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ResolutionProposal(BaseModel):
    """
    A VALIDATED post-purchase resolution proposal (Task 10).

    This is the only shape the rest of the system accepts from the reasoning model:
    the raw provider response is parsed into this strict schema (or replaced by a
    deterministic safe-fallback) BEFORE the Authority Engine sees it. It is data
    only — nothing here executes, and the amount for a refund proposal is always
    the canonical persisted order amount, never the model's number.
    """

    action: AllowedAction
    issue_assessment: str
    reasoning_summary: str
    requested_amount: Decimal | None = None
    currency: str | None = None
    confidence: float | None = None
    needs_escalation: bool = False
    source: str = Field(
        default="reasoning_model",
        description="'reasoning_model' | 'safe_fallback' — how this validated proposal "
        "came to exist; safe_fallback means the model output was rejected and the "
        "deterministic escalation was used instead.",
    )


class RecoveryMandate(BaseModel):
    """
    Signed at the moment of purchase. Unlike a payment-authorization mandate (which expires at
    settlement), the Recovery Mandate stays alive until the objective is verified or explicitly
    abandoned. It is what lets a Buyer Agent, a Merchant Agent, and a payment provider all agree
    on what "fixed" means, without a human re-explaining it every time.
    """

    mandate_id: str = Field(default_factory=lambda: f"mandate_{uuid.uuid4().hex[:12]}")
    mission_id: str
    objective: Objective
    authority: Authority
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    signature: str | None = Field(
        default=None,
        description="Placeholder for a cryptographic signature over the mandate contents, "
        "proving it is the user's authorization and not a merchant- or platform-issued claim.",
    )


class TransactionRef(BaseModel):
    merchant_name: str
    order_id: str
    payment_id: str | None = None
    shipment_tracking_id: str | None = None
    amount_paid: Decimal
    currency: str = "INR"


class Mission(BaseModel):
    """
    The primary object in the system — replaces a chat transcript as the source of truth for a
    case. Everything that happens to a purchase after payment is a change to this object.
    """

    mission_id: str = Field(default_factory=lambda: f"mission_{uuid.uuid4().hex[:12]}")
    user_id: str
    mission_type: str = Field(default="recovery", description="'recovery' or 'shopping'")
    mandate: RecoveryMandate
    transaction: TransactionRef
    status: MissionStatus = MissionStatus.MONITORING
    shopping_intent: ShoppingIntent | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    resolution_summary: str | None = None
    total_recovery_spend: Decimal = Field(default=Decimal("0"))

    @field_validator("status")
    @classmethod
    def _status_is_valid(cls, v: MissionStatus) -> MissionStatus:
        return v

    def to_intent_receipt(self) -> dict:
        """
        The machine-readable case file: what the user wanted, what they authorized, what
        actually happened, and what was done about it. Every field here must trace back to a
        real audit_log row — this method only ever reads from the Mission's own state, never
        invents a value.
        """
        return {
            "mission_id": self.mission_id,
            "mission_type": self.mission_type,
            "objective": self.mandate.objective.description,
            "deadline": self.mandate.objective.deadline.isoformat()
            if self.mandate.objective.deadline
            else None,
            "recovery_budget": str(self.mandate.authority.recovery_budget),
            "allowed_actions": [a.value for a in self.mandate.authority.allowed_actions],
            "status": self.status.value,
            "total_recovery_spend": str(self.total_recovery_spend),
            "resolution_summary": self.resolution_summary,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "shopping_intent": self.shopping_intent.model_dump(mode="json")
            if self.shopping_intent
            else None,
        }
