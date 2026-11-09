"""
The Buyer Agent — acts for the customer.

Holds the Recovery Mandate, watches the real transaction, and when a deviation is found, drives
the mission through: investigate -> propose -> (Authority Engine) -> negotiate -> execute ->
verify. It can propose anything; it can execute nothing that the Authority Engine hasn't
approved first.
"""
from __future__ import annotations

from app.agents.reasoning_provider import ReasoningProvider
from app.authority.engine import AuthorityDecision, ProposedAction, evaluate
from app.models.message import MessageType, NegotiationMessage, Party
from app.models.mission import AllowedAction, Mission, MissionStatus


class BuyerAgent:
    def __init__(self, reasoning: ReasoningProvider) -> None:
        self.reasoning = reasoning

    async def open_claim(self, mission: Mission, deviation: str) -> NegotiationMessage:
        """First step after a deviation is detected: state the claim to the merchant side."""
        text = await self.reasoning.draft_negotiation_message(
            {"kind": "claim", "deviation": deviation}
        )
        mission.status = MissionStatus.NEGOTIATING
        return NegotiationMessage(
            mission_id=mission.mission_id,
            sender=Party.BUYER_AGENT,
            type=MessageType.CLAIM,
            claim_description=text,
        )

    async def evaluate_incoming_offer(
        self, mission: Mission, offered_remedy: str, offered_amount
    ) -> tuple[str, AuthorityDecision | None]:
        """
        Decide whether an incoming offer from the Merchant Agent satisfies the objective.
        Returns ("accept" | "counter" | "reject", authority_decision_if_spend_required).
        """
        verdict = await self.reasoning.evaluate_offer(
            {
                "offered_amount": str(offered_amount or 0),
                "remaining_budget": str(mission.mandate.authority.remaining_budget),
                "objective_met": offered_remedy in ("replacement", "full_refund"),
            }
        )

        if verdict == "accept" and offered_amount:
            proposal = ProposedAction(
                action=AllowedAction.PAY_RECOVERY_COST, amount=offered_amount
            )
            decision = evaluate(mission.mandate.authority, proposal)
            if not decision.approved:
                # The offer looked acceptable to the reasoning layer, but exceeds what the
                # user actually delegated. The Authority Engine's word is final.
                return "escalate", decision
            return "accept", decision

        return verdict, None
