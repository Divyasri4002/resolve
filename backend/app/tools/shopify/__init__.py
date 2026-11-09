"""
Shopify provider package (Task 14B.3; read-only boundary 14C.7N; capability split 14C.7O).

Public surface mirrors the other provider packages: factories that build the configured clients
from Settings. Two capabilities live here, and they are separate objects on purpose:

  * `ShopifyReadOnlyClient` — every read RESOLVE performs. Query documents only, with no write
    allowance of any kind, so nothing a caller hands it can widen it;
  * `ShopifyReturnWriteClient` — RESOLVE's one declared write, the return operation, and nothing
    else. It cannot read. Only the return-provider seam builds it.

`get_shopify_client()` hands out the read capability; `get_shopify_clients()` hands out the pair
over ONE shared credential and ONE token cache. Splitting a capability must never mean duplicating
a secret, so there is one Shopify app, one client id, one client secret here — never a second
installed app, and no scope requested by this module.

Nothing here is connected to the refund/replacement/return executors or the Resolution
Orchestrator.
"""
from __future__ import annotations

from app.config import get_settings
from app.tools.shopify.admin_transport import ShopifyAdminTransport
from app.tools.shopify.client import DEFAULT_API_VERSION, ShopifyReadOnlyClient
from app.tools.shopify.errors import (
    ShopifyAuthError,
    ShopifyConfigError,
    ShopifyError,
    ShopifyGraphQLError,
    ShopifyMalformedResponseError,
    ShopifyRateLimitError,
    ShopifyReadOnlyViolation,
    ShopifyWriteCapabilityViolation,
)
from app.tools.shopify.models import ShopifyLineItem, ShopifyOrder, ShopifyProduct, ShopifyReturn
from app.tools.shopify.return_write_client import ShopifyReturnWriteClient
from app.tools.shopify.sanitizer import redact


def get_shopify_clients(
    settings=None,
) -> tuple[ShopifyReadOnlyClient, ShopifyReturnWriteClient]:
    """
    Build the read capability and the isolated return-write capability over one configured
    credential.

    Both clients are handed the SAME `ShopifyAdminTransport`, which is what keeps this a single
    Shopify app: one client id, one client secret, one short-lived access token exchanged once and
    shared. Constructing is inert by design — nothing is sent, and no credential is read from
    anywhere but configuration, so importing or constructing while Shopify is unconfigured is safe
    and the first call raises a credential-free ShopifyConfigError.

    Callers outside the return-provider seam should use `get_shopify_client()` instead: the write
    capability exists so that exactly one operation can leave RESOLVE, and the only code allowed to
    hold it is the code that submits returns.
    """
    cfg = settings or get_settings()
    admin = ShopifyAdminTransport(
        store_domain=cfg.shopify_store_domain,
        client_id=cfg.shopify_client_id,
        client_secret=cfg.shopify_client_secret,
        api_version=cfg.shopify_api_version or DEFAULT_API_VERSION,
    )
    return ShopifyReadOnlyClient(admin=admin), ShopifyReturnWriteClient(admin=admin)


def get_shopify_client(settings=None) -> ShopifyReadOnlyClient:
    """
    Build a read-only Shopify client from configuration. Credentials are environment-only and
    are never logged; construction succeeds even when Shopify is unconfigured so that importing
    RESOLVE never requires it — the first call then raises a credential-free ShopifyConfigError.
    """
    read, _write = get_shopify_clients(settings)
    return read


__all__ = [
    "DEFAULT_API_VERSION",
    "ShopifyAuthError",
    "ShopifyConfigError",
    "ShopifyError",
    "ShopifyGraphQLError",
    "ShopifyLineItem",
    "ShopifyMalformedResponseError",
    "ShopifyOrder",
    "ShopifyProduct",
    "ShopifyRateLimitError",
    "ShopifyReadOnlyClient",
    "ShopifyReadOnlyViolation",
    "ShopifyReturn",
    "ShopifyReturnWriteClient",
    "ShopifyWriteCapabilityViolation",
    "get_shopify_client",
    "get_shopify_clients",
    "redact",
]
