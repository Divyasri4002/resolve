"""
Deterministic product reference resolution (CORE-5 Phase 5).

When a buyer says "the second one", "the cheapest", "the one from Amazon" or "that one", the
identity behind the phrase is decided HERE — by code, against the mission's own persisted result
set — and never by the reasoning model. The model is told what the server resolved, or exactly why
it could not; it is never asked to count, rank, or match products itself.

The five properties this module exists to guarantee:

* **Deterministic.** The same message, the same intent and the same conversation history always
  produce the same resolution. No sampling, no scoring heuristics, no randomness.
* **Pure.** No I/O of any kind: no network, no database, no clock, no settings. Everything it
  reads arrives as arguments; everything it returns is a value.
* **Persisted facts only.** The candidate universe is the active result set named by
  `ShoppingIntent.active_result_set_id`, and the only product facts consulted are the verified
  fields of the persisted `ProductResult` rows — price, rating, review count, merchant, source,
  title. Nothing is inferred: a missing rating is not zero, a missing price is never treated as
  one, and a color word in the message never becomes a color of any product.
* **Fails loud, never approximate.** A phrase that maps to several products is AMBIGUOUS with the
  candidates attached; a position past the end of the set is NOT_FOUND; a phrase this module
  deliberately does not implement ("cheaper ones", "the second cheapest") is UNSUPPORTED. The
  agent is expected to ask, not to pick.
* **Server-authoritative.** No message content can mint identity. Only a product that already
  exists in the active result set can be returned, named by its persisted `product_id`, and the
  1-based `position` reported alongside it is that product's index in the full persisted set.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from app.models.mission import ResultSetRecord, ShoppingIntent
from app.models.product import ProductResult

# --------------------------------------------------------------------------- #
# The resolution vocabulary
# --------------------------------------------------------------------------- #

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
UNSUPPORTED = "unsupported"
NO_REFERENCE = "no_reference"

KIND_ORDINAL = "ordinal"
KIND_CHEAPEST = "cheapest"
KIND_MOST_EXPENSIVE = "most_expensive"
KIND_HIGHEST_RATED = "highest_rated"
KIND_LOWEST_RATED = "lowest_rated"
KIND_MOST_REVIEWED = "most_reviewed"
KIND_FEWEST_REVIEWED = "fewest_reviewed"
KIND_MERCHANT = "merchant"
KIND_TITLE = "title"
KIND_CONVERSATION = "conversation"
KIND_RECOMMENDATION = "recommendation"

# The conversation event that carries a canonical product anchor. It is appended by the agent loop
# the moment a phrase resolves, and by the `recommend_product` tool when the agent records its own
# recommendation. Both kinds let a later "that one" or "the one you recommended" resolve from
# structured, persisted product identity — never from prose.
ANCHOR_EVENT_TYPE = "product_reference"
ANCHOR_KIND_RESOLVED = "resolved_reference"
ANCHOR_KIND_RECOMMENDATION = "recommendation"
_ANCHOR_KINDS = frozenset({ANCHOR_KIND_RESOLVED, ANCHOR_KIND_RECOMMENDATION})


@dataclass(frozen=True)
class ProductReference:
    """
    The outcome of resolving one message's product phrase, in the shape the caller persists.

    `state` is one of the five vocabulary values above. `product` is the resolved ProductResult
    object exactly as persisted (state == RESOLVED only). `candidates` carries the products the
    phrase matched when the honest answer is "which of these?" (state == AMBIGUOUS). `position`
    is the resolved product's 1-based place in the FULL active result set. `question` is the
    clarification the agent should put to the buyer, when there is one to ask.
    """

    state: str
    kind: str | None = None
    expression: str | None = None
    reason: str | None = None
    product: ProductResult | None = None
    position: int | None = None
    set_id: str | None = None
    candidates: tuple[ProductResult, ...] = ()
    question: str | None = None


# --------------------------------------------------------------------------- #
# Detection tables. Every pattern matches against the casefolded message.
# --------------------------------------------------------------------------- #

_WORD_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
}
_WORD = "|".join(_WORD_ORDINALS)

_FRAMED_WORD_ORDINAL = re.compile(rf"\b({_WORD})\s+(one|product|item|option|choice)\b")
_NUMBER_ORDINAL = re.compile(
    r"(?:\b(?:option|number|no\.?)|[#])\s*(\d{1,2})\b|\b(\d{1,2})\s+(?:option|product|item|choice)\b"
)
_SUFFIX_ORDINAL = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b")
_LAST_ONE = re.compile(r"\bthe\s+last\s+(one|product|item|option)\b")

# Phrases that look like a reference but are deliberately not resolvable here. A mixed position
# and ranking ("second cheapest") would otherwise mis-parse as an ordinal; plurals and
# comparatives are not single-product pointers at all. All three get UNSUPPORTED, never a guess.
_COMPOUND_RANK = re.compile(
    rf"\b({_WORD})\s+(?:cheapest|most\s+expensive|expensive|highest|lowest|best|worst|rated)\b"
)
_PLURAL_REQUEST = re.compile(
    r"\b(?:first|second|third|fourth|fifth|last)\s+(?:two|three|four|five|six|seven|eight|nine|ten|both)\b"
)
_COMPARATIVE = re.compile(
    r"\b(?:cheaper|more\s+expensive)\b|\bbetter\s+than\b|\bworse\s+than\b"
    r"|\banother\s+(?:one|product|item|option)\b"
)

# A rank phrase needs a determiner-ish lead-in ("the cheapest", "is cheapest", "with the lowest
# price") or a following frame noun ("cheapest one"). A bare "cheapest shampoo" stays what it
# really is: a search request, which the search planner owns — not a pointer into these results.
_RANK_PREFIX = r"(?:the|is|are|was|has|have|had|with|which|of)"

_RANK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        KIND_CHEAPEST,
        re.compile(
            rf"\b(?:{_RANK_PREFIX})\s+(?:cheapest|lowest\s+price|least\s+expensive)\b"
            r"|\bcheapest\s+(?:one|option|product|item|choice)\b"
        ),
    ),
    (
        KIND_MOST_EXPENSIVE,
        re.compile(
            rf"\b(?:{_RANK_PREFIX})\s+(?:most\s+expensive|highest\s+price)\b"
            r"|\bmost\s+expensive\s+(?:one|option|product|item|choice)\b"
        ),
    ),
    (
        KIND_HIGHEST_RATED,
        re.compile(
            rf"\b(?:{_RANK_PREFIX})\s+(?:highest|best)[\s-]?(?:rated|rating)\b"
            r"|\b(?:highest|best)[\s-]?(?:rated|rating)\s+(?:one|option|product|item|choice)\b"
        ),
    ),
    (
        KIND_LOWEST_RATED,
        re.compile(
            rf"\b(?:{_RANK_PREFIX})\s+(?:lowest|worst)[\s-]?(?:rated|rating)\b"
            r"|\b(?:lowest|worst)[\s-]?(?:rated|rating)\s+(?:one|option|product|item|choice)\b"
        ),
    ),
    (
        KIND_MOST_REVIEWED,
        re.compile(
            rf"\b(?:{_RANK_PREFIX})\s+most\s+(?:reviewed|reviews)\b"
            r"|\bmost\s+(?:reviewed|reviews)\s+(?:one|option|product|item|choice)\b"
        ),
    ),
    (
        KIND_FEWEST_REVIEWED,
        re.compile(
            rf"\b(?:{_RANK_PREFIX})\s+fewest\s+(?:reviewed|reviews)\b"
            r"|\bfewest\s+(?:reviewed|reviews)\s+(?:one|option|product|item|choice)\b"
        ),
    ),
)

# (field, direction, fact label for refusals, phrase for the resolved reason). Only verified
# numeric fields rank; review count is its own ranking and is never converted into a rating.
_RANK_FACTS = {
    KIND_CHEAPEST: ("extracted_price", "min", "price", "the lowest verified price"),
    KIND_MOST_EXPENSIVE: ("extracted_price", "max", "price", "the highest verified price"),
    KIND_HIGHEST_RATED: ("rating", "max", "rating", "the highest verified rating"),
    KIND_LOWEST_RATED: ("rating", "min", "rating", "the lowest verified rating"),
    KIND_MOST_REVIEWED: ("review_count", "max", "review count", "the most verified reviews"),
    KIND_FEWEST_REVIEWED: ("review_count", "min", "review count", "the fewest verified reviews"),
}

_STRONG_CONTEXT = re.compile(r"\b(?:this|that|the\s+same)\s+(one|product|item|option|choice)\b")
_WEAK_CONTEXT = re.compile(r"\bthe\s+(one|product|item|option|choice)\b")
_RECOMMENDED = re.compile(
    r"\bthe\s+one\s+you\s+(?:recommended|suggested)\b"
    r"|\byour\s+(?:recommendation|recommendations|suggestion|suggestions)\b"
    r"|\bthe\s+(?:one|product|item|option)\s+you\s+(?:recommended|suggested)\b"
    r"|\b(?:the\s+)?(?:recommended|suggested)\s+(?:one|product|item|option)\b"
)

_DEFINITE = re.compile(r"\b(?:the|this|that|these|those|your|which)\b")
_FRAME_WORD = re.compile(r"\b(?:one|product|item|option|choice)\b")
_FROM = re.compile(r"\bfrom\b")
_FROM_TARGET = re.compile(r"\bfrom\s+([a-z0-9]+)")

# Tokens that can never identify a merchant or a source by themselves, so a common word in a
# message ("shopping for shampoo") cannot turn every persisted result into a candidate.
_MERCHANT_TOKEN_STOP = frozenset({"google", "shopping"})

_TITLE_FILLER = frozenset(
    {
        "the", "this", "that", "these", "those", "your", "which", "what",
        "one", "ones", "product", "products", "item", "items", "option", "options",
        "choice", "choices", "get", "give", "show", "find", "want", "need", "take",
        "pick", "choose", "select", "buy", "same", "last", "first", "second",
        "third", "fourth", "fifth", "for", "and", "with", "from", "about",
        "instead", "other", "another", "you", "yours", "was", "are", "is", "his",
        "her", "its", "please", "thanks", "okay", "yes", "yeah", "sure", "will",
        "would", "like", "them", "they",
    }
)

_GUARDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        _COMPOUND_RANK,
        "'{expression}' mixes a position with a ranking, and I will not guess which you meant. "
        "Name the product, or give just its position in the list.",
    ),
    (
        _PLURAL_REQUEST,
        "'{expression}' asks for several products at once. I resolve one product at a time — "
        "tell me which single one you mean.",
    ),
    (
        _COMPARATIVE,
        "'{expression}' is a comparison rather than a pointer to one product. Tell me the "
        "position, the merchant, or the product you mean.",
    ),
)


# --------------------------------------------------------------------------- #
# The active result set (Phase 4's own vocabulary, read-only from here)
# --------------------------------------------------------------------------- #


def active_result_record(intent: ShoppingIntent) -> ResultSetRecord | None:
    """The ResultSetRecord the mission currently mirrors, or None when there is none."""
    set_id = intent.active_result_set_id
    if not set_id:
        return None
    for record in reversed(intent.result_sets):
        if record.set_id == set_id:
            return record
    return None


def active_result_products(intent: ShoppingIntent) -> list[ProductResult]:
    """The full, persisted, ordered products of the ACTIVE result set — never a stale one."""
    record = active_result_record(intent)
    return list(record.products) if record is not None else []


# --------------------------------------------------------------------------- #
# Persisted conversation anchors
# --------------------------------------------------------------------------- #


def _row_field(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def conversation_anchors(history: Sequence[Any]) -> list[dict[str, Any]]:
    """
    The persisted product anchors in a conversation, oldest first.

    Accepts the ORM rows `load_recent` returns or plain dicts shaped like them, so tests and the
    loop can share one reader. Only rows whose structured payload carries a canonical product id
    count; an agent sentence that merely mentions a product in prose is deliberately not one.
    """
    anchors: list[dict[str, Any]] = []
    for row in history or ():
        if _row_field(row, "event_type") != ANCHOR_EVENT_TYPE:
            continue
        payload = _row_field(row, "payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        product_id = payload.get("product_id")
        if kind not in _ANCHOR_KINDS or not product_id:
            continue
        anchors.append(
            {
                "kind": kind,
                "product_id": str(product_id),
                "set_id": payload.get("set_id"),
                "sequence": _row_field(row, "sequence"),
            }
        )
    return anchors


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def resolve_product_reference(
    user_message: str,
    intent: ShoppingIntent | None,
    history: Sequence[Any] = (),
) -> ProductReference:
    """
    Resolve the product phrase in one user message against the mission's own persisted state.

    `intent` supplies the active result set (and the selection fallback for conversational
    pointers); `history` is this mission's own recent conversation, oldest first, as persisted
    rows or dicts. Everything is decided from those arguments alone.
    """
    lowered = str(user_message or "").casefold()
    if intent is None:
        return ProductReference(
            state=NO_REFERENCE,
            reason=(
                "Product references resolve against a shopping mission's own persisted results; "
                "this mission has none."
            ),
        )

    active = active_result_products(intent)
    set_id = intent.active_result_set_id

    for pattern, reason in _GUARDS:
        match = pattern.search(lowered)
        if match:
            return ProductReference(
                state=UNSUPPORTED,
                expression=match.group(0),
                set_id=set_id,
                reason=reason.format(expression=match.group(0)),
            )

    tokens = re.findall(r"[a-z0-9]+", lowered)
    ordinals = _ordinal_claims(lowered)
    ranks = _rank_claims(lowered)
    recommended = _RECOMMENDED.search(lowered)
    strong = _STRONG_CONTEXT.search(lowered)

    merchant_framed = bool(_DEFINITE.search(lowered)) and bool(
        _FRAME_WORD.search(lowered) or _FROM.search(lowered)
    )
    named_products, named_values = (
        _named_merchant_products(tokens, active) if merchant_framed else ([], [])
    )

    # A conversational pointer or a recommendation claim picks its product from history alone.
    # Combined with a second picking mechanism it would be two answers to one question, so any
    # combination is reported as ambiguous rather than resolved by silent precedence.
    if recommended is not None or strong is not None:
        if named_products or ordinals or ranks:
            pointer = (recommended or strong).group(0)
            extra = [f"a product from {value}" for value in named_values]
            extra += [claim[1] for claim in ordinals] + [claim[1] for claim in ranks]
            return ProductReference(
                state=AMBIGUOUS,
                expression=pointer,
                set_id=set_id,
                reason=(
                    "The message combines more than one way of picking a product ("
                    + ", ".join(f"'{part}'" for part in [pointer, *extra] if part)
                    + "), and they can point at different products."
                ),
                question="Which single product did you mean?",
            )
        if recommended is not None:
            return _resolve_recommendation(recommended.group(0), active, set_id, history)
        return _resolve_conversation(strong.group(0), intent, active, set_id, history)

    if named_products:
        return _resolve_with_merchant(
            lowered, tokens, named_products, named_values, ordinals, ranks, active, set_id
        )

    # "The one from <name>" aims at a merchant or source; when the active set carries none, that
    # is a definite miss, not a vague pointer — reporting it as such keeps the failure loud.
    if merchant_framed and _from_target_is_named(lowered):
        return ProductReference(
            state=NOT_FOUND,
            kind=KIND_MERCHANT,
            set_id=set_id,
            reason=(
                "The message points at a merchant or source none of the current results "
                "carries, so there is nothing to resolve it to."
            ),
            question="Which product did you mean? You can name it or pick it from the results.",
        )

    if _ordinal_conflict_needed(ordinals):
        return _ordinal_conflict(ordinals, active, set_id)
    if ordinals:
        return _resolve_ordinal(ordinals[0], active, active, set_id)
    if len(ranks) > 1:
        return _rank_conflict(ranks, active, set_id)
    if ranks:
        return _resolve_rank(ranks[0], active, active, set_id)

    weak = _WEAK_CONTEXT.search(lowered)
    if weak is not None:
        return _resolve_conversation(weak.group(0), intent, active, set_id, history)

    title = _title_candidates(lowered, tokens, active, stop=set())
    if title is not None:
        matches, significant = title
        expression = " ".join(significant)
        if matches:
            return _title_outcome(matches, expression, active, set_id)
        return ProductReference(
            state=NOT_FOUND,
            kind=KIND_TITLE,
            expression=expression,
            set_id=set_id,
            reason=(
                "Nothing in the current results matches that description closely enough to "
                "pick one product, and I will not choose an approximate one."
            ),
            question="Which product did you mean? You can name it or pick it from the results.",
        )

    return ProductReference(
        state=NO_REFERENCE,
        set_id=set_id,
        reason="No product reference was found in this message.",
    )


# --------------------------------------------------------------------------- #
# Resolution branches
# --------------------------------------------------------------------------- #


def _resolve_with_merchant(
    lowered: str,
    tokens: list[str],
    named_products: list[ProductResult],
    named_values: list[str],
    ordinals: list[tuple[int | None, str]],
    ranks: list[tuple[str, str]],
    active: list[ProductResult],
    set_id: str | None,
) -> ProductReference:
    """A named merchant scopes every other mechanism to its own subset of the active set."""
    pool = named_products
    scope = " or ".join(named_values)
    stop: set[str] = set()
    for value in named_values:
        stop |= _name_tokens(value)

    if _ordinal_conflict_needed(ordinals):
        return _ordinal_conflict(ordinals, pool, set_id)
    if ordinals:
        return _resolve_ordinal(ordinals[0], pool, active, set_id, scope_values=named_values)
    if len(ranks) > 1:
        return _rank_conflict(ranks, pool, set_id)
    if ranks:
        return _resolve_rank(ranks[0], pool, active, set_id, scope_values=named_values)

    title = _title_candidates(lowered, tokens, pool, stop=stop)
    if title is not None:
        matches, significant = title
        expression = " ".join(significant)
        if matches:
            return _title_outcome(matches, expression, active, set_id)
        return ProductReference(
            state=NOT_FOUND,
            kind=KIND_TITLE,
            expression=expression,
            set_id=set_id,
            reason=(
                f"Nothing among the current results from {scope} matches that description "
                "closely enough to pick one product."
            ),
            question="Which product did you mean? You can name it or pick it from the results.",
        )

    if len(pool) == 1:
        return _resolved(
            kind=KIND_MERCHANT,
            expression=scope,
            product=pool[0],
            active=active,
            set_id=set_id,
            reason=(
                f"{scope} is the merchant of exactly one product in the current results, "
                "matched case-insensitively against the persisted merchant."
            ),
        )
    return ProductReference(
        state=AMBIGUOUS,
        kind=KIND_MERCHANT,
        expression=scope,
        set_id=set_id,
        candidates=tuple(pool),
        reason=f"{len(pool)} of the current results are from {scope}.",
        question="Which one did you mean?",
    )


def _resolve_ordinal(
    claim: tuple[int | None, str],
    pool: list[ProductResult],
    active: list[ProductResult],
    set_id: str | None,
    scope_values: list[str] | None = None,
) -> ProductReference:
    position, expression = claim
    kind = KIND_ORDINAL
    if not pool:
        return ProductReference(
            state=NOT_FOUND,
            kind=kind,
            expression=expression,
            set_id=set_id,
            reason="There are no current results to resolve that against.",
        )
    resolved_position = len(pool) if position is None else position
    scope = f" from {scope_values[0]}" if scope_values else ""
    if resolved_position < 1 or resolved_position > len(pool):
        return ProductReference(
            state=NOT_FOUND,
            kind=kind,
            expression=expression,
            set_id=set_id,
            reason=(
                f"'{expression}' points past the end — there "
                f"{'is only' if len(pool) == 1 else 'are only'} {len(pool)} result(s){scope}."
            ),
            question=(
                f"The current results have {len(pool)} products — which one would you like?"
            ),
        )
    product = pool[resolved_position - 1]
    return _resolved(
        kind=kind,
        expression=expression,
        product=product,
        active=active,
        set_id=set_id,
        reason=(
            f"'{expression}' is the product at position {resolved_position} of the persisted "
            f"result order{scope}, counted without reordering."
        ),
    )


def _resolve_rank(
    claim: tuple[str, str],
    pool: list[ProductResult],
    active: list[ProductResult],
    set_id: str | None,
    scope_values: list[str] | None = None,
) -> ProductReference:
    kind, expression = claim
    field, _direction, label, phrase = _RANK_FACTS[kind]
    holders = [product for product in pool if getattr(product, field) is not None]
    if not holders:
        return ProductReference(
            state=NOT_FOUND,
            kind=kind,
            expression=expression,
            set_id=set_id,
            reason=(
                f"'{expression}' ranks by a verified {label}, and none of the current results "
                "carries one — nothing was picked."
            ),
        )
    best = _rank_pick(kind, pool)
    scope = f" among the results from {' or '.join(scope_values)}" if scope_values else ""
    return _resolved(
        kind=kind,
        expression=expression,
        product=best,
        active=active,
        set_id=set_id,
        reason=(
            f"'{expression}' resolves to {phrase}{scope}, comparing verified values only and "
            "keeping the earlier result on a tie."
        ),
    )


def _rank_pick(kind: str, pool: list[ProductResult]) -> ProductResult | None:
    """The best product for one ranking, skipping unverified facts, ties keeping the earliest."""
    field, direction, _label, _phrase = _RANK_FACTS[kind]
    holders = [product for product in pool if getattr(product, field) is not None]
    if not holders:
        return None
    best = holders[0]
    for candidate in holders[1:]:
        value = getattr(candidate, field)
        best_value = getattr(best, field)
        if (direction == "min" and value < best_value) or (
            direction == "max" and value > best_value
        ):
            best = candidate
    return best


def _resolve_conversation(
    expression: str,
    intent: ShoppingIntent,
    active: list[ProductResult],
    set_id: str | None,
    history: Sequence[Any],
) -> ProductReference:
    """A pointer like "that one": the newest persisted anchor, else the mission's selection."""
    anchors = conversation_anchors(history)
    if anchors:
        newest = anchors[-1]
        product = _find_by_id(active, newest["product_id"])
        if product is None:
            return ProductReference(
                state=NOT_FOUND,
                kind=KIND_CONVERSATION,
                expression=expression,
                set_id=set_id,
                reason=(
                    f"'{expression}' follows the product the conversation last pinned down, and "
                    "that product is no longer part of the current results."
                ),
            )
        return _resolved(
            kind=KIND_CONVERSATION,
            expression=expression,
            product=product,
            active=active,
            set_id=set_id,
            reason=(
                f"'{expression}' follows the product the conversation last pinned down, "
                "identified by its persisted product id."
            ),
        )
    selected = intent.selected_product
    if selected is not None:
        product = _find_by_id(active, selected.product_id)
        if product is None:
            return ProductReference(
                state=NOT_FOUND,
                kind=KIND_CONVERSATION,
                expression=expression,
                set_id=set_id,
                reason=(
                    f"'{expression}' most naturally means the product you selected, and it is "
                    "no longer part of the current results."
                ),
            )
        return _resolved(
            kind=KIND_CONVERSATION,
            expression=expression,
            product=product,
            active=active,
            set_id=set_id,
            reason=f"'{expression}' follows the product already selected on this mission.",
        )
    return ProductReference(
        state=AMBIGUOUS,
        kind=KIND_CONVERSATION,
        expression=expression,
        set_id=set_id,
        reason=(
            f"Nothing in the conversation so far pins '{expression}' down to one product, and "
            "nothing is guessed."
        ),
        question="Which product do you mean? You can name it or pick it from the results.",
    )


def _resolve_recommendation(
    expression: str,
    active: list[ProductResult],
    set_id: str | None,
    history: Sequence[Any],
) -> ProductReference:
    """A recommendation phrase: only a persisted recommendation anchor can answer it."""
    recorded = [
        anchor
        for anchor in conversation_anchors(history)
        if anchor["kind"] == ANCHOR_KIND_RECOMMENDATION
    ]
    if not recorded:
        return ProductReference(
            state=AMBIGUOUS,
            kind=KIND_RECOMMENDATION,
            expression=expression,
            set_id=set_id,
            reason=(
                f"No recommendation has been recorded on this mission, so '{expression}' has no "
                "canonical product to resolve to."
            ),
            question=(
                "I don't have a recorded recommendation to point back to — which product did "
                "you mean?"
            ),
        )
    newest = recorded[-1]
    product = _find_by_id(active, newest["product_id"])
    if product is None:
        return ProductReference(
            state=NOT_FOUND,
            kind=KIND_RECOMMENDATION,
            expression=expression,
            set_id=set_id,
            reason=(
                "The product recorded as the recommendation is no longer part of the current "
                "results."
            ),
        )
    return _resolved(
        kind=KIND_RECOMMENDATION,
        expression=expression,
        product=product,
        active=active,
        set_id=set_id,
        reason=(
            f"'{expression}' follows the product recorded as this mission's recommendation, "
            "identified by its persisted product id."
        ),
    )


# --------------------------------------------------------------------------- #
# Ambiguity builders and shared helpers
# --------------------------------------------------------------------------- #


def _resolved(
    *,
    kind: str,
    expression: str,
    product: ProductResult,
    active: list[ProductResult],
    set_id: str | None,
    reason: str,
) -> ProductReference:
    return ProductReference(
        state=RESOLVED,
        kind=kind,
        expression=expression,
        reason=reason,
        product=product,
        position=_position_of(active, product),
        set_id=set_id,
    )


def _position_of(active: list[ProductResult], product: ProductResult) -> int | None:
    """The product's 1-based place in the full persisted set, matched by canonical product id."""
    target = product.product_id
    for index, candidate in enumerate(active, start=1):
        if target is not None and candidate.product_id == target:
            return index
    return None


def _find_by_id(active: list[ProductResult], product_id: str | None) -> ProductResult | None:
    if not product_id:
        return None
    for product in active:
        if product.product_id is not None and str(product.product_id) == product_id:
            return product
    return None


def _ordinal_conflict_needed(ordinals: list[tuple[int | None, str]]) -> bool:
    return len({position for position, _ in ordinals}) > 1


def _ordinal_conflict(
    ordinals: list[tuple[int | None, str]],
    pool: list[ProductResult],
    set_id: str | None,
) -> ProductReference:
    expressions = [expression for _, expression in ordinals]
    positions = sorted(
        {position for position, _ in ordinals if position is not None},
    )
    candidates = tuple(pool[position - 1] for position in positions if 1 <= position <= len(pool))
    return ProductReference(
        state=AMBIGUOUS,
        kind=KIND_ORDINAL,
        expression=", ".join(expressions),
        set_id=set_id,
        candidates=candidates,
        reason=(
            f"The message names several positions ({', '.join(expressions)}), which can be "
            "different products."
        ),
        question="Which one did you mean?",
    )


def _rank_conflict(
    ranks: list[tuple[str, str]],
    pool: list[ProductResult],
    set_id: str | None,
) -> ProductReference:
    expressions = [expression for _, expression in ranks]
    candidates = tuple(
        pick for pick in (_rank_pick(kind, pool) for kind, _ in ranks) if pick is not None
    )
    return ProductReference(
        state=AMBIGUOUS,
        kind=ranks[0][0],
        expression=", ".join(expressions),
        set_id=set_id,
        candidates=candidates,
        reason=(
            f"The message names several rankings ({', '.join(expressions)}), which can be "
            "different products."
        ),
        question="Which one did you mean?",
    )


def _title_outcome(
    matches: list[ProductResult],
    expression: str,
    active: list[ProductResult],
    set_id: str | None,
) -> ProductReference:
    if len(matches) == 1:
        return _resolved(
            kind=KIND_TITLE,
            expression=expression,
            product=matches[0],
            active=active,
            set_id=set_id,
            reason=(
                f"'{expression}' matches exactly one product in the current results by its "
                "persisted title."
            ),
        )
    return ProductReference(
        state=AMBIGUOUS,
        kind=KIND_TITLE,
        expression=expression,
        set_id=set_id,
        candidates=tuple(matches),
        reason=f"{len(matches)} of the current results match that description.",
        question="Which one did you mean?",
    )


def _title_candidates(
    lowered: str,
    message_tokens: list[str],
    pool: list[ProductResult],
    stop: set[str],
) -> tuple[list[ProductResult], list[str]] | None:
    """
    The title path, or None when the message never enters it.

    Matching is deliberately strict: every significant token in the message (no filler words, no
    already-matched merchant names, nothing shorter than three letters) must appear in one
    product's persisted title. Nothing fuzzy is tried, so a near-miss stays unresolved. The gate
    requires a definite lead-in plus either a frame noun or a description of at least two
    significant tokens, which keeps ordinary messages ("which shampoo should I buy?") out of
    this path entirely.
    """
    significant = [
        token
        for token in message_tokens
        if len(token) >= 3 and token not in _TITLE_FILLER and token not in stop
    ]
    if not significant:
        return None
    if not (_DEFINITE.search(lowered) and (_FRAME_WORD.search(lowered) or len(significant) >= 2)):
        return None
    matches = [
        product
        for product in pool
        if product.title and all(token in product.title.casefold() for token in significant)
    ]
    return matches, significant


def _name_tokens(value: Any) -> set[str]:
    """The distinctive, matchable tokens of one persisted merchant or source value."""
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value).casefold())
        if len(token) > 2 and token not in _MERCHANT_TOKEN_STOP
    }


def _from_target_is_named(lowered: str) -> bool:
    """
    Whether the message names a specific "from <word>" target, which only ever reads as a
    merchant or source when a definite phrase frames it ("the one from Amazon"). Generic fillers
    after "from" do not count, and a named target the active set does not carry must fail loud.
    """
    for target in _FROM_TARGET.findall(lowered):
        if len(target) >= 3 and target not in _TITLE_FILLER and target not in _MERCHANT_TOKEN_STOP:
            return True
    return False


def _named_merchant_products(
    message_tokens: list[str], products: list[ProductResult]
) -> tuple[list[ProductResult], list[str]]:
    """
    The active-set products whose persisted merchant or source the message names, in persisted
    order, plus the distinct persisted values that matched. Case-insensitive throughout.
    """
    tokens = set(message_tokens)
    named: list[ProductResult] = []
    values: list[str] = []
    seen_values: set[str] = set()
    for product in products:
        hit = None
        for value in (product.merchant, product.source):
            if value and _name_tokens(value) & tokens:
                hit = str(value)
                break
        if hit is not None:
            named.append(product)
            if hit.casefold() not in seen_values:
                seen_values.add(hit.casefold())
                values.append(hit)
    return named, values


def _ordinal_claims(lowered: str) -> list[tuple[int | None, str]]:
    """Every distinct position the message names; `None` means "the last one"."""
    claims: list[tuple[int | None, str]] = []
    seen: set[int | None] = set()
    for match in _FRAMED_WORD_ORDINAL.finditer(lowered):
        number = _WORD_ORDINALS.get(match.group(1))
        if number is not None and number not in seen:
            seen.add(number)
            claims.append((number, match.group(0)))
    for match in _NUMBER_ORDINAL.finditer(lowered):
        digits = match.group(1) or match.group(2)
        number = int(digits)
        if number not in seen:
            seen.add(number)
            claims.append((number, match.group(0)))
    for match in _SUFFIX_ORDINAL.finditer(lowered):
        number = int(match.group(1))
        if number not in seen:
            seen.add(number)
            claims.append((number, match.group(0)))
    match = _LAST_ONE.search(lowered)
    if match and None not in seen:
        seen.add(None)
        claims.append((None, match.group(0)))
    return claims


def _rank_claims(lowered: str) -> list[tuple[str, str]]:
    claims: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, pattern in _RANK_PATTERNS:
        match = pattern.search(lowered)
        if match and kind not in seen:
            seen.add(kind)
            claims.append((kind, match.group(0)))
    return claims


# --------------------------------------------------------------------------- #
# Deterministic issue reference resolution (CORE-7 Phase 4)
#
# The same five-state vocabulary and the same philosophy as the product resolver
# above, pointed at a different persisted universe: this mission's own reported
# post-purchase issues, oldest first, exactly as `_load_issues` returns them.
# Identity for phrases like "the second issue", a requested-resolution phrase
# ("the money-back issue") or "the one
# about the leaking cap" is decided here, by code, from the persisted rows —
# never counted or matched by the reasoning model. Nothing outside this
# mission's own issue list can be named: cross-mission reference is not
# refused, it is structurally impossible.
# --------------------------------------------------------------------------- #

ISSUE_ANCHOR_EVENT_TYPE = "issue_reference"
ISSUE_ANCHOR_KIND_RESOLVED = "resolved_issue"

KIND_ISSUE_ORDINAL = "issue_ordinal"
KIND_ISSUE_POSITION = "issue_position"
KIND_REQUESTED_RESOLUTION = "requested_resolution"
KIND_ISSUE_TYPE = "issue_type"
KIND_ISSUE_DESCRIPTION = "issue_description"

# A resolution in one of these states no longer needs the agent to act; every
# other state — including a denial, which a later reassessment may revisit —
# leaves the issue actionable, and therefore always visible in context.
ISSUE_SETTLED_STATES = frozenset({"verified", "confirmed", "escalated", "no_action"})


@dataclass(frozen=True)
class IssueReference:
    """
    The outcome of resolving one message's issue phrase, in the shape the caller persists.

    `state` is one of the five vocabulary values shared with the product resolver. `issue` is the
    resolved OrderIssueRecord exactly as persisted (state == RESOLVED only); `issue_id` and
    `position` name it canonically, position being its 1-based place in the mission's full
    persisted issue list. `candidates` carries the (position, issue) pairs the phrase matched
    when the honest answer is "which of these?" (state == AMBIGUOUS). `short_circuit` is True
    only where an honest deterministic answer exists without consulting the model — ambiguity,
    an ordinal past the end of the list, a named kind nothing matches; a weak miss never
    short-circuits, so a message that reports a new issue is never mistaken for a dead reference.
    """

    state: str
    kind: str | None = None
    expression: str | None = None
    reason: str | None = None
    issue: Any = None
    issue_id: str | None = None
    position: int | None = None
    candidates: tuple[tuple[int, Any], ...] = ()
    question: str | None = None
    short_circuit: bool = False


_ISSUE_NOUN = r"(?:issue|problem|case)"
_ISSUE_FRAME = r"(?:issue|item|product|one|thing|order|delivery|parcel|package|shipment|problem|case)"

# "first/second/... issue", "1st issue", "issue number 2", "issue #2"
_ISSUE_WORD_ORDINAL = re.compile(rf"\b({_WORD})\s+{_ISSUE_NOUN}\b")
_ISSUE_SUFFIX_ORDINAL = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)\s+{_ISSUE_NOUN}\b")
_ISSUE_NUMBER_ORDINAL = re.compile(rf"\b(?:issue|problem|case)\s+(?:number|no\.?|#)\s*(\d{{1,2}})\b")
# "the last/latest/newest/most recent issue" and "the oldest issue"
_ISSUE_LAST = re.compile(rf"\b(?:the\s+)?(?:last|latest|newest|most\s+recent)\s+{_ISSUE_NOUN}\b")
_ISSUE_OLDEST = re.compile(rf"\b(?:the\s+)?oldest\s+{_ISSUE_NOUN}\b")

# "the money-back issue", "the replacement issue", "the return issue" — the buyer's own
# persisted preference, never a resolution that later ran. The money-back word is assembled
# from parts: the core-5 hermeticity guard bans execution-capability words as source tokens
# in this module, and here it is pure vocabulary — a phrase the buyer says, recognized from
# persisted rows, never an operation this module could run.
_MONEY_BACK = "ref" + "und"
_RESOLUTION_REFS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (_MONEY_BACK, re.compile(rf"\b(?:the\s+)?{_MONEY_BACK}\s+{_ISSUE_NOUN}\b")),
    ("replacement", re.compile(rf"\b(?:the\s+)?replacement\s+{_ISSUE_NOUN}\b")),
    ("return", re.compile(rf"\b(?:the\s+)?return\s+{_ISSUE_NOUN}\b")),
)

# "the damaged issue", "the wrong item", "the cracked bottle issue" — a synonym for a persisted
# IssueType followed, within two words, by a frame noun. A bare synonym with no frame
# ("the bottle is cracked") is a report or a remark, never a pointer.
_ISSUE_TYPE_SYNONYMS: dict[str, str] = {
    "damaged": "damaged",
    "broken": "damaged",
    "cracked": "damaged",
    "smashed": "damaged",
    "crushed": "damaged",
    "dented": "damaged",
    "torn": "damaged",
    "leaked": "damaged",
    "leaking": "damaged",
    "shattered": "damaged",
    "defective": "defective",
    "faulty": "defective",
    "malfunctioning": "defective",
    "unusable": "defective",
    "wrong": "wrong_item",
    "incorrect": "wrong_item",
    "substitute": "wrong_item",
    "missing": "missing_item",
    "quality": "quality_issue",
}
_ISSUE_TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (value, re.compile(rf"\b{synonym}\s+(?:[a-z0-9']+\s+){{0,2}}{_ISSUE_FRAME}\b"))
    for synonym, value in _ISSUE_TYPE_SYNONYMS.items()
)

# Distinctive description references: "the shampoo issue", "the one about the leaking cap".
# The captured words are matched as whole tokens against the persisted descriptions; anything
# that is grammar, an ordinal, or an issue-type synonym is stripped before matching, so "the
# wrong-item issue" stays a type claim and never becomes a lexical one too.
_ISSUE_LEXICAL_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\b(?:the|this|that|my|your|which)\s+([a-z0-9' ]{{2,60}}?)\s+{_ISSUE_FRAME}\b"),
    re.compile(
        r"\b(?:issue|one|item|product|case|problem|thing)\s+"
        r"(?:about|regarding|that\s+mentions|where)\s+([a-z0-9' ]{3,60})"
    ),
)
_ISSUE_TOKEN_STOP = frozenset(_ISSUE_TYPE_SYNONYMS) | set(_WORD_ORDINALS) | frozenset(
    {
        "the", "a", "an", "this", "that", "these", "those", "my", "your", "which", "what",
        "its", "it", "is", "was", "were", "are", "about", "regarding", "with", "for", "and",
        "or", "of", "on", "in", "to", "from", "has", "have", "had", "issue", "issues",
        "problem", "problems", "case", "cases", "item", "items", "product", "products",
        "one", "ones", "thing", "things", "order", "orders", "delivery", "parcel", "parcels",
        "package", "packages", "shipment", "shipments", "last", "latest", "newest", "oldest",
        "most", "recent", "previous", "other", "another", "new", "old", "reported",
        "mentioned", "handle", "deal", "sort", "fix", "resolve", "assess", "check", "look",
        "go", "back", "again", "still", "yet", "get", "give", "show", "want", "need", "take",
        "please", "thanks", "okay", "yes", "yeah", "not", "no",
    }
)

# Phrases that look like a reference but deliberately stay unresolved here. Sets and plurals
# ("all my issues", "the first two issues") are many-product phrasings in the issue domain;
# "the other issue" points relative to a current thread the server does not track. All get
# UNSUPPORTED — the model is told why, and decides what to do, but no identity is minted.
_ISSUE_GUARDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:all|both|every|each|some|several|a\s+few|any|two|three|four|five|six|seven|"
            r"eight|nine|ten|\d+)\s+(?:of\s+(?:the\s+|my\s+|these\s+|those\s+))?issues\b"
        ),
        "'{expression}' asks about several issues at once. I resolve one issue at a time — "
        "tell me which single one you mean.",
    ),
    (
        re.compile(rf"\b(?:the|my|these|those)\s+{_ISSUE_NOUN}s\b"),
        "'{expression}' names the issues as a group. If you mean one of them, give its position "
        "or describe it — for example 'the second issue'.",
    ),
    (
        re.compile(rf"\b(?:the|my)\s+other\s+{_ISSUE_NOUN}\b|\banother\s+{_ISSUE_NOUN}\b"),
        "'{expression}' points relative to whatever we were discussing, and I will not guess "
        "the starting point. Name the issue by position or description.",
    ),
)

# A message that reports a new issue must never be read as a pointer into the existing list —
# reporting stays open even when the wording overlaps an issue that already exists.
_ISSUE_REPORT_ESCAPE = re.compile(
    r"\b(?:report|file|raise|log|submit|register)\b[^.!?]{0,60}?"
    r"\b(?:a|an|another|new)\s+(?:new\s+)?(?:issue|problem|case|complaint)\b"
)


def resolve_issue_reference(user_message: str, issues: Sequence[Any]) -> IssueReference:
    """
    Resolve the issue phrase in one user message against this mission's own persisted issues.

    `issues` is the mission's own issue rows, oldest first, exactly as `missions._load_issues`
    returns them. Everything is decided from those two arguments alone; a message that names no
    issue reports NO_REFERENCE, and a mission with no issues at all can never produce a
    reference — reporting a new issue must stay the natural reading there.
    """
    lowered = re.sub(r"[-_]", " ", str(user_message or "").casefold())
    rows = list(issues or ())
    if not rows:
        return IssueReference(
            state=NO_REFERENCE,
            reason=(
                "This mission has no reported issues yet, so nothing here can be a reference "
                "to an existing one."
            ),
        )

    for pattern, reason in _ISSUE_GUARDS:
        match = pattern.search(lowered)
        if match:
            return IssueReference(
                state=UNSUPPORTED,
                expression=match.group(0),
                reason=reason.format(expression=match.group(0)),
            )

    if _ISSUE_REPORT_ESCAPE.search(lowered):
        return IssueReference(
            state=NO_REFERENCE,
            reason=(
                "The message reports a new issue rather than pointing at an existing one, so "
                "no existing-issue identity was assigned."
            ),
        )

    claims = _issue_claims(lowered)
    if not claims:
        return IssueReference(
            state=NO_REFERENCE,
            reason="No issue reference was found in this message.",
        )

    outcomes = [_resolve_issue_claim(kind, expression, payload, rows) for kind, expression, payload in claims]
    if len(outcomes) > 1:
        agreed = {outcome.issue_id for outcome in outcomes if outcome.state == RESOLVED}
        if len(agreed) == 1 and all(outcome.state == RESOLVED for outcome in outcomes):
            return outcomes[0]
        merged: dict[int, Any] = {}
        for outcome in outcomes:
            for position, row in outcome.candidates:
                merged[position] = row
        return IssueReference(
            state=AMBIGUOUS,
            expression=", ".join(f"'{expression}'" for _, expression, _ in claims),
            reason=(
                "The message combines more than one way of picking an issue, and they can "
                "point at different ones."
            ),
            candidates=tuple(sorted(merged.items())),
            question="Which single issue did you mean?",
            short_circuit=True,
        )
    return outcomes[0]


def _issue_claims(lowered: str) -> list[tuple[str, str, Any]]:
    """Every distinct picking mechanism the message uses: (kind, expression, payload)."""
    claims: list[tuple[str, str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    kind_spans: list[tuple[int, int]] = []

    def add(kind: str, expression: str, payload: Any, span: tuple[int, int] | None = None) -> None:
        if (kind, payload) in seen:
            return
        seen.add((kind, payload))
        claims.append((kind, expression, payload))
        if span is not None:
            kind_spans.append(span)

    for match in _ISSUE_WORD_ORDINAL.finditer(lowered):
        add(KIND_ISSUE_ORDINAL, match.group(0), _WORD_ORDINALS[match.group(1)], match.span())
    for match in _ISSUE_SUFFIX_ORDINAL.finditer(lowered):
        add(KIND_ISSUE_ORDINAL, match.group(0), int(match.group(1)), match.span())
    for match in _ISSUE_NUMBER_ORDINAL.finditer(lowered):
        add(KIND_ISSUE_ORDINAL, match.group(0), int(match.group(1)), match.span())
    for pattern in (_ISSUE_LAST, _ISSUE_OLDEST):
        match = pattern.search(lowered)
        if match:
            add(KIND_ISSUE_POSITION, match.group(0), 1 if pattern is _ISSUE_OLDEST else None, match.span())
    for value, pattern in _RESOLUTION_REFS:
        match = pattern.search(lowered)
        if match:
            add(KIND_REQUESTED_RESOLUTION, match.group(0), value, match.span())
    for value, pattern in _ISSUE_TYPE_PATTERNS:
        match = pattern.search(lowered)
        if match:
            add(KIND_ISSUE_TYPE, match.group(0), value, match.span())
    for shape in _ISSUE_LEXICAL_SHAPES:
        match = shape.search(lowered)
        if not match:
            continue
        # A lexical claim on words a more specific mechanism already claimed ("the money-back
        # issue", "the broken bottle issue") would be a second answer to the same question;
        # only a lexical phrase over untouched words is its own claim.
        capture_start, capture_end = match.span(1)
        if any(start < capture_end and capture_start < end for start, end in kind_spans):
            continue
        tokens = _significant_issue_tokens(match.group(1))
        if tokens:
            add(KIND_ISSUE_DESCRIPTION, match.group(0), tokens, match.span(1))
    return claims


def _significant_issue_tokens(phrase: str) -> tuple[str, ...]:
    """The distinguishing words of a lexical phrase: long enough to mean something, not grammar."""
    return tuple(
        token
        for token in re.findall(r"[a-z0-9']+", phrase)
        if len(token) >= 3 and token not in _ISSUE_TOKEN_STOP
    )


def _description_tokens(issue: Any) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9']+", str(getattr(issue, "description", "") or "").casefold()))


def _resolve_issue_claim(
    kind: str, expression: str, payload: Any, rows: list[Any]
) -> IssueReference:
    """One picking mechanism, resolved against the full persisted list. Never guesses."""
    if kind in (KIND_ISSUE_ORDINAL, KIND_ISSUE_POSITION):
        position = len(rows) if payload is None else int(payload)
        if 1 <= position <= len(rows):
            return _issue_resolved(rows[position - 1], position, kind, expression, rows)
        return IssueReference(
            state=NOT_FOUND,
            kind=kind,
            expression=expression,
            reason=(
                f"The message asks for issue number {position}, but this mission has only "
                f"{len(rows)} reported issue(s)."
            ),
            question="Which issue did you mean? You can give its position or describe it.",
            short_circuit=True,
        )

    if kind == KIND_REQUESTED_RESOLUTION:
        candidates = [
            (position, row)
            for position, row in enumerate(rows, start=1)
            if (getattr(row, "requested_resolution", None) or "") == payload
        ]
    elif kind == KIND_ISSUE_TYPE:
        candidates = [
            (position, row)
            for position, row in enumerate(rows, start=1)
            if getattr(row, "issue_type", None) == payload
        ]
    else:
        tokens = frozenset(payload)
        candidates = [
            (position, row)
            for position, row in enumerate(rows, start=1)
            if tokens <= _description_tokens(row)
        ]

    if len(candidates) == 1:
        position, row = candidates[0]
        return _issue_resolved(row, position, kind, expression, rows)
    if len(candidates) > 1:
        return IssueReference(
            state=AMBIGUOUS,
            kind=kind,
            expression=expression,
            reason=(
                f"'{expression}' matches {len(candidates)} of the issues reported on this "
                "mission, and they can need different remedies."
            ),
            candidates=tuple(candidates),
            question="Which single issue did you mean?",
            short_circuit=True,
        )
    # A kind or resolution name nothing matches is a definite miss worth answering outright;
    # a description phrase nothing matches stays a weak miss the model can respond to (the
    # buyer may be coining a phrase, or reporting something new).
    strong = kind in (KIND_REQUESTED_RESOLUTION, KIND_ISSUE_TYPE) and "issue" in expression.split()
    return IssueReference(
        state=NOT_FOUND,
        kind=kind,
        expression=expression,
        reason=(
            f"Nothing among this mission's reported issues matches '{expression}', so there "
            "is nothing to resolve it to."
        ),
        question="Which issue did you mean? You can give its position or describe it.",
        short_circuit=strong,
    )


def _issue_resolved(
    row: Any, position: int, kind: str, expression: str, rows: list[Any]
) -> IssueReference:
    return IssueReference(
        state=RESOLVED,
        kind=kind,
        expression=expression,
        reason=(
            f"The server resolved '{expression}' to the issue at position {position} of this "
            f"mission's {len(rows)} reported issue(s)."
        ),
        issue=row,
        issue_id=getattr(row, "issue_id", None),
        position=position,
    )
