"""
Credential redaction for outbound HTTP request log lines.

Some providers (SerpApi among them) authenticate with a key that must travel in the URL's query
string. httpx logs every request line at INFO with the full URL, so one `docker logs` away sits a
live credential — measured for real during CORE-4's live run. The key cannot move to a header the
provider does not support, so the log line is what gets fixed: a logging filter scrubs
credential-shaped query values before any handler renders the record.

The pattern is name-based, in the spirit of `app.agents.context.CREDENTIAL_NAME_MARKERS`: any query
parameter named like a credential loses its value, whatever module logged the line.
"""
from __future__ import annotations

import logging
import re

_REDACTED = "[redacted]"

_CREDENTIAL_PARAM_NAMES = (
    "api_key",
    "apikey",
    "access_token",
    "key",
    "token",
    "secret",
    "password",
)

_CREDENTIAL_QUERY_PATTERN = re.compile(
    r"([?&](?:" + "|".join(_CREDENTIAL_PARAM_NAMES) + r")=)[^&\s\"']+",
    re.IGNORECASE,
)


class CredentialQueryRedactionFilter(logging.Filter):
    """Rewrite any log record whose message carries a credential-shaped query value."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken record must not break logging itself
            return True
        scrubbed = _CREDENTIAL_QUERY_PATTERN.sub(rf"\1{_REDACTED}", message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = None
        return True


def install_credential_query_redaction() -> None:
    """Attach the filter to the loggers that render outbound request lines."""
    redaction_filter = CredentialQueryRedactionFilter()
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).addFilter(redaction_filter)
