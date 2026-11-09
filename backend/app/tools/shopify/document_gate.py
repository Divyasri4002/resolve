"""
Task 14C.7N — decide, locally, what kind of GraphQL document is about to be sent.

Why this exists: `ShopifyReadOnlyClient.execute_graphql` accepts a document as text, so the
read-only boundary was only ever a convention about what the client's own methods happened to
pass. A convention is not a property of code. This module turns it into one: a document is
classified before anything is built or sent, and anything that is not unambiguously exactly one
read operation is refused.

The classification is deliberately not a GraphQL parser and adds no dependency. It answers one
question, and it answers it conservatively:

  * exactly one root `query` operation (named, or the anonymous `{…}` shorthand) -> a read;
  * anything else — `mutation`, `subscription`, no operation, an empty or non-text document,
    unbalanced braces, several root operations, an operation with no body — is not a read.

Fail-closed is the whole point: a classifier that had to be *taught* about each new dangerous
operation would let an unknown one through. Here, an operation this module cannot positively
identify as a single read never reaches the transport.

Root field names are extracted for one reason: so a write surface that whitelists operations by
exact name can refuse `orderCancel` while naming it, and cannot be bypassed by a document that
bundles two root fields (`mutation { a, b }`) or hides one behind an alias.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

QUERY = "query"
MUTATION = "mutation"
SUBSCRIPTION = "subscription"
UNKNOWN = "unknown"

_OPERATION_KINDS = frozenset({QUERY, MUTATION, SUBSCRIPTION})
# A GraphQL name, and the only shape this module will ever put into an error message.
_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_OPENERS = "{(["
_CLOSERS = ")]}"


@dataclass(frozen=True)
class DocumentVerdict:
    """What `classify` learned about one document. `reason` is safe to surface; documents are not."""

    kind: str
    root_fields: tuple[str, ...]
    root_operations: int
    well_formed: bool
    reason: str

    @property
    def is_read(self) -> bool:
        """Exactly one root query operation, positively identified."""
        return self.well_formed and self.kind == QUERY and self.root_operations == 1

    @property
    def surface_name(self) -> str:
        """The one root field name, if there is exactly one and it is a plain GraphQL name."""
        if len(self.root_fields) != 1:
            return ""
        candidate = self.root_fields[0]
        return candidate if _SAFE_NAME.match(candidate) else ""


def _without_comments(text: str) -> str:
    """Blank out `# … end of line` comments, ignoring `#` inside string literals."""
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            end = _string_end(text, index)
            out.append(text[index:end])
            index = end
            continue
        if char == "#":
            while index < length and text[index] != "\n":
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _string_end(text: str, start: int) -> int:
    """Index just past the double-quoted string literal beginning at `start`."""
    index = start + 1
    length = len(text)
    while index < length:
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index + 1
        index += 1
    return length


def _scan(text: str):
    """
    Walk a comment-free document once, yielding every bare word that occurs at nesting depth 0.

    Depth counts `{`/`(`/`[` together, so a variable *named* `mutation` in a definition list
    (`query Q($mutation: Boolean!)`) is not mistaken for a root operation, and a field named
    `query` inside a selection is invisible to the caller.
    """
    index = 0
    length = len(text)
    depth = 0
    while index < length:
        char = text[index]
        if char == '"':
            index = _string_end(text, index)
            continue
        if char in _OPENERS:
            depth += 1
            index += 1
            continue
        if char in _CLOSERS:
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and (char.isalpha() or char == "_"):
            end = index + 1
            while end < length and (text[end].isalnum() or text[end] in "_"):
                end += 1
            yield text[index:end].lower(), index
            index = end
            continue
        index += 1


def _operation_body(text: str, keyword_index: int) -> tuple[int, int] | None:
    """The span between the operation's own opening and closing brace, or None if there is none."""
    depth = 0
    opened: int | None = None
    for index in range(keyword_index, len(text)):
        char = text[index]
        if char == '"':
            continue
        if char == "{":
            if opened is None:
                opened = index
            depth += 1
            continue
        if char == "}":
            if opened is None:
                return None
            depth -= 1
            if depth == 0:
                return (opened, index)
    return None


def _root_fields(text: str, opened: int, closed: int) -> tuple[str, ...]:
    """
    Names selected directly by the operation body — depth 1, so nested selections do not count.

    A word immediately followed by `:` is an alias, not a field, and is skipped; the name after it
    is the field. `@directive` names are skipped. Field arguments live inside parentheses, which
    are already deeper than 1.
    """
    fields: list[str] = []
    index = opened + 1
    depth = 1
    skip_word = False
    while index < closed:
        char = text[index]
        if char == '"':
            index = _string_end(text, index)
            continue
        if char in _OPENERS:
            depth += 1
            index += 1
            continue
        if char in _CLOSERS:
            depth -= 1
            index += 1
            continue
        if char == "@":
            skip_word = True
            index += 1
            continue
        if depth == 1 and (char.isalpha() or char == "_"):
            end = index + 1
            while end < closed and (text[end].isalnum() or text[end] in "_"):
                end += 1
            word = text[index:end]
            following = text[end:closed].lstrip(" \t\r\n,")
            is_alias = following.startswith(":")
            if not skip_word and not is_alias:
                fields.append(word)
            skip_word = False
            index = end
            continue
        index += 1
    return tuple(fields)


def _balanced(text: str) -> bool:
    depth = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            index = _string_end(text, index)
            continue
        if char in "{([":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                return False
        index += 1
    return depth == 0


def classify(document: object) -> DocumentVerdict:
    """Classify one GraphQL document. Never raises: an unreadable document is `unknown`."""
    if not isinstance(document, str):
        return DocumentVerdict(UNKNOWN, (), 0, False, "the document is not text")
    text = _without_comments(document)
    if not text.strip():
        return DocumentVerdict(UNKNOWN, (), 0, False, "the document carries no operation")
    if not _balanced(text):
        return DocumentVerdict(UNKNOWN, (), 0, False, "the document has unbalanced braces")

    operations = [
        (kind, index) for kind, index in _scan(text) if kind in _OPERATION_KINDS
    ]
    if not operations and text.lstrip().startswith("{"):
        operations = [(QUERY, 0)]  # the anonymous query shorthand
    if not operations:
        return DocumentVerdict(UNKNOWN, (), 0, False, "the document has no root operation")
    if len(operations) > 1:
        kinds = {kind for kind, _ in operations}
        kind = kinds.pop() if len(kinds) == 1 else UNKNOWN
        return DocumentVerdict(
            kind, (), len(operations), False, "the document has more than one root operation"
        )

    kind, keyword_index = operations[0]
    span = _operation_body(text, keyword_index)
    if span is None:
        return DocumentVerdict(kind, (), 1, False, "the operation carries no selection body")
    fields = _root_fields(text, *span)
    if not fields:
        return DocumentVerdict(kind, (), 1, False, "the operation selects nothing")
    return DocumentVerdict(kind, fields, 1, True, "")


__all__ = [
    "MUTATION",
    "QUERY",
    "SUBSCRIPTION",
    "UNKNOWN",
    "DocumentVerdict",
    "classify",
]
