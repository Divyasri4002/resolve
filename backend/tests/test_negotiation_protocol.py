"""
Unit tests for the negotiation protocol state machine. No database, no network — the protocol
is validated purely on legal-transition logic.
"""
import pytest

from app.models.message import MessageType, NegotiationMessage, Party
from app.protocol.negotiation import (
    MAX_NEGOTIATION_ROUNDS,
    IllegalTransition,
    Negotiation,
    NegotiationState,
)


def msg(mission_id, sender, type_, **kw):
    return NegotiationMessage(mission_id=mission_id, sender=sender, type=type_, **kw)


def test_happy_path_claim_offer_accept():
    neg = Negotiation(mission_id="m1")
    neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.CLAIM, claim_description="damaged"))
    assert neg.state is NegotiationState.CLAIM_OPEN

    neg.apply(
        msg(
            "m1",
            Party.MERCHANT_AGENT,
            MessageType.OFFER,
            proposed_remedy="replacement",
            proposed_amount=4500,
        )
    )
    assert neg.state is NegotiationState.OFFER_PENDING

    neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.ACCEPT, accepted_message_id="x"))
    assert neg.state is NegotiationState.SETTLED
    assert neg.is_terminal


def test_reject_returns_to_claim_open_for_further_negotiation():
    neg = Negotiation(mission_id="m1")
    neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.CLAIM, claim_description="wrong item"))
    neg.apply(
        msg("m1", Party.MERCHANT_AGENT, MessageType.OFFER, proposed_remedy="partial_refund", proposed_amount=500)
    )
    neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.REJECT))
    assert neg.state is NegotiationState.CLAIM_OPEN


def test_cannot_offer_before_a_claim_exists():
    neg = Negotiation(mission_id="m1")
    with pytest.raises(IllegalTransition):
        neg.apply(
            msg("m1", Party.MERCHANT_AGENT, MessageType.OFFER, proposed_remedy="refund", proposed_amount=100)
        )


def test_cannot_accept_before_any_offer():
    neg = Negotiation(mission_id="m1")
    neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.CLAIM, claim_description="lost package"))
    with pytest.raises(IllegalTransition):
        neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.ACCEPT))


def test_escalate_is_legal_from_any_open_state():
    neg = Negotiation(mission_id="m1")
    neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.CLAIM, claim_description="customs surprise"))
    neg.apply(
        msg("m1", Party.MERCHANT_AGENT, MessageType.ESCALATE, escalation_reason="over merchant budget")
    )
    assert neg.state is NegotiationState.ESCALATED
    assert neg.is_terminal


def test_negotiation_auto_escalates_past_max_rounds():
    neg = Negotiation(mission_id="m1")
    neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.CLAIM, claim_description="damaged"))
    neg.apply(
        msg("m1", Party.MERCHANT_AGENT, MessageType.OFFER, proposed_remedy="partial_refund", proposed_amount=100)
    )
    for i in range(MAX_NEGOTIATION_ROUNDS):
        sender = Party.BUYER_AGENT if i % 2 == 0 else Party.MERCHANT_AGENT
        neg.apply(
            msg("m1", sender, MessageType.COUNTER, proposed_remedy="partial_refund", proposed_amount=100 + i)
        )
    assert neg.state is NegotiationState.ESCALATED


def test_no_message_is_legal_once_settled():
    neg = Negotiation(mission_id="m1")
    neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.CLAIM, claim_description="damaged"))
    neg.apply(
        msg("m1", Party.MERCHANT_AGENT, MessageType.OFFER, proposed_remedy="replacement", proposed_amount=0)
    )
    neg.apply(msg("m1", Party.BUYER_AGENT, MessageType.ACCEPT))
    with pytest.raises(IllegalTransition):
        neg.apply(msg("m1", Party.MERCHANT_AGENT, MessageType.COUNTER, proposed_remedy="x", proposed_amount=1))
