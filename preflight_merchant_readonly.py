"""
Read-only live preflight for the merchant channel (disposable scratch harness).

Target: resolve-merchant-test.myshopify.com via Shopify client credentials. Four gates
run BEFORE any network call; a failed gate exits 1 having performed zero HTTP. When the
gates pass, the script performs exactly two calls: one client-credentials token grant
and one SHOP_IDENTITY GraphQL read (shop name + currencyCode). No mutation document is
reachable from this file, and the run command mounts the channel package read-only.

Secrets policy: the client id, client secret, and any granted token are never printed,
logged, or written anywhere. Output is limited to booleans, the shop fields Shopify
returns, and credential-free error messages.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "/merchant-channel")

from merchant_channel.auth import TokenAcquisitionError
from merchant_channel.config import ChannelConfigError, load_config
from merchant_channel.documents import SHOP_IDENTITY
from merchant_channel.gateway import (
    GatewayDefiniteFailure,
    GatewayOutcomeUnknown,
    ShopifyAdminGateway,
)

EXPECTED_DOMAIN = "resolve-merchant-test.myshopify.com"
EXPECTED_SHOP_NAME = "RESOLVE Merchant Test"
EXPECTED_CURRENCY = "INR"


def _fail(reason: str) -> int:
    print(f"PREFLIGHT GATE FAILURE: {reason}")
    print("NETWORK CALLS MADE: 0")
    return 1


async def main() -> int:
    static = os.environ.get("MERCHANT_SHOPIFY_ACCESS_TOKEN")
    if static:
        return _fail(
            "MERCHANT_SHOPIFY_ACCESS_TOKEN is set; this preflight is client-credentials only."
        )
    try:
        config = load_config(os.environ)
        mode = config.credential_mode()
    except ChannelConfigError as exc:
        return _fail(f"configuration refused: {exc}")
    if mode != "client_credentials":
        return _fail(f"credential mode is {mode!r}, expected 'client_credentials'.")
    if config.store_domain != EXPECTED_DOMAIN:
        return _fail(f"target domain is {config.store_domain!r}, expected {EXPECTED_DOMAIN!r}.")

    lowered = SHOP_IDENTITY.lower()
    if "mutation" in lowered or "draftorder" in lowered:
        return _fail("SHOP_IDENTITY is not a pure query; refusing to send it.")

    print(
        "ALL GATES PASSED: mode=client_credentials, static token absent, "
        f"target={config.store_domain}, document=SHOP_IDENTITY (query)"
    )
    print("NETWORK PHASE: exactly 1 token grant + 1 shop-identity GraphQL read")

    gateway = ShopifyAdminGateway(config)
    try:
        token = await gateway._tokens.token()
    except TokenAcquisitionError as exc:
        print(f"TOKEN ACQUISITION: FAILED — {exc}")
        return 1
    print(f"TOKEN ACQUISITION: succeeded (opaque-shpua-shape={token.startswith('shpua_')})")

    try:
        shop_name, currency = await gateway.shop_identity()
    except GatewayDefiniteFailure as exc:
        print(f"SHOP IDENTITY READ: FAILED (definite) — {exc}")
        return 1
    except GatewayOutcomeUnknown as exc:
        print(f"SHOP IDENTITY READ: UNKNOWN OUTCOME — {exc}")
        return 1

    print(f"SHOP NAME RETURNED: {shop_name!r}")
    print(f"CURRENCY RETURNED: {currency!r}")
    print(f"NAME MATCH ({EXPECTED_SHOP_NAME!r}): {shop_name == EXPECTED_SHOP_NAME}")
    print(f"CURRENCY MATCH ({EXPECTED_CURRENCY!r}): {currency == EXPECTED_CURRENCY}")
    print("MUTATIONS SENT: 0 (no create/complete path exists in this script)")
    print("CREDENTIALS OR TOKENS PRINTED: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
