"""
The Merchant Agent — acts for the seller.

Runs the same protocol as the Buyer Agent, from the other side: receives a claim, checks it
against the merchant's own policy and its OWN independent authority (its own spend ceiling,
its own allowed remedies), and proposes or accepts a settlement. It is a full, separate
participant — not a mock of a reply — with its own Authority Engine check on every offer it
makes.

Where a real merchant has no agent of their own, RESOLVE's Buyer Agent falls back to drafting
structured, policy-aware correspondence instead — this class is only instantiated for
merchants running the protocol themselves.
"""
from __future__ import annotations

from decimal import Decimal

from app.agents.reasoning_provider import ReasoningProvider
from app.authority.engine import AuthorityDecision, ProposedAction, evaluate
from app.models.message import MessageType, NegotiationMessage, Party
from app.models.mission import AllowedAction, Authority


class MerchantAgent:
    def __init__(self, reasoning: ReasoningProvider, authority: Authority, policy: dict) -> None:
        self.reasoning = reasoning
        self.authority = authority  # the MERCHANT's own bounded authority, independent of the buyer's
        self.policy = policy  # e.g. {"replacement_available": True, "return_window_days": 30}

    async def respond_to_claim(
        self, mission_id: str, claim: NegotiationMessage
    ) -> tuple[NegotiationMessage, AuthorityDecision]:
        """
        Check the claim against merchant policy and this agent's own authority, then propose a
        remedy. Every remedy that costs the merchant money passes through THIS agent's own
        Authority Engine check — a merchant-side bot with no spend ceiling would defeat the
        whole point of a bounded, two-sided negotiation.
        """
        if self.policy.get("replacement_available", False):
            remedy, amount = "replacement", Decimal(str(self.policy.get("replacement_cost", 0)))
            action = AllowedAction.REQUEST_REPLACEMENT
        else:
            remedy = "partial_refund"
            amount = Decimal(str(self.policy.get("partial_refund_amount", 0)))
            action = AllowedAction.PAY_RECOVERY_COST

        decision = evaluate(self.authority, ProposedAction(action=action, amount=amount))

        if not decision.approved:
            message = NegotiationMessage(
                mission_id=mission_id,
                sender=Party.MERCHANT_AGENT,
                type=MessageType.ESCALATE,
                escalation_reason=decision.reason,
            )
            return message, decision

        text = await self.reasoning.draft_negotiation_message(
            {"kind": "offer", "remedy": remedy, "amount": str(amount)}
        )
        message = NegotiationMessage(
            mission_id=mission_id,
            sender=Party.MERCHANT_AGENT,
            type=MessageType.OFFER,
            proposed_remedy=remedy,
            proposed_amount=amount,
        )
        message.claim_description = text
        return message, decision
