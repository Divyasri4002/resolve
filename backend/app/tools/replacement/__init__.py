"""
Replacement provider package: the seam between the replacement executor and merchant-side
fulfilment capability.

The registry intentionally offers exactly one implementation — "none". A test-only fake lives in
the test suite and is installed through the same seam, so no automated test can be mistaken for a
real merchant integration, and no fake can be enabled by configuration.
"""
from __future__ import annotations

from app.config import get_settings
from app.tools.replacement.base import (
    ReplacementAck,
    ReplacementError,
    ReplacementProvider,
    ReplacementProviderRejected,
    ReplacementProviderUnavailable,
    ReplacementRequest,
    ReplacementStatusInfo,
)
from app.tools.replacement.unconnected import UnconnectedReplacementProvider


def get_replacement_provider(provider_name: str | None = None) -> ReplacementProvider:
    """Return the configured replacement provider, defaulting to the explicit 'none'."""
    settings = get_settings()
    provider = (provider_name or settings.replacement_provider or "none").lower()
    if provider == "none":
        return UnconnectedReplacementProvider()
    raise ValueError(
        f"Unknown replacement provider: {provider!r}. No merchant fulfilment provider is "
        "connected in this deployment."
    )


__all__ = [
    "ReplacementAck",
    "ReplacementError",
    "ReplacementProvider",
    "ReplacementProviderRejected",
    "ReplacementProviderUnavailable",
    "ReplacementRequest",
    "ReplacementStatusInfo",
    "UnconnectedReplacementProvider",
    "get_replacement_provider",
]
