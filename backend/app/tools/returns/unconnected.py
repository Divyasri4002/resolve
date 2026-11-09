"""
The default return provider: none is connected.

Exists so that "we cannot actually open a return with the merchant" is an explicit, first-class
outcome rather than something the executor papers over. It never returns an acknowledgement, so a
return can never reach `requested` or `confirmed` through it.
"""
from __future__ import annotations

from app.tools.returns.base import (
    ReturnAck,
    ReturnProvider,
    ReturnProviderUnavailable,
    ReturnRequest,
    ReturnStatusInfo,
    ReturnTarget,
)


class UnconnectedReturnProvider(ReturnProvider):
    """Refuses every operation, stating plainly that no merchant channel exists."""

    name = "none"

    async def resolve_return_target(self, request: ReturnRequest) -> ReturnTarget | None:
        # No merchant channel, so there is no target to resolve and nothing to submit.
        return None

    async def request_return(
        self, request: ReturnRequest, target: ReturnTarget | None = None
    ) -> ReturnAck:
        raise ReturnProviderUnavailable()

    async def get_return_status(self, provider_reference: str) -> ReturnStatusInfo:
        raise ReturnProviderUnavailable(
            "No merchant return provider is connected, so the status of this return cannot be "
            "read from anywhere."
        )
