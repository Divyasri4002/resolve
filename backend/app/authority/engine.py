"""
THE AUTHORITY ENGINE.

This is the single most important file in the system.

The reasoning model (see app/agents/reasoning_provider.py) is allowed to PROPOSE an action:
a refund, a replacement, an additional payment, a negotiation offer. It is never allowed to
EXECUTE one. Every proposed action — on the Buyer Agent's side or the Merchant Agent's side —
must pass through this module first.

Design constraints this file must satisfy, and does:
  1. Zero network calls. Zero model calls. Pure, synchronous, deterministic Python.
  2. Fully unit-testable in isolation (see backend/tests/test_authority_engine.py) — you should
     never need a database, an API key, or a running server to prove this logic is correct.
  3. Fails closed. If anything about a proposal is ambiguous, unparseable, or missing, the
     default decision is BLOCKED, never APPROVED.
  4. Every call returns a fully-reasoned AuthorityDecision — never just True/False — because the
     "reason" field is what gets written to the insert-only audit log.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.models.mission import AllowedAction, Authority, PurchaseAuthorizationStatus


class Decision(StrEnum):
    APPROVED = "approved"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ProposedAction:
    """What the Recovery Planner (LLM) wants to do. Nothing more than data — this dataclass
    has no methods that touch money or state."""

    action: AllowedAction
    amount: Decimal | None = None  # required for PAY_RECOVERY_COST, ignored otherwise


@dataclass(frozen=True)
class AuthorityDecision:
    decision: Decision
    reason: str
    action: AllowedAction
    requested_amount: Decimal | None

    @property
    def approved(self) -> bool:
        return self.decision is Decision.APPROVED


# Actions that spend no money, contact no merchant, and change no payment or order state —
# they ask, do nothing, or hand off to a human. Requiring delegation for these would
# prevent an agent from escalating even when its mandate has run out, so they are always
# allowed. Anything that proposes a remedy (refund / replacement / return / negotiate /
# spend) still needs delegated authority. (Task 10 extension.)
_INTRINSICALLY_SAFE_ACTIONS = frozenset(
    {
        AllowedAction.ESCALATE_TO_HUMAN,
        AllowedAction.REQUEST_MORE_INFORMATION,
        AllowedAction.NO_ACTION,
    }
)


def refund_ceiling(authority: Authority) -> Decimal | None:
    """
    The deterministic maximum refund value this mission may reverse, or None when the deployment
    grants no refund authority at all (Task 11B).

    Two narrowing layers, both server-side:
      * settings.shopping_refund_authority_enabled — off by default, so refunds are denied
        everywhere until an operator turns bounded refund authority on.
      * min(deployment ceiling, this mandate's own refund_limit) — a mandate may ask for less
        than the deployment allows, never more.

    Nothing here is read from a model response or a request body: the ceiling is policy, and the
    amount it is compared against is the canonical persisted payment/order amount.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.shopping_refund_authority_enabled:
        return None
    ceiling = settings.shopping_refund_max_inr
    if authority.refund_limit is not None:
        ceiling = min(ceiling, authority.refund_limit)
    return ceiling


def evaluate(authority: Authority, proposal: ProposedAction) -> AuthorityDecision:
    """
    The one function every state-changing step in the system must call before it does anything.

    Checks performed, in order (first failure wins — fail closed):
      0. Is this an intrinsically safe action (no spend, no merchant contact, no payment or
         order change)? These never require delegated authority.
      1. Is the mandate still within its validity window?
      2. Is this action type in the allowed_actions list at all?
      3. If it refunds, is bounded refund authority delegated, and is the canonical amount
         within the delegated ceiling?
      4. If it replaces, is bounded replacement authority enabled for this deployment?
      5. If it returns, is bounded return authority enabled for this deployment?
      6. If it moves money, is the amount present, positive, and within the remaining budget?
    """
    from datetime import datetime

    # 0. Intrinsically safe: asking a human, asking the user for info, or doing nothing
    # cannot exceed any authority.
    if proposal.action in _INTRINSICALLY_SAFE_ACTIONS:
        return AuthorityDecision(
            decision=Decision.APPROVED,
            reason=(
                f"Action '{proposal.action.value}' spends nothing, contacts no merchant, and "
                "changes no payment or order state — no delegated authority is required."
            ),
            action=proposal.action,
            requested_amount=proposal.amount,
        )

    # 1. Expiry
    if authority.expires_at is not None and datetime.utcnow() > authority.expires_at:
        return AuthorityDecision(
            decision=Decision.BLOCKED,
            reason="Mandate has expired — no action may be taken without renewed authority.",
            action=proposal.action,
            requested_amount=proposal.amount,
        )

    # 2. Action type allow-list
    if not authority.has_action(proposal.action):
        return AuthorityDecision(
            decision=Decision.BLOCKED,
            reason=f"Action '{proposal.action.value}' was never delegated to this agent.",
            action=proposal.action,
            requested_amount=proposal.amount,
        )

    # 3. Refund ceiling — a refund reverses a captured payment, so it is bounded by its own
    # delegated ceiling (Task 11B) rather than by the recovery spend budget.
    if proposal.action is AllowedAction.REQUEST_REFUND:
        ceiling = refund_ceiling(authority)
        if ceiling is None:
            return AuthorityDecision(
                decision=Decision.BLOCKED,
                reason=(
                    "Bounded refund authority is not configured for this deployment — a refund "
                    "needs explicit delegated refund authority."
                ),
                action=proposal.action,
                requested_amount=proposal.amount,
            )
        if proposal.amount is None or proposal.amount <= 0:
            return AuthorityDecision(
                decision=Decision.BLOCKED,
                reason=(
                    "A refund proposal must carry a positive canonical amount derived from the "
                    "persisted payment — a missing or non-positive amount is never refundable."
                ),
                action=proposal.action,
                requested_amount=proposal.amount,
            )
        if proposal.amount > ceiling:
            return AuthorityDecision(
                decision=Decision.BLOCKED,
                reason=(
                    f"Refund of ₹{proposal.amount} exceeds the delegated refund ceiling of "
                    f"₹{ceiling}."
                ),
                action=proposal.action,
                requested_amount=proposal.amount,
            )

    # 4. Replacement channel — asking a merchant to send another unit moves no money, so there
    # is no amount ceiling here; what must exist is a deployment that has replacement authority
    # switched on at all (Task 12A) and a mandate that delegated the action (checked above).
    if proposal.action is AllowedAction.REQUEST_REPLACEMENT:
        from app.config import get_settings

        if not get_settings().shopping_replacement_authority_enabled:
            return AuthorityDecision(
                decision=Decision.BLOCKED,
                reason=(
                    "Bounded replacement authority is not configured for this deployment — a "
                    "replacement needs explicit delegated replacement authority."
                ),
                action=proposal.action,
                requested_amount=proposal.amount,
            )

    # 5. Return channel — returning goods moves no money either, so its only bound is its own
    # deployment switch (Task 12B) plus the mandate delegation checked above. Refund and
    # replacement authority never imply it, and it never implies them.
    if proposal.action is AllowedAction.REQUEST_RETURN:
        from app.config import get_settings

        if not get_settings().shopping_return_authority_enabled:
            return AuthorityDecision(
                decision=Decision.BLOCKED,
                reason=(
                    "Bounded return authority is not configured for this deployment — a "
                    "return needs explicit delegated return authority."
                ),
                action=proposal.action,
                requested_amount=proposal.amount,
            )

    # 6. Spend checks — only relevant for the one action type that moves money
    if proposal.action is AllowedAction.PAY_RECOVERY_COST:
        if proposal.amount is None or proposal.amount <= 0:
            return AuthorityDecision(
                decision=Decision.BLOCKED,
                reason="A spend proposal must include a positive amount.",
                action=proposal.action,
                requested_amount=proposal.amount,
            )

        if proposal.amount > authority.remaining_budget:
            return AuthorityDecision(
                decision=Decision.BLOCKED,
                reason=(
                    f"Requested ₹{proposal.amount} exceeds remaining recovery authority of "
                    f"₹{authority.remaining_budget} (budget ₹{authority.recovery_budget}, "
                    f"already spent ₹{authority.spent_so_far})."
                ),
                action=proposal.action,
                requested_amount=proposal.amount,
            )

    # Every check passed.
    return AuthorityDecision(
        decision=Decision.APPROVED,
        reason="Within delegated authority.",
        action=proposal.action,
        requested_amount=proposal.amount,
    )


def apply_spend(authority: Authority, amount: Decimal) -> Authority:
    """
    Returns a NEW Authority with spent_so_far incremented. Called only after:
      (a) evaluate() returned APPROVED, and
      (b) the payment was actually confirmed via a Razorpay webhook — never on the strength of
          the initiating API call alone.
    Authority is treated as immutable; this never mutates the object passed in.
    """
    return authority.model_copy(update={"spent_so_far": authority.spent_so_far + amount})


class PurchaseAction(StrEnum):
    """
    The one action of the merchant-purchase gate, deliberately OUTSIDE AllowedAction.

    AllowedAction is the proposal vocabulary the reasoning model may put in a ResolutionProposal,
    and the LLM must never authorize a purchase — so place_merchant_order is not a member of it
    and never can be proposed by the agent. evaluate_purchase is a separate pure path that only
    the user's own purchase endpoint (and the read-only eligibility tool) may consult.
    """

    PLACE_MERCHANT_ORDER = "place_merchant_order"


# The only currency a merchant purchase may be authorized in. RESOLVE's payments, orders and
# checkout are INR end to end; a purchase against any other currency would pair a verified
# payment with an order priced on a different rail.
PURCHASE_CURRENCY = "INR"


@dataclass(frozen=True)
class PurchaseFacts:
    """
    Everything the purchase gate needs, and all of it server-derived.

    No field here may be read from a request body or a model response: the facts are computed by
    the caller from the persisted mission, order, payment evidence and authorization record. The
    dataclass exists so the gate stays pure — the same property the Authority Engine itself has —
    and every refusal is a checkable statement about persisted state.
    """

    # Deployment policy
    purchase_authority_enabled: bool
    test_mode: bool
    # Order facts (persisted ResolveOrderRecord)
    order_belongs_to_mission: bool
    order_state_eligible: bool  # the user has explicitly confirmed the order record
    merchant_identity_complete: bool  # product GID + variant GID + store domain all persisted
    already_linked: bool  # the order already carries a merchant order reference
    canonical_amount: Decimal | None  # the order's persisted amount
    currency: str | None  # the order's persisted currency
    # Buyer payment evidence (persisted ShoppingPayment on the mission)
    payment_evidence_verified: bool  # status verified AND Razorpay references match the order
    payment_amount_matches_order: bool  # the verified payment's amount equals the order amount
    # Prior authorization record, if one exists (None = no authorization yet)
    prior_authorization_state: str | None = None


def evaluate_purchase(facts: PurchaseFacts) -> AuthorityDecision:
    """
    The deterministic merchant-purchase gate (buyer-flow Phase 3).

    A separate path from evaluate() because place_merchant_order is not an agent-proposable
    action: the user's POST endpoint is the only authorization boundary for a purchase. The same
    design constraints apply — zero network calls, zero model calls, pure synchronous
    deterministic Python, fails closed, and a fully-reasoned decision for the audit log.

    Checks performed in order (first failure wins — fail closed):
      1. Is bounded purchase authority switched on for this deployment?
      2. Is RESOLVE running Razorpay TEST mode?
      3. Does the order belong to this mission?
      4. Is the order in an eligible state (the user explicitly confirmed the order record)?
      5. Is the buyer payment evidence verified, with references matching this order?
      6. Does the verified payment amount equal the persisted order amount exactly?
      7. Is the persisted Shopify product/variant/store identity complete?
      8. Is the order not already linked to a merchant order?
      9. Does any prior authorization record forbid a duplicate purchase?
     10. Is the canonical amount present, positive, and in the one supported currency?
    """
    def blocked(reason: str) -> AuthorityDecision:
        return AuthorityDecision(
            decision=Decision.BLOCKED,
            reason=reason,
            action=PurchaseAction.PLACE_MERCHANT_ORDER,
            requested_amount=facts.canonical_amount,
        )

    # 1. Deployment switch — off by default everywhere; a mandate or a request can never
    #    enable a purchase, only an operator can.
    if not facts.purchase_authority_enabled:
        return blocked(
            "Bounded merchant-purchase authority is not configured for this deployment — "
            "no purchase may be authorized until an operator enables it."
        )

    # 2. TEST mode only. A purchase submitted against live Razorpay credentials is refused
    #    before any merchant contact.
    if not facts.test_mode:
        return blocked(
            "Merchant purchase is restricted to Razorpay TEST mode — this deployment is not "
            "running test credentials, so no purchase may be authorized."
        )

    # 3. Order/mission binding.
    if not facts.order_belongs_to_mission:
        return blocked(
            "The order does not belong to this mission — a purchase may only be authorized "
            "for an order resolved server-side from the authenticated mission."
        )

    # 4. Eligible state — the user must have explicitly confirmed the order record.
    if not facts.order_state_eligible:
        return blocked(
            "The order is not in a purchase-eligible state — the user must have explicitly "
            "confirmed the RESOLVE order record first."
        )

    # 5. Buyer payment evidence: a verified TEST payment whose Razorpay references are the
    #    ones persisted on this order. Nothing else counts as paid.
    if not facts.payment_evidence_verified:
        return blocked(
            "The buyer payment evidence is not verified against this order — the persisted "
            "payment must be verified and carry this order's Razorpay references."
        )

    # 6. Amount agreement: the verified payment and the canonical order must state the same
    #    amount exactly. Any disagreement is a reason to refuse, never to reconcile here.
    if not facts.payment_amount_matches_order:
        return blocked(
            "The verified payment amount does not match the persisted order amount exactly — "
            "a purchase is never authorized on mismatched amounts."
        )

    # 7. Merchant catalog identity: the channel buys a variant Shopify itself identified. An
    #    order without the complete persisted identity has nothing real to buy.
    if not facts.merchant_identity_complete:
        return blocked(
            "The order has no complete persisted Shopify product/variant/store identity — "
            "a merchant purchase cannot be authorized without it."
        )

    # 8. No duplication through the linkage itself.
    if facts.already_linked:
        return blocked(
            "This order is already linked to a merchant order — authorizing another purchase "
            "for it would create a duplicate."
        )

    # 9. No duplication through the authorization record: only a bare claim (or no record at
    #    all) may proceed. Every terminal state — including purchase_unknown — is a refusal,
    #    because a state-checked resubmission is permitted and a duplicate is not.
    if facts.prior_authorization_state is not None:
        if facts.prior_authorization_state == PurchaseAuthorizationStatus.AUTHORIZED.value:
            pass  # a persisted claim with no submission outcome; resubmission is permitted
        else:
            return blocked(
                f"An authorization record already exists for this order in state "
                f"'{facts.prior_authorization_state}' — submitting another purchase would "
                "risk a duplicate. The existing record is returned instead; unknown outcomes "
                "are never retried automatically."
            )

    # 10. Canonical amount and currency.
    if facts.canonical_amount is None or facts.canonical_amount <= 0:
        return blocked(
            "The order's canonical amount is missing or not positive — nothing is purchasable."
        )
    if (facts.currency or "").upper() != PURCHASE_CURRENCY:
        return blocked(
            f"The order currency {facts.currency!r} is not the supported purchase currency "
            f"({PURCHASE_CURRENCY}) — a purchase is never authorized across currencies."
        )

    return AuthorityDecision(
        decision=Decision.APPROVED,
        reason="Within delegated purchase authority: the confirmed order, its verified TEST "
        "payment, and the persisted merchant identity all agree, and no purchase is on record.",
        action=PurchaseAction.PLACE_MERCHANT_ORDER,
        requested_amount=facts.canonical_amount,
    )
