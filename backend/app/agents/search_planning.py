"""
Deterministic search planning and result-set reuse (CORE-5 Phase 4).

When a buyer refines what they want after a search has already answered them, the question
"search again, or filter what we already have?" is answered here — by rules over verified
facts, never by a model and never by a guess. The planner receives only the mission's own
structured intent and the result sets this mission has already been shown, and decides:

* **reuse** — the changed constraint can be evaluated against facts the current results
  already carry (a verified price, a verified merchant, a requested count), so the set is
  filtered locally and the products, their identities and their URLs are preserved exactly.
* **new_search** — the change concerns something the results do not verify (a colour, a
  material, a category, a raised budget) or nothing verifies it against, so a real provider
  search must run. When uncertain, the answer is always a new search; this module never
  guesses.

The boundary this module holds is the one that makes the rest of the system honest: it can
say "these are the same discovered products filtered to your new constraint" because a
filter is all that happened, and it refuses to say a product matches a constraint when the
fact the constraint needs is unknown — an unknown price never satisfies a price filter, an
unknown merchant never satisfies a brand or exclusion. It performs no I/O of any kind: no
HTTP, no credentials, no database, no provider call. It only computes the decision and, for
reuse, the derived set; the actual provider execution stays in the discovery flow where it
has always been.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agents.constraints import CONSTRAINT_FIELDS
from app.models.mission import ResultSetRecord, ShoppingIntent
from app.models.product import ProductResult

# The result-set history is bounded so an ordinary conversation cannot grow a mission's
# persisted state without limit; the newest set is always the active one and the oldest
# entries are the ones that give way.
MAX_RESULT_SETS = 10

REUSE = "reuse"
NEW_SEARCH = "new_search"

# Fixed evaluation order over the closed constraint registry, so a turn that changed several
# fields at once always reports the same first reason.
_EVALUATION_ORDER: tuple[str, ...] = tuple(sorted(CONSTRAINT_FIELDS))


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SearchPlan:
    """What the planner decided, and exactly why — carried into the record and the answer."""

    action: str
    reason: str
    filters: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    source_set_id: str | None = None


def plan_search(intent: ShoppingIntent) -> SearchPlan:
    """
    Decide reuse-versus-new-search for this mission's current state, deterministically.

    The current constraint view is compared field-by-field against the snapshot recorded with
    the active result set — the constraints that set was actually produced under. A changed
    field either yields local filters (when the change can be evaluated against verified facts
    on every result) or forces a new search (when it cannot). A mission with no usable result
    set always gets a real search, because there is nothing to reuse.
    """
    _ensure_active_record(intent)
    active = _active_record(intent)
    if active is None:
        return SearchPlan(action=NEW_SEARCH, reason=_NO_SET_REASON)
    if not active.products:
        return SearchPlan(action=NEW_SEARCH, reason=_EMPTY_SET_REASON)

    view = _constraint_view(intent)
    filters: list[dict[str, Any]] = []
    for name in _EVALUATION_ORDER:
        current = _coerce_value(name, view[name])
        previous = _coerce_value(name, active.constraint_snapshot.get(name))
        if _same(name, current, previous):
            continue
        outcome = _evaluate_change(name, current, previous, active.products, intent.currency)
        if isinstance(outcome, str):
            return SearchPlan(action=NEW_SEARCH, reason=outcome, source_set_id=active.set_id)
        filters.extend(outcome)

    if filters:
        kept = apply_filters(active.products, filters)
        if not kept:
            return SearchPlan(
                action=NEW_SEARCH, reason=_DRY_RUN_EMPTY_REASON, source_set_id=active.set_id
            )
        return SearchPlan(
            action=REUSE,
            reason=_REUSE_FILTERED_REASON,
            filters=tuple(filters),
            source_set_id=active.set_id,
        )
    return SearchPlan(action=REUSE, reason=_UNCHANGED_REASON, source_set_id=active.set_id)


# Reasons are fixed strings: the same state always produces the same words.
_NO_SET_REASON = "no result set exists yet, so a real provider search is required"
_EMPTY_SET_REASON = "the current result set holds no products, so a real provider search is required"
_UNCHANGED_REASON = "the structured constraints have not changed since these results were found"
_REUSE_FILTERED_REASON = "the changed constraint can be applied to facts already verified on these results"
_DRY_RUN_EMPTY_REASON = (
    "applying the new constraint to the verified facts of the current results would leave none "
    "of them, so a fresh search is required"
)
_BUDGET_RAISED_REASON = (
    "the price ceiling was raised, and these results were found under a narrower one, so a fresh "
    "search is required"
)
_BUDGET_UNVERIFIED_PRICE_REASON = (
    "a price filter cannot be applied honestly because the price of some discovered results is "
    "not a verified fact"
)
_BUDGET_CURRENCY_REASON = (
    "the discovered results are not all priced in the mission's currency, so a price filter "
    "cannot be applied"
)
_QUANTITY_TOO_FEW_REASON = "the current result set holds fewer products than the user asked to see"
_QUANTITY_REMOVED_REASON = (
    "the earlier requested count was lifted, and a filtered view cannot show products it does "
    "not hold"
)
_BRAND_NO_MERCHANT_REASON = (
    "the merchant behind some discovered results is unknown, so the brand cannot be verified "
    "against them"
)
_BRAND_NO_MATCH_REASON = "no discovered result carries the requested brand as its verified merchant"
_BRAND_CLEARED_REASON = (
    "the brand constraint was lifted, and these results were found for the previous one"
)
_EXCLUSION_MERCHANT_UNKNOWN_REASON = (
    "the exclusion cannot be evaluated because the merchant behind some discovered results is "
    "unknown"
)
_EXCLUSION_NOT_EVALUABLE_REASON = (
    "the exclusion does not correspond to any verified fact on these results, so they cannot be "
    "filtered by it"
)
_EXCLUSION_REMOVED_REASON = (
    "a previously excluded constraint was lifted, and the filtered view cannot restore what it hid"
)
_CONCEPT_CHANGE_REASONS: dict[str, str] = {
    "category": "the product category changed, and category is not a verified fact carried by these results",
    "color": "the color constraint changed, and color is not a verified fact on these results",
    "material": "the material constraint changed, and material is not a verified fact on these results",
    "gender": "the gender line changed, and it is not a verified fact on these results",
    "intended_use": "the intended use changed, and it is not a verified fact on these results",
    "size_or_variant": "the size or variant changed, and these results were found for the previous one",
    "preferences": "the stated preferences changed, and these results were found without them",
}


def _evaluate_change(
    name: str,
    current: Any,
    previous: Any,
    products: list[ProductResult],
    currency: str,
) -> str | list[dict[str, Any]]:
    """
    One changed constraint field: the local filters that evaluate it, or the reason a new
    search is forced. The filters below are the only ones the planner ever synthesizes; the
    unverifiable fields have no filter to yield by design.
    """
    if name == "budget":
        if previous is not None and (current is None or current > previous):
            return _BUDGET_RAISED_REASON
        if any(product.extracted_price is None for product in products):
            return _BUDGET_UNVERIFIED_PRICE_REASON
        wanted = str(currency).strip().casefold()
        if any(str(product.currency or "").strip().casefold() != wanted for product in products):
            return _BUDGET_CURRENCY_REASON
        return [{"kind": "max_price", "value": current}]

    if name == "quantity":
        if current is None:
            return _QUANTITY_REMOVED_REASON
        if len(products) < current:
            return _QUANTITY_TOO_FEW_REASON
        return [{"kind": "limit", "value": current}]

    if name == "brand":
        if current is None:
            return _BRAND_CLEARED_REASON
        if any(_missing_text(product.merchant) for product in products):
            return _BRAND_NO_MERCHANT_REASON
        if not any(_name_match(product.merchant, current) for product in products):
            return _BRAND_NO_MATCH_REASON
        return [{"kind": "merchant_is", "value": current}]

    if name == "exclusions":
        added = _added_exclusions(current, previous)
        if _removed_exclusions(current, previous):
            return _EXCLUSION_REMOVED_REASON
        if any(_missing_text(product.merchant) for product in products):
            return _EXCLUSION_MERCHANT_UNKNOWN_REASON
        for value in added:
            if not any(_name_match(product.merchant, value) for product in products):
                return _EXCLUSION_NOT_EVALUABLE_REASON
        return [{"kind": "merchant_not", "value": value} for value in added]

    return _CONCEPT_CHANGE_REASONS[name]


# --------------------------------------------------------------------------- #
# Local filtering — the only place a reuse ever derives its set
# --------------------------------------------------------------------------- #


def apply_filters(products: list[ProductResult], specs: list[dict[str, Any]]) -> list[ProductResult]:
    """
    Filter with the facts the results themselves carry, never mutating a result.

    A missing fact never satisfies a filter: a result with no verified price is dropped by a
    price filter, one with no merchant is dropped by a brand filter and by an exclusion, one
    with no rating or review count is dropped by those thresholds. The returned list holds the
    original, untouched ProductResult objects in their original order; a limit is applied last,
    as a slice.
    """
    kept = list(products)
    limits: list[int] = []
    for spec in specs:
        kind = spec.get("kind")
        value = spec.get("value")
        if kind == "max_price":
            bound = _as_decimal(value)
            if bound is not None:
                kept = [
                    p
                    for p in kept
                    if p.extracted_price is not None and p.extracted_price <= bound
                ]
        elif kind == "min_price":
            bound = _as_decimal(value)
            if bound is not None:
                kept = [
                    p
                    for p in kept
                    if p.extracted_price is not None and p.extracted_price >= bound
                ]
        elif kind == "min_rating":
            bound = _as_decimal(value)
            if bound is not None:
                kept = [
                    p for p in kept if p.rating is not None and Decimal(str(p.rating)) >= bound
                ]
        elif kind == "min_reviews":
            count = _as_int(value)
            if count is not None:
                kept = [p for p in kept if p.review_count is not None and p.review_count >= count]
        elif kind == "merchant_is":
            wanted = _text(value)
            kept = [p for p in kept if not _missing_text(p.merchant) and _name_match(p.merchant, wanted)]
        elif kind == "merchant_not":
            excluded = _text(value)
            kept = [
                p
                for p in kept
                if not _missing_text(p.merchant) and not _name_match(p.merchant, excluded)
            ]
        elif kind == "limit":
            count = _as_int(value)
            if count is not None:
                limits.append(count)
        else:
            raise ValueError(f"unknown filter kind: {kind!r}")
    if limits:
        kept = kept[: min(limits)]
    return kept


def apply_reuse(intent: ShoppingIntent, plan: SearchPlan) -> ResultSetRecord:
    """
    Derive the reused set: filter the active one, record the derivation, make it current.

    The previous set is preserved untouched in the history; the derived entry is linked to it
    through `source_set_id` and snapshots the constraints it was filtered under, so the next
    turn compares against what this set actually represents.
    """
    source = _active_record(intent)
    if source is None:
        raise ValueError("apply_reuse requires an active result set; call plan_search first")
    filtered = apply_filters(source.products, list(plan.filters))
    record = ResultSetRecord(
        origin="filtered",
        source_set_id=source.set_id,
        constraint_snapshot=_constraint_view(intent),
        products=filtered,
        note=plan.reason,
    )
    _append_record(intent, record)
    return record


def reuse_message(record: ResultSetRecord, source: ResultSetRecord) -> str:
    """
    What the user is told about a reuse. It names the filtering for what it is — no provider
    was called, nothing new was discovered, and every product shown came from this mission's
    own earlier result set.
    """
    kept = len(record.products)
    total = len(source.products)
    if kept == total:
        return (
            f"These are the same discovered products as before — all {kept} of them already "
            "satisfy your request, so no new search was run."
        )
    return (
        f"These are the same {total} discovered products as before, filtered to your new "
        f"constraint using only the facts already verified on them: {kept} match. No new "
        "search was run."
    )


# --------------------------------------------------------------------------- #
# Query construction for a real search
# --------------------------------------------------------------------------- #


def query_from_constraints(intent: ShoppingIntent, fallback: str | None = None) -> str | None:
    """
    The query a NEW search sends, built from the canonical structured constraints only.

    The historical raw prompt is the last resort, never the input: a follow-up message like
    "make it under 700" must not become a search for the words "make it under 700". The
    caller's suggested query is used only when the constraints alone say nothing.
    """
    parts: list[str] = [
        intent.brand,
        intent.category,
        intent.color,
        intent.material,
        intent.gender,
        intent.intended_use,
        *(intent.preferences or []),
        intent.size_or_variant,
    ]
    text = " ".join(str(part).strip() for part in parts if part and str(part).strip()).strip()
    if text:
        return text[:200]
    if fallback and fallback.strip():
        return fallback.strip()[:200]
    raw = (intent.raw_prompt or "").strip()
    return raw[:200] if raw else None


# --------------------------------------------------------------------------- #
# Result-set history
# --------------------------------------------------------------------------- #


def record_search_set(intent: ShoppingIntent, products: list[ProductResult], query: str | None) -> ResultSetRecord:
    """
    Record a real provider search as a new result set and make it current.

    A search never destroys what came before it: the previous set stays in the history under
    the constraints it was found with. This runs only after a successful provider call — a
    failed search records nothing, so the mission's last good set remains its current one.
    """
    _ensure_active_record(intent)
    record = ResultSetRecord(
        origin="search",
        query=query,
        constraint_snapshot=_constraint_view(intent),
        products=list(products),
        note="A real provider search produced this result set.",
    )
    _append_record(intent, record)
    return record


def _ensure_active_record(intent: ShoppingIntent) -> None:
    """
    Give a mission whose sets predate this history a record of what it already has.

    Discovered products that were found before result sets were recorded are synthesized as
    one `imported` entry with an empty constraint snapshot: the constraints that search ran
    under are simply not known, and an empty snapshot makes every constraint the mission now
    carries get re-evaluated against the set rather than silently assumed satisfied.
    """
    if intent.result_sets or not intent.discovered_products:
        return
    imported = ResultSetRecord(
        origin="imported",
        query=intent.discovery_query,
        constraint_snapshot={},
        products=list(intent.discovered_products),
        note=(
            "Recorded from mission state: this result set was found before provenance was kept, "
            "so the constraints it was searched under are unknown."
        ),
    )
    intent.result_sets.append(imported)
    intent.active_result_set_id = imported.set_id


def _append_record(intent: ShoppingIntent, record: ResultSetRecord) -> None:
    intent.result_sets.append(record)
    overflow = len(intent.result_sets) - MAX_RESULT_SETS
    if overflow > 0:
        del intent.result_sets[:overflow]
    intent.active_result_set_id = record.set_id
    intent.discovered_products = list(record.products)


def _active_record(intent: ShoppingIntent) -> ResultSetRecord | None:
    if intent.result_sets:
        for record in reversed(intent.result_sets):
            if record.set_id == intent.active_result_set_id:
                return record
        return intent.result_sets[-1]
    return None


# --------------------------------------------------------------------------- #
# Constraint comparison against the closed registry
# --------------------------------------------------------------------------- #


def _constraint_view(intent: ShoppingIntent) -> dict[str, Any]:
    """Exactly the registry fields, nothing else — the facts a set was produced under."""
    view = {name: getattr(intent, name) for name in _EVALUATION_ORDER}
    # JSON-safe by construction: this dict is persisted inside the result-set record, and a
    # Decimal has no place in a JSON column. Readers coerce it back through Decimal(str(...)).
    if isinstance(view.get("budget"), Decimal):
        view["budget"] = str(view["budget"])
    return view


def _coerce_value(name: str, raw: Any) -> Any:
    """
    One constraint value — from the live intent or read back from a persisted snapshot —
    coerced to the shape comparisons and filters use. Both sides of every comparison go
    through this, so a value's storage form never changes what the planner decides.
    """
    if name == "budget":
        if raw is None or raw == "":
            return None
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
    if name == "quantity":
        return _as_int(raw)
    if name in ("exclusions", "preferences"):
        if isinstance(raw, list):
            return [str(item) for item in raw]
        return []
    if raw is None:
        return None
    return str(raw)


def _same(name: str, current: Any, previous: Any) -> bool:
    if name == "budget":
        return current == previous  # both Decimal | None
    if name == "quantity":
        return current == previous
    if name in ("exclusions", "preferences"):
        return _casefold_set(current) == _casefold_set(previous)
    return _casefold_text(current) == _casefold_text(previous)


def _added_exclusions(current: Any, previous: Any) -> list[str]:
    prev_keys = _casefold_set(previous)
    return [value for value in (current or []) if str(value).strip().casefold() not in prev_keys]


def _removed_exclusions(current: Any, previous: Any) -> bool:
    cur_keys = _casefold_set(current)
    return any(str(value).strip().casefold() not in cur_keys for value in (previous or []))


def _casefold_set(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {str(value).strip().casefold() for value in values if str(value).strip()}


def _casefold_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.casefold() if text else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _missing_text(value: Any) -> bool:
    return _text(value) == ""


def _name_match(verified: Any, claimed: Any) -> bool:
    """
    Substring match in either direction, case-insensitively — e.g. merchant "Nike Official"
    against brand "nike", or merchant "Amazon.in" against an exclusion of "amazon". Either
    side empty is never a match.
    """
    left = _casefold_text(verified)
    right = _casefold_text(claimed)
    if not left or not right:
        return False
    return left in right or right in left


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
