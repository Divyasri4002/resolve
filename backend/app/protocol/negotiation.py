"""
The negotiation protocol state machine.

This is what lets a Buyer Agent and a Merchant Agent settle a dispute directly, without a
human relaying messages between them. It is intentionally a small, closed state machine —
not a free-form chat — so each side can validate what a legal next message looks like, and so
the whole exchange is a clean, replayable log (every NegotiationMessage is persisted to the
`messages` table in the order it happened).

Valid transitions:

    (start) --CLAIM--> CLAIM_OPEN
    CLAIM_OPEN --EVIDENCE--> CLAIM_OPEN            (either side may add evidence any time)
    CLAIM_OPEN --OFFER--> OFFER_PENDING
    OFFER_PENDING --COUNTER--> OFFER_PENDING       (back-and-forth, bounded by max_rounds)
    OFFER_PENDING --ACCEPT--> SETTLED
    OFFER_PENDING --REJECT--> CLAIM_OPEN           (objective still unmet, keep negotiating)
    (any state) --ESCALATE--> ESCALATED            (either side's Authority Engine blocked it)

Nothing in this file moves money. It only decides whether a message is legal right now, and
what state the negotiation is in afterward. The actual payment happens in
app/payments/razorpay_client.py, and only after the Authority Engine has approved it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.models.message import MessageType, NegotiationMessage

MAX_NEGOTIATION_ROUNDS = 6  # hard ceiling — prevents an infinite offer/counter loop


class NegotiationState(StrEnum):
    NOT_STARTED = "not_started"
    CLAIM_OPEN = "claim_open"
    OFFER_PENDING = "offer_pending"
    SETTLED = "settled"
    ESCALATED = "escalated"


class IllegalTransition(Exception):
    """Raised when a message type isn't valid from the negotiation's current state."""


_LEGAL_NEXT: dict[NegotiationState, set[MessageType]] = {
    NegotiationState.NOT_STARTED: {MessageType.CLAIM},
    NegotiationState.CLAIM_OPEN: {
        MessageType.EVIDENCE,
        MessageType.OFFER,
        MessageType.ESCALATE,
    },
    NegotiationState.OFFER_PENDING: {
        MessageType.COUNTER,
        MessageType.ACCEPT,
        MessageType.REJECT,
        MessageType.EVIDENCE,
        MessageType.ESCALATE,
    },
    NegotiationState.SETTLED: set(),
    NegotiationState.ESCALATED: set(),
}


@dataclass
class Negotiation:
    """In-memory negotiation state for one mission. The full message history is the audit
    trail; this object just tracks 'where are we right now' and enforces legal transitions."""

    mission_id: str
    state: NegotiationState = NegotiationState.NOT_STARTED
    rounds: int = 0
    history: list[NegotiationMessage] = field(default_factory=list)

    def apply(self, message: NegotiationMessage) -> NegotiationState:
        if message.type not in _LEGAL_NEXT[self.state]:
            raise IllegalTransition(
                f"'{message.type.value}' is not a legal message while negotiation is in "
                f"state '{self.state.value}'."
            )

        if message.type in (MessageType.OFFER, MessageType.COUNTER):
            self.rounds += 1
            if self.rounds > MAX_NEGOTIATION_ROUNDS:
                self.state = NegotiationState.ESCALATED
                self.history.append(message)
                return self.state

        match message.type:
            case MessageType.CLAIM:
                self.state = NegotiationState.CLAIM_OPEN
            case MessageType.EVIDENCE:
                pass  # state unchanged — evidence never advances the negotiation by itself
            case MessageType.OFFER | MessageType.COUNTER:
                self.state = NegotiationState.OFFER_PENDING
            case MessageType.ACCEPT:
                self.state = NegotiationState.SETTLED
            case MessageType.REJECT:
                self.state = NegotiationState.CLAIM_OPEN
            case MessageType.ESCALATE:
                self.state = NegotiationState.ESCALATED

        self.history.append(message)
        return self.state

    @property
    def is_terminal(self) -> bool:
        return self.state in (NegotiationState.SETTLED, NegotiationState.ESCALATED)
