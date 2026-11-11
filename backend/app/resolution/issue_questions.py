"""Per-category intake questions for reported issues (M22 §2).

A reported issue starts with only the buyer's free-text description. These sets are the
deterministic, category-relevant follow-up questions RESOLVE asks so a later assessment
reasons over the buyer's own specifics instead of guessing. The sets are a pure constant:
no model writes them, no client supplies them, and the same issue_type always produces
the same questions.

Answers are the buyer's own words, stored on the issue row as context — never a
decision, an authorization, or a fact about the order. The backend's own DB facts
(order, payment, authority) reach the assessment through the canonical context builder;
these answers only ever describe what the buyer says about the problem.
"""
from __future__ import annotations

from dataclasses import dataclass

ANSWER_MAX_CHARS = 500


@dataclass(frozen=True)
class CategoryQuestion:
    question_id: str
    prompt: str


ISSUE_CATEGORY_QUESTIONS: dict[str, tuple[CategoryQuestion, ...]] = {
    "damaged": (
        CategoryQuestion(
            "damage_nature",
            "What exactly is damaged, and where on the item is the damage?",
        ),
        CategoryQuestion(
            "packaging_state",
            "What condition was the outer packaging in when it arrived?",
        ),
        CategoryQuestion(
            "damage_photos",
            "Do you have photos of the damage? If yes, what do they show?",
        ),
    ),
    "defective": (
        CategoryQuestion(
            "defect_behavior",
            "What does the product do, or fail to do, that it shouldn't?",
        ),
        CategoryQuestion(
            "first_failure",
            "Did it work at all before the problem appeared?",
        ),
        CategoryQuestion(
            "troubleshooting",
            "What troubleshooting, if any, have you already tried?",
        ),
    ),
    "wrong_item": (
        CategoryQuestion(
            "expected_item",
            "What did you order — the exact name or model you expected?",
        ),
        CategoryQuestion(
            "received_item",
            "What did you actually receive?",
        ),
        CategoryQuestion(
            "mismatch_point",
            "How does what you received differ from what you ordered?",
        ),
    ),
    "missing_item": (
        CategoryQuestion(
            "missing_part",
            "Is the whole order missing, or part of it — and which part?",
        ),
        CategoryQuestion(
            "package_contents",
            "What did the package actually contain when you opened it?",
        ),
    ),
    "not_received": (
        CategoryQuestion(
            "tracking_seen",
            "What does the carrier's tracking show, if you were able to check it?",
        ),
        CategoryQuestion(
            "delivery_location",
            "Was anything left at the delivery address — a notice, a neighbour, a safe place?",
        ),
    ),
    "quality_issue": (
        CategoryQuestion(
            "quality_gap",
            "How does the item differ from what you expected when you bought it?",
        ),
        CategoryQuestion(
            "usage_so_far",
            "Have you used the item, and for how long, before noticing the problem?",
        ),
    ),
    "other": (
        CategoryQuestion(
            "issue_summary",
            "Describe the issue in your own words — what happened?",
        ),
        CategoryQuestion(
            "desired_outcome",
            "What outcome would you consider this issue resolved?",
        ),
    ),
}


def questions_for(issue_type: str) -> tuple[CategoryQuestion, ...]:
    """The deterministic question set for an issue category (empty for unknown types)."""
    return ISSUE_CATEGORY_QUESTIONS.get(issue_type, ())


def questions_payload(issue_type: str) -> list[dict]:
    """JSON-shaped question set, in canonical order."""
    return [
        {"question_id": q.question_id, "prompt": q.prompt}
        for q in questions_for(issue_type)
    ]


def normalize_answers(issue_type: str, raw: object) -> tuple[dict[str, str] | None, str | None]:
    """Validate a client answer map against the category's question set.

    Returns (normalized_answers, error). Normalized answers are stripped, bounded text
    keyed only by question ids this category actually defines; any unknown id, wrong
    type, empty answer, or overlong answer is a rejection of the whole submission —
    the stored answer set is never a partial mix of old and new.
    """
    if not isinstance(raw, dict):
        return None, "answers must be an object of question_id -> text"
    known = {q.question_id: q for q in questions_for(issue_type)}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if key not in known:
            return None, f"unknown question_id {key!r} for issue_type {issue_type!r}"
        if not isinstance(value, str):
            return None, f"answer for {key!r} must be text"
        stripped = value.strip()
        if not stripped:
            return None, f"answer for {key!r} is empty"
        if len(stripped) > ANSWER_MAX_CHARS:
            return None, f"answer for {key!r} exceeds {ANSWER_MAX_CHARS} characters"
        normalized[key] = stripped
    if not normalized:
        return None, "at least one answer is required"
    return normalized, None
