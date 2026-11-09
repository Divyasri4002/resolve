"""
The default replacement provider: none is connected.

This exists so that "we cannot actually ask a merchant for a replacement" is a first-class,
explicit outcome in the system rather than something the executor papers over. It never returns
an acknowledgement, so no replacement can ever reach `requested` or `confirmed` through it.
"""
from __future__ import annotations

from app.tools.replacement.base import (
    ReplacementAck,
    ReplacementProvider,
    ReplacementProviderUnavailable,
    ReplacementRequest,
    ReplacementStatusInfo,
)


class UnconnectedReplacementProvider(ReplacementProvider):
    """Refuses every operation, stating plainly that no merchant channel exists."""

    name = "none"

    async def request_replacement(self, request: ReplacementRequest) -> ReplacementAck:
        raise ReplacementProviderUnavailable()

    async def get_replacement_status(self, provider_reference: str) -> ReplacementStatusInfo:
        raise ReplacementProviderUnavailable(
            "No merchant replacement provider is connected, so the status of this replacement "
            "cannot be read from anywhere."
        )
