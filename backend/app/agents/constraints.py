"""
Deterministic constraint-state transitions for a shopping mission's intent (CORE-5 Phase 3).

The division of labor this module exists to enforce:

* **The reasoning provider interprets.** It reads the user's latest message in the context of
  the current canonical intent and returns a structured proposal: which constraint changes to
  make, or that the message is too ambiguous to interpret.
* **The server transitions state.** Everything in this module is closed and deterministic: a
  fixed registry of mergeable fields, a fixed operation vocabulary, and validation that drops
  anything outside both. No natural-language phrase is ever matched here.

A proposal the model cannot confidently make never mutates state: `ambiguous` applies zero
updates and carries the clarification question back to the user. A malformed entry is dropped,
never raised. Constraint values are data — product attributes a user named — and nothing in
this module gives them a destination beyond the intent's own fields.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

# The complete set of fields a constraint update may address. Anything else in a model
# proposal — however it is named — has no destination and is dropped.
CONSTRAINT_FIELDS = frozenset(
    {
        "category",
        "budget",
        "brand",
        "color",
        "material",
        "gender",
        "intended_use",
        "size_or_variant",
        "quantity",
        "exclusions",
        "preferences",
    }
)

LIST_FIELDS = frozenset({"exclusions", "preferences"})
SCALAR_FIELDS = CONSTRAINT_FIELDS - LIST_FIELDS

# Concepts with a dedicated structured field. A proposal that sets one of these must not
# also duplicate the same value into `preferences`.
STRUCTURED_CONCEPT_FIELDS = frozenset({"color", "material", "gender", "intended_use"})

_OPS = frozenset({"set", "clear", "add", "remove"})

_MAX_SCALAR_CHARS = 200
_MAX_LIST_ITEM_CHARS = 120
_MAX_LIST_ITEMS = 20

# The intent keys that constitute constraint state. Currency is deliberately absent — every
# intent carries the INR default, so it can never signal that anything was interpreted. A
# mission whose intent carries none of these keys has no interpreted constraints yet (for
# example one created during a reasoning outage), so the next interpretation is read as the
# initial state rather than merged.
_CONSTRAINT_STATE_KEYS = tuple(sorted(CONSTRAINT_FIELDS))


def has_constraint_state(intent_data: dict[str, Any]) -> bool:
    """Whether this persisted intent already holds any interpreted constraint at all."""
    return any(intent_data.get(key) not in (None, [], "") for key in _CONSTRAINT_STATE_KEYS)


def _clean_scalar(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:_MAX_SCALAR_CHARS]


def _clean_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items: list[str] = []
    for entry in value:
        if not isinstance(entry, (str, int, float)):
            return None
        text = str(entry).strip()[:_MAX_LIST_ITEM_CHARS]
        if text and not any(item.casefold() == text.casefold() for item in items):
            items.append(text)
    return items[:_MAX_LIST_ITEMS] if items else []


def normalize_update(raw: Any) -> dict[str, Any] | None:
    """
    One proposed update, validated against the closed registry — or None.

    Anything outside the registry (an unknown field, an unknown op, a wrong-typed value, an
    add/remove aimed at a scalar field) is dropped rather than repaired: a partial guess at
    what the model meant would be a state transition the user did not ask for.
    """
    if not isinstance(raw, dict):
        return None
    field = raw.get("field")
    op = raw.get("op")
    if field not in CONSTRAINT_FIELDS or op not in _OPS:
        return None

    if op == "clear":
        return {"field": field, "op": "clear"}

    value = raw.get("value")

    if op in ("add", "remove"):
        if field not in LIST_FIELDS:
            return None
        text = _clean_scalar(value) if isinstance(value, str) else None
        if text is None:
            return None
        return {"field": field, "op": op, "value": text[:_MAX_LIST_ITEM_CHARS]}

    # op == "set"
    if field == "quantity":
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 1:
            return {"field": field, "op": "set", "value": value}
        if isinstance(value, str) and value.strip().isdigit():
            number = int(value.strip())
            if number >= 1:
                return {"field": field, "op": "set", "value": number}
        return None
    if field == "budget":
        if isinstance(value, bool) or value is None:
            return None
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if amount < 0:
            return None
        return {"field": field, "op": "set", "value": amount}
    if field in LIST_FIELDS:
        cleaned = _clean_list(value)
        if cleaned is None:
            return None
        return {"field": field, "op": "set", "value": cleaned}
    text = _clean_scalar(value)
    if text is None:
        return None
    return {"field": field, "op": "set", "value": text}


def parse_proposal(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, str | None]:
    """
    The full update proposal a provider interpretation carries, validated as a whole.

    Returns the surviving updates, whether the model flagged the message ambiguous, and the
    clarification question to ask when it did. Ambiguity applies zero updates — that decision
    belongs to the caller. Malformed containers become empty, never errors.
    """
    raw_updates = parsed.get("constraint_updates")
    updates = []
    if isinstance(raw_updates, list):
        for entry in raw_updates:
            normalized = normalize_update(entry)
            if normalized is not None:
                updates.append(normalized)
    ambiguous = bool(parsed.get("ambiguous"))
    clarification = parsed.get("clarification") or parsed.get("clarification_prompt")
    clarification = clarification.strip()[:600] if isinstance(clarification, str) and clarification.strip() else None
    return _without_duplicated_concepts(updates), ambiguous, clarification


def _without_duplicated_concepts(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Enforce that a concept with a dedicated field is not duplicated into `preferences`.

    Within one proposal, a value being set on a structured concept field removes any equal —
    case-insensitively — preferences entry the same proposal tries to add or set. Existing
    persisted preferences are never touched here; this governs only what the proposal writes.
    """
    concept_values = {
        update["value"].casefold()
        for update in updates
        if update["field"] in STRUCTURED_CONCEPT_FIELDS
        and update["op"] == "set"
        and isinstance(update["value"], str)
    }
    if not concept_values:
        return updates

    surviving: list[dict[str, Any]] = []
    for update in updates:
        if update["field"] != "preferences":
            surviving.append(update)
            continue
        if update["op"] == "add":
            if update["value"].casefold() in concept_values:
                continue
            surviving.append(update)
        elif update["op"] == "set":
            remaining = [item for item in update["value"] if item.casefold() not in concept_values]
            if remaining:
                surviving.append({**update, "value": remaining})
        else:
            surviving.append(update)
    return surviving


def apply_updates(intent_data: dict[str, Any], updates: list[dict[str, Any]]) -> None:
    """
    Apply validated updates to an intent data dict, in place, deterministically.

    Scalar `set` replaces, `clear` writes None. List `set` replaces the whole list, `clear`
    empties it, `add` appends case-insensitively deduplicated, `remove` drops every
    case-insensitive match. Every entry was validated by `normalize_update` first.
    """
    for update in updates:
        field = update["field"]
        op = update["op"]
        if op == "clear":
            intent_data[field] = [] if field in LIST_FIELDS else None
        elif op == "set":
            intent_data[field] = update["value"]
        elif op == "add":
            values = list(intent_data.get(field) or [])
            if not any(str(item).casefold() == str(update["value"]).casefold() for item in values):
                values.append(update["value"])
            intent_data[field] = values
        elif op == "remove":
            intent_data[field] = [
                item for item in (intent_data.get(field) or []) if str(item).casefold() != str(update["value"]).casefold()
            ]


def compose_intent_turn_input(intent_data: dict[str, Any], user_input: str) -> str:
    """
    What the reasoning provider is asked to interpret on a follow-up turn.

    The provider sees the canonical current intent plus the user's new message — never the
    growing transcript of everything said before. A mission with no constraint state yet gets
    the plain message, so the interpretation reads as a first request.
    """
    if not has_constraint_state(intent_data):
        return user_input
    current: dict[str, Any] = {}
    for key in sorted(CONSTRAINT_FIELDS):
        value = intent_data.get(key)
        if value in (None, [], ""):
            continue
        current[key] = str(value) if isinstance(value, Decimal) else value
    envelope = {"current_intent": current, "user_message": user_input}
    return json.dumps(envelope, ensure_ascii=False)


def merge_intent_turn(
    intent_data: dict[str, Any], parsed: dict[str, Any], user_input: str
) -> dict[str, Any]:
    """
    One follow-up turn's deterministic state transition.

    The constraint_updates proposal is the only authoritative change set; the interpretation's
    plain fields describe the resulting intent but never overwrite it. The mission's own facts
    (products, selection, checkout, payment) are carried untouched — carry-forward re-checks
    them after this returns. `raw_prompt` stays the original first prompt; it never grows.
    """
    updates, ambiguous, clarification = parse_proposal(parsed)

    if not has_constraint_state(intent_data):
        # Nothing interpreted yet (a mission created during a reasoning outage): this
        # message's interpretation is the initial constraint state, exactly like creation.
        merged = dict(intent_data)
        for key in sorted(CONSTRAINT_FIELDS):
            if key in parsed:
                merged[key] = parsed[key]
        merged["raw_prompt"] = intent_data.get("raw_prompt") or user_input
        merged["missing_info"] = parsed.get("missing_info") or []
        merged["clarification_prompt"] = parsed.get("clarification_prompt")
        merged["state"] = parsed.get("state") or "shopping_intent"
        apply_updates(merged, updates)
        return merged

    if ambiguous:
        merged = dict(intent_data)
        merged["clarification_prompt"] = clarification
        merged["state"] = "needs_clarification"
        return merged

    merged = dict(intent_data)
    apply_updates(merged, updates)
    merged["raw_prompt"] = intent_data.get("raw_prompt") or user_input
    merged["missing_info"] = parsed.get("missing_info") or []
    merged["clarification_prompt"] = parsed.get("clarification_prompt")
    merged["state"] = parsed.get("state") or merged.get("state") or "shopping_intent"
    return merged
