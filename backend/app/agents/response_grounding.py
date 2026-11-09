"""
Deterministic evidence-token verification for user-facing agent responses (Task CORE-6 Phase 2).

Phase 1's audit measured the path from the model's free text to the user and found exactly two
mechanical filters on it — secret redaction and a length cap. This module adds the third: a pure,
deterministic check that every verifiable factual token in a response is backed by the canonical
product evidence the model was actually shown. It is the output-side half of grounding; the
input-side (what the model is told) stays in `app.agents.context` and the provider prompts.

Scope, and the discipline behind it — this is deliberately NOT a semantic fact checker:

* Checked mechanically: currency amounts, ratings, review counts, merchant names, explicit
  product positions, product URLs, and explicitly claimed product ids. Each is checked only
  against the allowlist built from the canonical product projection the context carried, plus
  the mission's own budget (in its threshold context) and any server-held amounts handed in.
* Never checked: subjective judgement, comparisons, recommendations, suitability reasoning,
  qualitative description. A sentence with no verifiable token passes with zero violations, so
  the agent stays free to reason, compare and recommend. Unsupported attributes ("100% cotton")
  are kept out of responses by the grounding instructions both providers are given — arbitrary
  attribute extraction would be unreliable and would turn the agent rigid.

Fail-closed conventions: a missing value is never evidence; a merchant mention that could name
more than one canonical merchant is a violation; a URL must match exactly; a position outside
the listed result set is a violation; the mission budget is accepted only in threshold context
("under ₹700"), never as a product's price.

Nothing in this module performs I/O of any kind — no network, no database, no clock, no
randomness — so the same message over the same evidence always produces the same verdict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# The honest ending when one bounded correction still cannot produce a grounded response. Server
# text, no product tokens, so it always passes the verifier it sits behind.
SAFE_FALLBACK_MESSAGE = (
    "I couldn't produce a fully grounded product response from the available product data. "
    "I can continue using the verified details available."
)

VIOLATION_KINDS = (
    "price",
    "rating",
    "review_count",
    "merchant",
    "position",
    "url",
    "product_id",
)

# --------------------------------------------------------------------------- #
# Token shapes. Every pattern below detects; the allowlist decides.
# --------------------------------------------------------------------------- #

_NUMBER = r"[0-9][0-9,]*(?:\.[0-9]+)?"
_CURRENCY_MARKER = r"(?:₹|\brs\.?|\binr\b|\brupees?\b)"

_PRICE_BEFORE = re.compile(rf"({_CURRENCY_MARKER})\s*({_NUMBER})", re.I)
_PRICE_AFTER = re.compile(rf"({_NUMBER})\s*({_CURRENCY_MARKER})", re.I)

# Budget amounts are accepted only in their threshold context, and only when the number IS the
# mission's budget — "under ₹700" passes when the budget is 700; "under ₹500" does not. A bare
# "₹700" outside such a context must be a real product price.
_BUDGET_CONTEXT_BEFORE = re.compile(
    rf"\b(?:under|below|within|up\s+to|upto|budget(?:\s+of|\s+is|:)?|max(?:imum)?|at\s+most|"
    rf"around|approx(?:imately)?|less\s+than|cheaper\s+than|no\s+more\s+than)"
    rf"\s*{_CURRENCY_MARKER}?\s*({_NUMBER})",
    re.I,
)
_BUDGET_CONTEXT_AFTER = re.compile(
    rf"({_NUMBER})\s*{_CURRENCY_MARKER}?\s*\b(?:budget|ceiling|cap)\b", re.I
)
_BUDGET_CONTEXT_BETWEEN = re.compile(
    rf"\bbetween\s+{_CURRENCY_MARKER}?\s*({_NUMBER})\s+and\s+{_CURRENCY_MARKER}?\s*({_NUMBER})", re.I
)

_RATING_MARKED = re.compile(
    rf"\b(\d{{1,2}}(?:\.\d+)?)\s*(?:/\s*5\b|out\s+of\s+5\b|stars?\b|★)", re.I
)
_RATING_WORD_FIRST = re.compile(r"\b(?:rated|ratings?)\s*(?:of|is|:)?\s*(\d{1,2}(?:\.\d+)?)", re.I)
_RATING_WORD_LAST = re.compile(r"\b(\d\.\d)\s*ratings?\b", re.I)
_RATING_BARE = re.compile(r"\b(\d\.\d)\b")

_REVIEW_AFTER = re.compile(rf"\b({_NUMBER})\s*(?:reviews?|ratings)\b", re.I)
_REVIEW_BEFORE = re.compile(rf"\b(?:reviews?|ratings)\s*[:\-]?\s*({_NUMBER})\b", re.I)

_ORDINAL_WORDS = {
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
}
_ORDINAL_ALTERNATION = "|".join(_ORDINAL_WORDS)
_NOUNS = r"(?:products?|options?|items?|choices?|ones?|results?)"
_POSITION_PATTERNS = (
    re.compile(rf"\bthe\s+({_ORDINAL_ALTERNATION})\s+{_NOUNS}\b", re.I),
    re.compile(rf"\b({_ORDINAL_ALTERNATION})\s+{_NOUNS}\b", re.I),
    re.compile(r"\b(?:option|number|no\.?|product|item|choice|result)\s*#?\s*(\d{1,2})\b", re.I),
    re.compile(r"(?:^|(?<=\s))#(\d{1,2})\b"),
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)\s+{_NOUNS}\b", re.I),
)

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

_MERCHANT_CANDIDATE = re.compile(
    r"\b(?:on|from|by|at|via)\s+"
    r"((?:[A-Za-z][\w&'\-]*(?:\.[A-Za-z]{2,})?)(?:[^\S\n]+(?-i:[A-Z][\w&'\-]*(?:\.[A-Za-z]{2,})?)){0,2})",
    re.I,
)

# Words that follow prepositions in ordinary sentences but are not merchants. Includes the
# prepositions themselves: after an earlier stage masks a token, "at ₹399 from Nykaa" becomes
# "at     from Nykaa", and a preposition must never be judged as a merchant candidate. A
# stoplist, not a dictionary of merchants: anything not listed must still match the canonical
# merchant values to pass.
_NON_MERCHANT_WORDS = frozenset(
    {
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december",
        "am", "pm", "ist", "gmt", "utc", "bst", "pst", "est", "cet",
        "today", "tomorrow", "tonight", "yesterday",
        "india", "usa", "uk", "us", "eu",
        "i", "me", "you", "your", "our", "their", "it", "its", "this", "that",
        "these", "those", "the", "a", "an", "and", "or", "to", "for", "with",
        "of", "in", "at", "on", "by", "from", "via", "one",
    }
)

# An explicitly claimed product id — "id is X", "product id: X". The capture must contain a digit
# so prose like "the id is the best part" is never mistaken for an identifier.
_ID_CLAIM = re.compile(
    r"\b(?:product\s+)?ids?\s*(?:is|:|=)?\s*[\"']?([A-Za-z0-9][A-Za-z0-9_\-:/\.]*)", re.I
)

_CORRECTION_PREAMBLE = (
    "Your response stated product facts that are not in the verified evidence:"
)
_CORRECTION_INSTRUCTION = (
    " Rewrite the response using only the verified facts. Keep your reasoning, comparisons and "
    "recommendation, and say plainly when the available product data does not confirm something."
)


@dataclass(frozen=True)
class GroundingViolation:
    """One unsupported factual token, with the mechanical reason it was refused."""

    kind: str
    token: str
    detail: str


@dataclass(frozen=True)
class GroundingVerdict:
    """The outcome of one verification pass. `allowed` is False only on mechanical violations."""

    allowed: bool
    violations: tuple[GroundingViolation, ...]
    correction_note: str | None
    evidence_summary: dict[str, Any]


@dataclass(frozen=True)
class GroundedResponse:
    """The final response after verification and at most one bounded correction."""

    message: str
    verdict: GroundingVerdict
    corrections: int


# --------------------------------------------------------------------------- #
# Evidence allowlist
# --------------------------------------------------------------------------- #


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _price_from_display(value: Any) -> Decimal | None:
    """A number out of a formatted price string like '₹1,999' or 'Rs. 549'."""
    if value is None:
        return None
    match = re.search(_NUMBER, str(value))
    if match is None:
        return None
    return _coerce_decimal(match.group(0))


def _absorb_product(product: dict[str, Any], evidence: dict[str, Any]) -> None:
    product_id = product.get("product_id")
    if isinstance(product_id, str) and product_id.strip():
        evidence["product_ids"].add(product_id.strip())
    for source in ("extracted_price", "price"):
        price = _coerce_decimal(product.get(source))
        if price is None:
            price = _price_from_display(product.get(source))
        if price is not None:
            evidence["prices"].add(price)
    rating = _coerce_decimal(product.get("rating"))
    if rating is not None:
        evidence["ratings"].add(rating)
    review_count = _coerce_decimal(product.get("review_count"))
    if review_count is not None:
        evidence["review_counts"].add(review_count)
    merchant = product.get("merchant")
    if isinstance(merchant, str) and merchant.strip():
        evidence["merchants"].add(merchant.strip())
    product_url = product.get("product_url")
    if isinstance(product_url, str) and product_url.strip():
        evidence["urls"].add(_normalize_url(product_url))


def _normalize_url(url: str) -> str:
    return url.strip().rstrip(".,;:!?'\"").rstrip("/")


def build_evidence(
    canonical_products: list[dict[str, Any]], relevant_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """
    The allowlist the verifier checks against: every verified fact the model was shown, and
    nothing else. `additional_products` (a selected product, a resolved reference) contribute
    their facts but never extend the numbered result set that position claims are checked
    against.
    """
    context = relevant_context or {}
    evidence: dict[str, Any] = {
        "positions": 0,
        "prices": set(),
        "ratings": set(),
        "review_counts": set(),
        "merchants": set(),
        "urls": set(),
        "product_ids": set(),
        "titles": set(),
        "budget": _coerce_decimal(context.get("budget")),
        "amounts": set(),
        "currency": context.get("currency"),
    }
    for product in canonical_products or []:
        if isinstance(product, dict):
            evidence["positions"] += 1
            _absorb_product(product, evidence)
            title = product.get("title")
            if isinstance(title, str) and title.strip():
                evidence["titles"].add(title.strip().casefold())
    for product in context.get("additional_products") or []:
        if isinstance(product, dict):
            _absorb_product(product, evidence)
            title = product.get("title")
            if isinstance(title, str) and title.strip():
                evidence["titles"].add(title.strip().casefold())
    for amount in context.get("amounts") or []:
        value = _coerce_decimal(amount)
        if value is not None:
            evidence["amounts"].add(value)
    return evidence


def _evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "listed_products": evidence["positions"],
        "prices": sorted(str(price) for price in evidence["prices"]),
        "ratings": sorted(str(rating) for rating in evidence["ratings"]),
        "review_counts": sorted(str(count) for count in evidence["review_counts"]),
        "merchants": sorted(evidence["merchants"]),
        "product_urls": len(evidence["urls"]),
        "product_ids": len(evidence["product_ids"]),
        "budget": str(evidence["budget"]) if evidence["budget"] is not None else None,
        "server_amounts": sorted(str(amount) for amount in evidence["amounts"]),
        "currency": evidence["currency"],
    }


# --------------------------------------------------------------------------- #
# The scanner: mask-then-check, stage by stage, so "4.2 rating" is never read
# as a review count and "₹4.5" is never read as a rating.
# --------------------------------------------------------------------------- #


def _masked_findings(
    pattern: re.Pattern[str], text: str
) -> tuple[str, list[tuple[str, tuple[str, ...]]]]:
    """Find every match, keep (whole match, captured groups), and mask the whole match out."""
    findings: list[tuple[str, tuple[str, ...]]] = []

    def _take(match: re.Match[str]) -> str:
        findings.append((match.group(0), match.groups()))
        return " " * (match.end() - match.start())

    text = pattern.sub(_take, text)
    return text, findings


def _scan(message: str, evidence: dict[str, Any]) -> list[GroundingViolation]:
    violations: list[GroundingViolation] = []
    text = message

    # 1. URLs: exact matches only, never fetched, never normalized into something else.
    text, findings = _masked_findings(_URL, text)
    for whole, _groups in findings:
        if _normalize_url(whole) not in evidence["urls"]:
            violations.append(
                GroundingViolation("url", whole, "no verified product carries this URL")
            )

    # 2. Budget-context amounts: the number that IS the mission's budget is masked here; a
    #    threshold number that is not the budget survives to be judged as a product price.
    text = _mask_budget_matches(text, evidence["budget"])

    # 3. Currency amounts outside budget context: must be a verified product price or a
    #    server-held amount.
    for pattern in (_PRICE_BEFORE, _PRICE_AFTER):
        text, findings = _masked_findings(pattern, text)
        for whole, groups in findings:
            value = _first_number(groups)
            if value is None:
                continue
            if value not in evidence["prices"] and value not in evidence["amounts"]:
                violations.append(
                    GroundingViolation(
                        "price",
                        whole.strip(),
                        "no verified product or server amount has this value",
                    )
                )

    # 4. Ratings in every equivalent shape.
    for pattern in (_RATING_MARKED, _RATING_WORD_FIRST, _RATING_WORD_LAST, _RATING_BARE):
        text, findings = _masked_findings(pattern, text)
        for whole, groups in findings:
            value = _first_number(groups)
            if value is None:
                continue
            if value not in evidence["ratings"]:
                violations.append(
                    GroundingViolation("rating", whole.strip(), "no verified product has this rating")
                )

    # 5. Review counts.
    for pattern in (_REVIEW_AFTER, _REVIEW_BEFORE):
        text, findings = _masked_findings(pattern, text)
        for whole, groups in findings:
            value = _first_number(groups)
            if value is None:
                continue
            if value not in evidence["review_counts"]:
                violations.append(
                    GroundingViolation(
                        "review_count",
                        whole.strip(),
                        "no verified product has this review count",
                    )
                )

    # 6. Explicit positions: must exist in the listed result set.
    for pattern in _POSITION_PATTERNS:
        text, findings = _masked_findings(pattern, text)
        for whole, groups in findings:
            raw = groups[-1]
            position = _ORDINAL_WORDS.get(raw.lower())
            if position is None:
                try:
                    position = int(raw)
                except ValueError:
                    continue
            if position < 1 or position > evidence["positions"]:
                violations.append(
                    GroundingViolation(
                        "position",
                        whole.strip(),
                        f"the verified results list only {evidence['positions']} product(s)",
                    )
                )

    # 7. Merchant assertions: every candidate must name the canonical merchant value, or name a
    #    product title (a product mention, not a merchant claim). Ambiguous -> violation. The
    #    candidate judged is the captured phrase AFTER the preposition — judging "on Amazon.in"
    #    with its preposition attached would let the stoplist swallow real assertions.
    text, findings = _masked_findings(_MERCHANT_CANDIDATE, text)
    for whole, groups in findings:
        candidate = (groups[0] or "").strip()
        problem = _merchant_problem(candidate, evidence)
        if problem is not None:
            violations.append(GroundingViolation("merchant", whole.strip(), problem))

    # 8. Explicit product-id claims.
    text, findings = _masked_findings(_ID_CLAIM, text)
    for whole, groups in findings:
        claimed = groups[-1]
        if not any(character.isdigit() for character in claimed):
            continue
        if claimed not in evidence["product_ids"]:
            violations.append(
                GroundingViolation("product_id", claimed, "no verified product carries this id")
            )

    return violations


def _first_number(groups: tuple[str, ...]) -> Decimal | None:
    """The first captured group that parses as a number — markers like the rupee sign never do."""
    for raw in groups:
        value = _coerce_decimal(raw)
        if value is not None:
            return value
    return None


def _mask_budget_matches(text: str, budget: Decimal | None) -> str:
    """Mask only the numbers inside budget-context matches that ARE the budget."""
    if budget is None:
        return text
    for pattern in (_BUDGET_CONTEXT_BEFORE, _BUDGET_CONTEXT_AFTER, _BUDGET_CONTEXT_BETWEEN):
        def _replace(match: re.Match[str]) -> str:
            characters = list(match.group(0))
            base = match.start()
            for index, raw in enumerate(match.groups(), start=1):
                value = _coerce_decimal(raw)
                if value is not None and value == budget:
                    start, end = match.span(index)
                    for offset in range(start - base, end - base):
                        characters[offset] = " "
            return "".join(characters)

        text = pattern.sub(_replace, text)
    return text


def _merchant_problem(candidate: str, evidence: dict[str, Any]) -> str | None:
    tokens = [token.strip(".,") for token in candidate.split()]
    if not tokens:
        return None
    if tokens[0].casefold() in _NON_MERCHANT_WORDS:
        return None
    # The whole candidate must be the canonical merchant (case-insensitively) or an unambiguous
    # shorter form of exactly one of them. A candidate with extra words ("Myntra Mall") invents a
    # merchant alias the evidence does not carry, so it fails closed — trailing tokens are never
    # trimmed away to force a match.
    merchants = {merchant.casefold() for merchant in evidence["merchants"]}
    phrase = " ".join(tokens).casefold()
    if phrase in merchants:
        return None
    containing = [merchant for merchant in merchants if phrase in merchant]
    if len(containing) == 1:
        return None
    if len(containing) > 1:
        return "names more than one possible merchant, so it cannot be verified"
    # A capitalized phrase that names one of the verified product titles is a product mention,
    # not a merchant assertion — "switching from Peter England to Arrow" says nothing about a
    # store. Merchant identity is never inferred from a title; the mention is simply not judged.
    hits = [title for title in evidence["titles"] if candidate.casefold() in title]
    if hits:
        return None
    return "no verified product names this merchant"


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #


def verify_response_against_context(
    message: str,
    canonical_products: list[dict[str, Any]],
    relevant_context: dict[str, Any] | None = None,
) -> GroundingVerdict:
    """
    Check one response against the canonical product evidence it was written over.

    With no product evidence at all there is nothing to verify against and nothing is invented
    to check: the verdict allows. With evidence, every detected factual token must be on the
    allowlist, and qualitative reasoning is passed through untouched.
    """
    evidence = build_evidence(canonical_products, relevant_context)
    if evidence["positions"] == 0 and not evidence["product_ids"] and not evidence["prices"]:
        return GroundingVerdict(True, (), None, _evidence_summary(evidence))
    violations = _scan(message, evidence)
    if violations:
        listed = "; ".join(
            f"{violation.token!r} ({violation.kind}: {violation.detail})"
            for violation in violations
        )
        note = f"{_CORRECTION_PREAMBLE} {listed}.{_CORRECTION_INSTRUCTION}"
        return GroundingVerdict(False, tuple(violations), note, _evidence_summary(evidence))
    return GroundingVerdict(True, (), None, _evidence_summary(evidence))


# --------------------------------------------------------------------------- #
# One bounded correction (CORE-6 §12). Response generation only: the provider
# is asked to re-word, never to act. The caller's tool surface is not involved.
# --------------------------------------------------------------------------- #


async def grounded_response(
    reasoning: Any,
    draft: str,
    *,
    user_request: str,
    products: list[dict[str, Any]],
    relevant_context: dict[str, Any] | None = None,
) -> GroundedResponse:
    """
    Verify a draft; on a mechanical violation, give the SAME provider exactly one chance to
    re-word it against the same evidence; verify the re-wording; fall back honestly otherwise.

    The draft is never silently rewritten by the server, the correction never names a tool, and
    a second failure ends in `SAFE_FALLBACK_MESSAGE` rather than a third attempt.
    """
    verdict = verify_response_against_context(draft, products, relevant_context)
    if verdict.allowed:
        return GroundedResponse(draft, verdict, 0)
    corrections = 0
    reground = getattr(reasoning, "reground_response", None)
    if reground is not None:
        corrections = 1
        try:
            corrected = await reground(
                user_request=user_request,
                evidence={"verified_products": products, "relevant_context": relevant_context},
                draft_response=draft,
                violations_note=verdict.correction_note or "",
            )
        except Exception:  # noqa: BLE001 — a failed correction must never break the turn
            corrected = None
        if corrected:
            second = verify_response_against_context(str(corrected), products, relevant_context)
            if second.allowed:
                return GroundedResponse(str(corrected).strip(), second, corrections)
    fallback = verify_response_against_context(SAFE_FALLBACK_MESSAGE, products, relevant_context)
    return GroundedResponse(SAFE_FALLBACK_MESSAGE, fallback, corrections)
