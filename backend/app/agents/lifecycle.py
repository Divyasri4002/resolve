"""
The shopping mission's lifecycle ladder, and the one rule that protects it (Task CORE-2, Phase 12).

A conversation is allowed to *advance* a mission. It is not allowed to move one backwards. Before
this rule existed, `/agent` re-parsed the shopping intent on every message and rewrote the status
from that parse, so a buyer who kept chatting after their order was confirmed could watch the
mission fall back to `discovery_completed` — a state that contradicts the order row, the payment,
and the audit trail.

The ladder is a rank, not a permission: whether a step may happen at all is decided by the guards of
the operation that wants to move the mission (ownership, `payment_verified` before an order, a
confirmed order before an issue, the Authority Engine before a remedy). This module only answers the
narrower question — *if* a conversational state write is allowed, is it forward, sideways, or
backwards?

`payment_failed` shares a rank with `payment_pending` on purpose. A failed test payment is not
progress lost: the mission is allowed to try again, and a retry from `payment_failed` back to
`payment_pending` is the same rung, not a descent.
"""
from __future__ import annotations

from app.models.mission import MissionStatus

# Rungs of the shopping lifecycle, in the order the product actually moves through them. Two states
# share a rank where a legitimate sideways retry exists between them.
_LIFECYCLE_RANK: dict[str, int] = {
    MissionStatus.SHOPPING_INTENT.value: 0,
    MissionStatus.NEEDS_CLARIFICATION.value: 1,
    MissionStatus.READY_FOR_DISCOVERY.value: 2,
    MissionStatus.DISCOVERY_COMPLETED.value: 3,
    MissionStatus.PRODUCT_SELECTED.value: 4,
    MissionStatus.CHECKOUT_REVIEW.value: 5,
    MissionStatus.CHECKOUT_CONFIRMED.value: 6,
    MissionStatus.PAYMENT_PENDING.value: 7,
    MissionStatus.PAYMENT_FAILED.value: 7,
    MissionStatus.PAYMENT_VERIFIED.value: 8,
    MissionStatus.ORDER_CREATED.value: 9,
    MissionStatus.ORDER_CONFIRMED.value: 10,
    MissionStatus.AWAITING_POST_PURCHASE_CONTEXT.value: 11,
}
# `delivered` (MissionStatus.DELIVERED) is deliberately absent: the demo delivery transition is
# endpoint-only, and a rank here would let a conversational turn fabricate it — both the write
# to it and any conversational move from it fall through the None-rank guard below untouched.

# The rung from which a mission has committed to something: a product is picked, a checkout is
# reviewed or confirmed, money has been paid, an order exists. A conversation may still refine the
# discovery ladder below this line — asking again about budget or searching again is normal — but it
# may never drag a mission back across it.
PROTECTED_FROM_RANK = _LIFECYCLE_RANK[MissionStatus.PRODUCT_SELECTED.value]

# States a conversational turn may write at all. Anything outside this set — a recovery mission's
# `executing`, a resolved mission — is not the shopping ladder's business and is left alone.
CONVERSATIONAL_STATES = frozenset(_LIFECYCLE_RANK)


def lifecycle_rank(status: str) -> int | None:
    return _LIFECYCLE_RANK.get(status)


def guarded_shopping_status(current: str, candidate: str) -> str:
    """
    The status a conversational turn is allowed to write.

    Forward: the candidate. Backwards inside the discovery ladder — refining a budget after a search,
    or parking for clarification after a provider error: also the candidate, because those rungs are
    re-entrant by design. Backwards *out of* the protected range, or anywhere the current state is not
    a shopping-ladder state at all: the current status, unchanged. A mission that has selected a
    product, confirmed a checkout, paid, or reported an issue cannot be talked back into looking for
    products.
    """
    current_rank = _LIFECYCLE_RANK.get(current)
    candidate_rank = _LIFECYCLE_RANK.get(candidate)
    if current_rank is None or candidate_rank is None:
        # At least one side is not a shopping-ladder state: leave the mission where it is.
        return current
    if candidate_rank >= current_rank:
        return candidate
    if current_rank < PROTECTED_FROM_RANK:
        return candidate
    return current
