"""
The negotiation protocol messages exchanged between a Buyer Agent and a Merchant Agent
(or, when no Merchant Agent exists, drafted as structured correspondence to a human merchant).

The protocol is deliberately small and closed: five message types, each with a specific
meaning, so both sides can validate what the other one is allowed to say next instead of
parsing free-form text as if it were a command.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class MessageType(StrEnum):
    CLAIM = "claim"                # Buyer Agent: "here is what went wrong"
    EVIDENCE = "evidence"          # Either side: supporting documents/photos/tracking
    OFFER = "offer"                # Either side: a proposed remedy
    COUNTER = "counter"            # Either side: a modified proposal
    ACCEPT = "accept"              # Either side: agreement reached
    REJECT = "reject"              # Either side: remedy inadequate, restate the objective
    ESCALATE = "escalate"          # Either side: authority exceeded, handing to a human


class Party(StrEnum):
    BUYER_AGENT = "buyer_agent"
    MERCHANT_AGENT = "merchant_agent"
    HUMAN_MERCHANT = "human_merchant"


class NegotiationMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:10]}")
    mission_id: str
    sender: Party
    type: MessageType
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Present on CLAIM
    claim_description: str | None = None

    # Present on OFFER / COUNTER
    proposed_remedy: str | None = None  # "replacement" | "refund" | "partial_refund" | ...
    proposed_amount: Decimal | None = None

    # Present on EVIDENCE
    evidence_refs: list[str] = Field(default_factory=list)

    # Present on ACCEPT
    accepted_message_id: str | None = None

    # Present on ESCALATE
    escalation_reason: str | None = None

    def summary(self) -> str:
        """One-line, human-readable rendering — used to draw the negotiation log in the UI."""
        who = "RESOLVE" if self.sender == Party.BUYER_AGENT else "Merchant"
        match self.type:
            case MessageType.CLAIM:
                return f"{who}: {self.claim_description}"
            case MessageType.EVIDENCE:
                return f"{who} shared evidence: {', '.join(self.evidence_refs)}"
            case MessageType.OFFER | MessageType.COUNTER:
                amt = f" (₹{self.proposed_amount})" if self.proposed_amount is not None else ""
                return f"{who} proposed: {self.proposed_remedy}{amt}"
            case MessageType.ACCEPT:
                return f"{who} accepted the proposal."
            case MessageType.REJECT:
                return f"{who} rejected the proposal — objective still not met."
            case MessageType.ESCALATE:
                return f"{who} escalated: {self.escalation_reason}"
        return f"{who}: {self.type.value}"
