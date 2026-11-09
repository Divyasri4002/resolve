"""
Return provider package: the seam between the return executor and merchant-side return
capability (RMA / label issuance).

Two implementations are selectable by configuration, and one of them is the default:

  * ``none`` (default) — :class:`UnconnectedReturnProvider`, which refuses explicitly and keeps
    every deployment inert by construction;
  * ``shopify`` — the already-implemented ``ShopifyReturnProvider``, built on the ONE existing
    Shopify credential path (``app.tools.shopify.get_shopify_clients`` reading ``Settings``), which
    hands it a read capability and the isolated return-write capability over that same credential.

Selecting ``shopify`` registers a capability, it does not exercise one: the branch imports lazily,
constructs a client from existing configuration, and performs no network exchange. A return is
still reached only through the hardened executor — authenticated mission, persisted assessment,
fresh Authority Engine decision, conflict checks, server-resolved target, and a claim committed
before any mutation — and the executor still refuses when the RESOLVE order carries no
server-established merchant linkage. Nothing here can be reached from an HTTP client: the return
endpoint takes no body, so no request can name a provider, an order, a line, a quantity or a
reason. (The name ``returns`` rather than ``return``: the latter is a Python reserved word.)
"""
from __future__ import annotations

from app.config import get_settings
from app.tools.returns.base import (
    ReturnAck,
    ReturnError,
    ReturnOutcomeUnknown,
    ReturnProvider,
    ReturnProviderRejected,
    ReturnProviderUnavailable,
    ReturnReconciliation,
    ReturnRequest,
    ReturnStatusInfo,
    ReturnSubmission,
    ReturnTarget,
)
from app.tools.returns.unconnected import UnconnectedReturnProvider


def get_return_provider(provider_name: str | None = None) -> ReturnProvider:
    """
    Return the configured return provider, defaulting to the explicit 'none'.

    'shopify' is built on the existing Shopify credential path — the same settings, the same
    client-credentials exchange, the same redacting sanitizer. What it gets is two capabilities over
    that one credential: the read-only client for every question it asks, and RESOLVE's isolated
    return-write capability for the single declared write. That is why this seam, and nothing else,
    is where the write capability is handed out; no second token store exists here and no credential
    is ever logged or returned. The clients are constructed eagerly; Shopify is contacted only when
    the provider itself is used, and an unconfigured store then surfaces as a credential-free
    ``ReturnProviderUnavailable`` at the point of use, never as a partial success.
    """
    settings = get_settings()
    provider = (provider_name or settings.return_provider or "none").lower()
    if provider == "none":
        return UnconnectedReturnProvider()
    if provider == "shopify":
        # Imported inside the branch on purpose: importing RESOLVE, or running with the default
        # configuration, must never pull in Shopify code or require Shopify credentials.
        from app.tools.shopify import get_shopify_clients
        from app.tools.shopify.return_provider import ShopifyReturnProvider

        read, write = get_shopify_clients(settings)
        return ShopifyReturnProvider(client=read, write_client=write)
    raise ValueError(
        f"Unknown return provider: {provider!r}. The configured providers are 'none' and "
        "'shopify'; nothing else is connected in this deployment."
    )


__all__ = [
    "ReturnAck",
    "ReturnError",
    "ReturnOutcomeUnknown",
    "ReturnProvider",
    "ReturnProviderRejected",
    "ReturnProviderUnavailable",
    "ReturnReconciliation",
    "ReturnRequest",
    "ReturnStatusInfo",
    "ReturnSubmission",
    "ReturnTarget",
    "UnconnectedReturnProvider",
    "get_return_provider",
]
