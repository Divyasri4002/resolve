"""
Credential redaction for the Shopify client (Task 14B.3).

The rule this enforces is simple and absolute: the client secret and any access token must
never appear in an exception message, a log line, or script output — even if Shopify itself
echoes something back. Secrets are registered the moment they are known (config load, token
exchange); `redact()` then scrubs every outbound string. This is a belt-and-braces guard on
top of never interpolating secrets into messages in the first place.
"""
from __future__ import annotations

import re
from typing import Iterable

# Secrets registered for this process. Values are never read back out or logged — the list is
# write-only from the caller's point of view and exists solely so redact() can find them.
_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    """Register a secret value to scrub. Empty/whitespace values are ignored; duplicates too."""
    if value is None:
        return
    cleaned = value.strip()
    if cleaned and cleaned not in _SECRETS:
        _SECRETS.append(cleaned)


def registered_secret_values() -> tuple[str, ...]:
    """Read-only view of registered secrets — provided so tests can assert redaction works.
    Production code must never log or return these."""
    return tuple(_SECRETS)


def reset_registered_secrets() -> None:
    """Clear registered secrets (used by tests for isolation)."""
    _SECRETS.clear()


def _apply_patterns(text: str) -> str:
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?\S+", r"\1\2[redacted]", text)
    # shpat_ (Admin), shpua_ (app/user access), shpca_ (app secret) token shapes.
    text = re.sub(r"(?i)shp(?:at|ua|ca)_[A-Za-z0-9]+", "[redacted-token]", text)
    text = re.sub(r"(?i)shppa_[A-Za-z0-9]+", "[redacted-secret]", text)
    text = re.sub(r"(?i)(client_secret\s*[=:]\s*)\S+", r"\1[redacted]", text)
    return text


def redact(text: str, extra_secrets: Iterable[str] | None = None) -> str:
    """
    Scrub every registered secret (and any extra supplied value) plus known credential shapes
    out of `text`. Non-strings are coerced via str() so an unexpected object cannot leak fields.
    """
    if text is None:
        return ""
    out = text if isinstance(text, str) else str(text)
    candidates = list(_SECRETS)
    if extra_secrets:
        candidates.extend(s for s in extra_secrets if s)
    for secret in candidates:
        if secret and secret in out:
            out = out.replace(secret, "[redacted]")
    return _apply_patterns(out)


__all__ = [
    "redact",
    "register_secret",
    "registered_secret_values",
    "reset_registered_secrets",
]
