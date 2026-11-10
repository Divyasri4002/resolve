"""
Unit tests for the Authority Engine — the one component that is not allowed to be wrong.

These tests need no database, no API key, and no running server: the whole point of the
Authority Engine is that it is pure, dependency-free Python.
"""
from decimal import Decimal

import pytest

from app.authority.engine import Decision, ProposedAction, apply_spend, evaluate
from app.models.mission import AllowedAction, Authority


def make_authority(**overrides) -> Authority:
    defaults = dict(
        allowed_actions=[
            AllowedAction.REQUEST_REFUND,
            AllowedAction.REQUEST_REPLACEMENT,
            AllowedAction.NEGOTIATE,
            AllowedAction.PAY_RECOVERY_COST,
        ],
        recovery_budget=Decimal("10000"),
        spent_so_far=Decimal("0"),
    )
    defaults.update(overrides)
    return Authority(**defaults)


def test_spend_within_budget_is_approved():
    authority = make_authority()
    decision = evaluate(authority, ProposedAction(AllowedAction.PAY_RECOVERY_COST, Decimal("4500")))
    assert decision.decision is Decision.APPROVED


def test_spend_over_budget_is_blocked():
    authority = make_authority()
    decision = evaluate(authority, ProposedAction(AllowedAction.PAY_RECOVERY_COST, Decimal("15000")))
    assert decision.decision is Decision.BLOCKED
    assert "exceeds remaining recovery authority" in decision.reason


def test_spend_exactly_at_remaining_budget_is_approved():
    authority = make_authority(spent_so_far=Decimal("6000"))
    decision = evaluate(authority, ProposedAction(AllowedAction.PAY_RECOVERY_COST, Decimal("4000")))
    assert decision.decision is Decision.APPROVED


def test_spend_one_rupee_over_remaining_budget_is_blocked():
    authority = make_authority(spent_so_far=Decimal("6000"))
    decision = evaluate(authority, ProposedAction(AllowedAction.PAY_RECOVERY_COST, Decimal("4000.01")))
    assert decision.decision is Decision.BLOCKED


def test_action_not_in_allow_list_is_blocked():
    authority = make_authority(allowed_actions=[AllowedAction.NEGOTIATE])
    decision = evaluate(authority, ProposedAction(AllowedAction.PAY_RECOVERY_COST, Decimal("100")))
    assert decision.decision is Decision.BLOCKED
    assert "never delegated" in decision.reason


def test_missing_amount_on_spend_is_blocked():
    authority = make_authority()
    decision = evaluate(authority, ProposedAction(AllowedAction.PAY_RECOVERY_COST, None))
    assert decision.decision is Decision.BLOCKED


def test_negative_amount_is_blocked():
    authority = make_authority()
    decision = evaluate(authority, ProposedAction(AllowedAction.PAY_RECOVERY_COST, Decimal("-1")))
    assert decision.decision is Decision.BLOCKED


def test_expired_mandate_blocks_every_action():
    from datetime import datetime, timedelta

    authority = make_authority(expires_at=datetime.utcnow() - timedelta(days=1))
    decision = evaluate(authority, ProposedAction(AllowedAction.NEGOTIATE))
    assert decision.decision is Decision.BLOCKED
    assert "expired" in decision.reason


def test_non_spend_action_within_allow_list_is_approved():
    authority = make_authority()
    decision = evaluate(authority, ProposedAction(AllowedAction.REQUEST_REPLACEMENT))
    assert decision.decision is Decision.APPROVED


def test_apply_spend_is_immutable_and_correct():
    authority = make_authority()
    updated = apply_spend(authority, Decimal("4500"))
    assert updated.spent_so_far == Decimal("4500")
    assert authority.spent_so_far == Decimal("0")  # original untouched
    assert updated.remaining_budget == Decimal("5500")


@pytest.mark.parametrize(
    "spent,requested,should_approve",
    [
        (Decimal("0"), Decimal("1"), True),
        (Decimal("9999"), Decimal("1"), True),
        (Decimal("10000"), Decimal("0.01"), False),
        (Decimal("0"), Decimal("10000.01"), False),
    ],
)
def test_budget_boundary_cases(spent, requested, should_approve):
    authority = make_authority(spent_so_far=spent)
    decision = evaluate(authority, ProposedAction(AllowedAction.PAY_RECOVERY_COST, requested))
    assert decision.approved is should_approve


# --------------------------------------------------------------------------- #
# classify_block (M22) — every blocked evaluate() outcome is pinned to exactly
# one category. The assessment layer routes on this classification, so any drift
# between evaluate()'s checks and classify_block's mirror of them fails here.
# --------------------------------------------------------------------------- #
from app.authority.engine import BlockCategory, classify_block  # noqa: E402
from app.config import get_settings  # noqa: E402


def _blocked(authority, proposal):
    decision = evaluate(authority, proposal)
    assert decision.decision is Decision.BLOCKED
    return decision


def test_classify_never_delegated_is_authority_gap():
    authority = make_authority(allowed_actions=[AllowedAction.NEGOTIATE])
    proposal = ProposedAction(AllowedAction.REQUEST_REFUND, Decimal("100"))
    decision = _blocked(authority, proposal)
    assert classify_block(authority, proposal, decision) is BlockCategory.AUTHORITY_GAP


def test_classify_expired_is_authority_gap():
    from datetime import datetime, timedelta

    authority = make_authority(expires_at=datetime.utcnow() - timedelta(days=1))
    proposal = ProposedAction(AllowedAction.NEGOTIATE)
    decision = _blocked(authority, proposal)
    assert classify_block(authority, proposal, decision) is BlockCategory.AUTHORITY_GAP


def test_classify_refund_above_ceiling_is_authority_gap():
    authority = make_authority(refund_limit=Decimal("100"))
    proposal = ProposedAction(AllowedAction.REQUEST_REFUND, Decimal("210"))
    decision = _blocked(authority, proposal)
    assert classify_block(authority, proposal, decision) is BlockCategory.AUTHORITY_GAP


def test_classify_refund_disabled_is_deployment_policy(monkeypatch):
    monkeypatch.setattr(get_settings(), "shopping_refund_authority_enabled", False)
    authority = make_authority()
    proposal = ProposedAction(AllowedAction.REQUEST_REFUND, Decimal("100"))
    decision = _blocked(authority, proposal)
    assert classify_block(authority, proposal, decision) is BlockCategory.DEPLOYMENT_POLICY


def test_classify_refund_missing_amount_is_proposal_invalid():
    authority = make_authority()
    proposal = ProposedAction(AllowedAction.REQUEST_REFUND, None)
    decision = _blocked(authority, proposal)
    assert classify_block(authority, proposal, decision) is BlockCategory.PROPOSAL_INVALID


def test_classify_replacement_disabled_is_deployment_policy(monkeypatch):
    monkeypatch.setattr(get_settings(), "shopping_replacement_authority_enabled", False)
    authority = make_authority()
    proposal = ProposedAction(AllowedAction.REQUEST_REPLACEMENT)
    decision = _blocked(authority, proposal)
    assert classify_block(authority, proposal, decision) is BlockCategory.DEPLOYMENT_POLICY


def test_classify_return_disabled_is_deployment_policy(monkeypatch):
    monkeypatch.setattr(get_settings(), "shopping_return_authority_enabled", False)
    authority = make_authority(allowed_actions=[AllowedAction.REQUEST_RETURN])
    proposal = ProposedAction(AllowedAction.REQUEST_RETURN)
    decision = _blocked(authority, proposal)
    assert classify_block(authority, proposal, decision) is BlockCategory.DEPLOYMENT_POLICY


def test_classify_switch_off_and_never_delegated_is_deployment_policy(monkeypatch):
    # The compound case: evaluate() reports "never delegated" (its check runs first), but no
    # delegation can cure a deployment whose switch is off — so the classification must be
    # policy, not an authority gap that would send the owner to delegate into a wall.
    monkeypatch.setattr(get_settings(), "shopping_replacement_authority_enabled", False)
    authority = make_authority(allowed_actions=[AllowedAction.NEGOTIATE])
    proposal = ProposedAction(AllowedAction.REQUEST_REPLACEMENT)
    decision = _blocked(authority, proposal)
    assert "never delegated" in decision.reason  # evaluate()'s own verdict, unchanged
    assert classify_block(authority, proposal, decision) is BlockCategory.DEPLOYMENT_POLICY


def test_classify_spend_over_budget_is_authority_gap():
    authority = make_authority()
    proposal = ProposedAction(AllowedAction.PAY_RECOVERY_COST, Decimal("15000"))
    decision = _blocked(authority, proposal)
    assert classify_block(authority, proposal, decision) is BlockCategory.AUTHORITY_GAP


def test_classify_spend_missing_amount_is_proposal_invalid():
    authority = make_authority()
    proposal = ProposedAction(AllowedAction.PAY_RECOVERY_COST, None)
    decision = _blocked(authority, proposal)
    assert classify_block(authority, proposal, decision) is BlockCategory.PROPOSAL_INVALID


def test_classify_refuses_an_approved_decision():
    authority = make_authority()
    proposal = ProposedAction(AllowedAction.PAY_RECOVERY_COST, Decimal("100"))
    decision = evaluate(authority, proposal)
    assert decision.approved
    with pytest.raises(ValueError):
        classify_block(authority, proposal, decision)
