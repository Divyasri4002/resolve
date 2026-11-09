"""
The offline agent's step policy — the rule table behind `MockReasoningProvider.choose_agent_action`
(Task CORE-2, Phase 15).

The mock provider is a deterministic stand-in for a reasoning model, and this is the part it stands
in for: given nothing but the canonical context, decide whether the next step is a declared tool or
a sentence to the user. It exists so the whole bounded loop, its persistence, its guards and its
tests run with no vendor, no key and no network.

Two things it deliberately is not:

* **Not a second authority.** It proposes. The tools it names inherit the guards of the operations
  they wrap, and the Authority Engine still decides at execution time. When this policy picks
  `resolve_issue` for a remedy the mandate never delegated, the orchestrator refuses and says so.
* **Not a parser of the user's will beyond the obvious.** It matches a product the user named, and
  it classifies a complaint into one of the issue categories the product already recognises. Both
  are keyword rules, and both are only ever run against a server-held list — a title the user has
  not chosen from cannot be selected, and an issue type outside `IssueType` is refused by the tool
  schema rather than guessed at.

The shape of every decision is the strict `AgentStepDecision`; anything this policy cannot express
as a declared tool or a sentence becomes a refusal to act.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.models.agent import AgentStepDecision

# The built-in terminator of the loop: a step whose whole effect is a sentence to the user. It is
# not a capability, it is how the agent stops, and it is declared alongside the tools so a provider
# always has one legal answer.
RESPOND_ACTION = "respond"

_ISSUE_CLASSIFICATION: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("damaged", ("damaged", "broken", "cracked", "dented", "arrived damaged")),
    ("defective", ("defective", "not working", "doesn't work", "stopped working", "faulty")),
    ("wrong_item", ("wrong item", "wrong product", "different item", "not what i ordered", "wrong one")),
    ("missing_item", ("missing", "incomplete", "only got one", "parts missing", "nothing inside")),
    ("not_received", ("not received", "never arrived", "did not arrive", "hasn't arrived", "no delivery")),
    ("quality_issue", ("poor quality", "low quality", "bad quality", "not as described", "fake", "counterfeit")),
)

_PREFERENCE_CLASSIFICATION: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("refund", ("refund", "money back", "my money", "reimburse")),
    ("replacement", ("replacement", "replace it", "send another", "new one", "exchange")),
)

# How a mock answers "which of these did you mean" — an ordinal, or a distinctive word from one
# title. Nothing here matches on price or merchant, because those are not how a buyer points.
_ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "1st": 1,
    "2nd": 2,
    "3rd": 3,
}


def next_step(context: dict[str, Any], available_tools: Iterable[str]) -> AgentStepDecision:
    """Decide the next step of one agent turn from the canonical context alone."""
    available = set(available_tools)
    mission = context.get("mission") or {}
    issue = context.get("issue")
    order = context.get("order")
    assessment = context.get("assessment")
    resolution = context.get("resolution")
    shopping = context.get("shopping")
    message = (context.get("current_user_message") or "").strip()

    if issue is not None:
        return _post_purchase_step(available, context, issue, assessment, resolution, message)

    if order is not None:
        return _after_order_step(available, order, issue, message)

    if shopping is None:
        return AgentStepDecision.respond(
            f"This mission is at {mission.get('status', 'an unknown stage')}. The tools available "
            "here do not apply to it, so nothing was changed."
        )

    return _shopping_step(available, shopping, mission, message)


def _post_purchase_step(
    available: set[str],
    context: dict[str, Any],
    issue: dict[str, Any],
    assessment: dict[str, Any] | None,
    resolution: dict[str, Any] | None,
    message: str,
) -> AgentStepDecision:
    issue_id = issue.get("issue_id")

    # The round trip (Phase 13): a question was asked, the user has now answered it, so the issue
    # is re-assessed with their words in it. This is a resume, not a restart — the same issue, the
    # same assessment row, the same authority gate.
    if issue.get("awaiting_user_information") and message:
        return _pick(available, "assess_issue", {"issue_id": issue_id})

    if assessment is None:
        return _pick(available, "assess_issue", {"issue_id": issue_id})

    if resolution is None:
        proposed = assessment.get("proposed_action")
        if proposed == "escalate_to_human":
            return _pick(available, "escalate_to_human", {"issue_id": issue_id})
        if proposed == "request_more_information":
            return _pick(
                available,
                "request_more_information",
                {
                    "issue_id": issue_id,
                    "question": (
                        "I need one more detail before I can continue: "
                        f"{assessment.get('decision_reason') or 'what the issue looked like when it arrived'}"
                    ),
                },
            )
        if proposed == "no_action":
            return AgentStepDecision.respond(
                "My review of this issue found no action to take, and I have not changed anything. "
                "If you think that is wrong, ask me to escalate it for human review."
            )
        return _pick(available, "resolve_issue", {"issue_id": issue_id})

    state = resolution.get("resolution_state")
    if state == "more_information_required":
        question = issue.get("more_information_question") or (
            "What exactly happened, and when?"
        )
        return AgentStepDecision.respond(question)
    if state in {"denied", "pending", "failed"}:
        # A refusal is a decision about the mandate as it stood at that moment, not a verdict on the
        # mission: if the buyer later delegates the remedy, the same proposal must be re-examinable.
        # Within one message, though, bouncing assess -> resolve -> assess would just burn the step
        # budget re-deriving the same answer, so a refusal this turn is reported, not retried.
        called = [
            entry.get("tool")
            for entry in (context.get("this_turn", {}).get("tools_already_called") or [])
        ]
        if state == "denied":
            if "resolve_issue" in called:
                return AgentStepDecision.respond(_resolution_recap(context))
            return _pick(available, "assess_issue", {"issue_id": issue_id})
        return _pick(available, "resolve_issue", {"issue_id": issue_id})
    return AgentStepDecision.respond(_resolution_recap(context))


def _after_order_step(
    available: set[str], order: dict[str, Any], issue: dict[str, Any] | None, message: str
) -> AgentStepDecision:
    if order.get("status") == "order_created":
        return AgentStepDecision.respond(
            f"RESOLVE order {order.get('order_id')} exists for "
            f"{order.get('title')} at {order.get('amount')} {order.get('currency')}. Confirming "
            "that order record is a step only you can take; once you do, tell me if anything is "
            "wrong with the purchase."
        )
    problem = _classify(message, _ISSUE_CLASSIFICATION)
    if problem is not None and message:
        return _pick(
            available,
            "report_issue",
            {
                "issue_type": problem,
                "description": message[:2000],
                "requested_resolution": _classify(message, _PREFERENCE_CLASSIFICATION),
            },
        )
    return AgentStepDecision.respond(
        f"Order {order.get('order_id')} is confirmed and nothing is reported against it. "
        "If something went wrong with the purchase, tell me what happened and I will record it."
    )


def _shopping_step(
    available: set[str],
    shopping: dict[str, Any],
    mission: dict[str, Any],
    message: str,
) -> AgentStepDecision:
    status = mission.get("status")
    products = shopping.get("discovered_products") or []

    if not products:
        if shopping.get("missing_info") and not message:
            return AgentStepDecision.respond(
                shopping.get("clarification_prompt")
                or "What are you looking for, and what is your budget?"
            )
        return _pick(available, "search_products", {"query": _search_query(shopping, message)})

    if not shopping.get("selected_product"):
        if _is_asking_for_options(message):
            pending = _pending_variant_question(products)
            if pending is not None:
                return AgentStepDecision.respond(
                    f"{pending.get('title')} comes in the options the store itself publishes: "
                    f"{', '.join(pending.get('variant_options') or []) or 'none it reported'}. "
                    "Which one do you want?"
                )
        chosen = _match_product(message, products)
        if chosen is not None:
            if chosen.get("requires_variant_clarification"):
                return _pick(
                    available,
                    "clarify_variant",
                    {
                        "product_id": chosen.get("product_id"),
                        "answer": message[:120] or "default",
                    },
                )
            return _pick(available, "select_product", {"product_id": chosen.get("product_id")})
        return AgentStepDecision.respond(_product_recap(shopping))

    if status in ("product_selected", "needs_clarification", "discovery_completed"):
        return _pick(available, "prepare_checkout", {})

    if status == "checkout_review":
        return AgentStepDecision.respond(
            "Here is the checkout review for "
            f"{(shopping.get('selected_product') or {}).get('title')}. Confirming it and paying are "
            "steps only you can take — I have not charged anything."
        )
    if status == "checkout_confirmed":
        return AgentStepDecision.respond(
            "Checkout is confirmed by you. The payment itself is yours to complete; nothing has "
            "been charged by me."
        )
    if status == "payment_pending":
        return AgentStepDecision.respond(
            "A test payment has been started and is waiting for you in the Razorpay checkout. "
            "I cannot complete a payment, and none is verified yet."
        )
    if status == "payment_failed":
        return AgentStepDecision.respond(
            "That payment did not verify, so no order exists. Nothing was charged."
        )
    if status == "payment_verified":
        return AgentStepDecision.respond(
            "Payment is verified, and the order record is created from that verified state by the "
            "confirmation step. I have not created an order and I cannot."
        )
    return AgentStepDecision.respond(
        f"This mission is at '{status}'. I have not changed anything."
    )


def _pick(
    available: set[str], name: str, arguments: dict[str, Any] | None
) -> AgentStepDecision:
    """A tool this policy wants that the surface does not declare is a refusal, never a guess."""
    if name not in available:
        return AgentStepDecision.respond(
            f"I would need to use '{name}', which is not one of my declared capabilities, so I "
            "did nothing."
        )
    clean = {key: value for key, value in (arguments or {}).items() if value is not None}
    return AgentStepDecision.tool_call(name, clean)


def _classify(message: str, table: tuple[tuple[str, tuple[str, ...]], ...]) -> str | None:
    lowered = message.lower()
    for value, tokens in table:
        if any(token in lowered for token in tokens):
            return value
    return None


def _search_query(shopping: dict[str, Any], message: str) -> str | None:
    parts = [shopping.get("brand"), shopping.get("category")]
    parts.extend(shopping.get("preferences") or [])
    query = " ".join(str(part) for part in parts if part).strip() or message.strip()
    return query[:200] if query else None


def _match_product(message: str, products: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    One product from THIS mission's own discovered list, or none.

    An ordinal ("the second one") or a word distinctive enough to appear in exactly one title. An
    ambiguous match is treated as no match — guessing between two products the buyer could still
    name is how a wrong order gets created.
    """
    lowered = message.lower()
    if not lowered:
        return None
    for word, index in _ORDINAL_WORDS.items():
        if word in lowered and 0 < index <= len(products):
            return products[index - 1]
    matches = [product for product in products if _names_product(lowered, product)]
    return matches[0] if len(matches) == 1 else None


_STOPWORD_TITLES = {
    "the",
    "a",
    "an",
    "and",
    "for",
    "with",
    "of",
    "to",
    "in",
    "on",
    "i",
    "want",
    "take",
    "get",
    "this",
    "that",
    "one",
    "item",
    "product",
    "please",
    "buy",
    "order",
}


def _names_product(lowered: str, product: dict[str, Any]) -> bool:
    title = str(product.get("title") or "").lower()
    tokens = {
        token
        for token in _title_tokens(title)
        if len(token) > 3 and token not in _STOPWORD_TITLES
    }
    return any(token in lowered for token in tokens)


def _title_tokens(title: str) -> list[str]:
    return [
        token
        for token in "".join(character if character.isalnum() else " " for character in title).split()
    ]


def _pending_variant_question(products: list[dict[str, Any]]) -> dict[str, Any] | None:
    for product in products:
        if product.get("requires_variant_clarification"):
            return product
    return None


def _is_asking_for_options(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in ("which size", "what size", "what options", "which options", "which one is available")
    )


def _product_recap(shopping: dict[str, Any]) -> str:
    products = (shopping.get("discovered_products") or [])[:3]
    lines = [
        f"• {product.get('title')} at {product.get('price')} from {product.get('merchant')}"
        for product in products
    ]
    return (
        f"I found {len(shopping.get('discovered_products') or [])} options. Top matches:\n"
        + "\n".join(lines)
        + "\nWhich one would you like? Name it, or say first, second or third."
    )


def _resolution_recap(context: dict[str, Any]) -> str:
    resolution = context.get("resolution") or {}
    state = resolution.get("resolution_state")
    action = resolution.get("action")
    note = resolution.get("message") or resolution.get("resolution_note") or ""
    return (
        f"The resolution for this issue is recorded as '{state}' "
        f"(action: {action}, provider attempted: {bool(resolution.get('provider_attempted'))}). "
        f"{note}".strip()
    )
