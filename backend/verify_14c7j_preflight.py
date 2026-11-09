"""
Task 14C.7J PHASE 0 — pre-flight audit for a disposable live pairing.

Read-only by construction: every Shopify document below is a `query`, the database is only SELECTed,
and nothing here creates, stamps, cancels, signs, posts or enables anything. The script's whole
purpose is to answer "are the prerequisites for ONE truthful live pairing met?" before any write is
even considered, and to say which one is not met.

Output rule: secrets are reported as `configured = true/false` and never as values. Store
identifiers, currencies, counts and GIDs are printed because they are not credentials; no customer
field is queried or printed at all.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.tools.shopify import get_shopify_client
from app.tools.shopify.sanitizer import redact

SENT: list[str] = []
MERCHANT_CHANNEL_ROOT = os.environ.get("MERCHANT_CHANNEL_ROOT", "")

SCOPES = """
query Preflight14c7jScopes {
  currentAppInstallation { accessScopes { handle } }
}
"""

SHOP_AND_CATALOG = """
query Preflight14c7jStore {
  shop { name currencyCode }
  products(first: 10) {
    edges {
      node {
        id
        title
        status
        priceRangeV2 { minVariantPrice { amount currencyCode } }
        variants(first: 5) {
          edges {
            node {
              id
              sku
              price
              availableForSale
              selectedOptions { name value }
            }
          }
        }
      }
    }
  }
}
"""

ORDERS = """
query Preflight14c7jOrders {
  orders(first: 10) {
    edges {
      node {
        id
        name
        currencyCode
        displayFinancialStatus
        displayFulfillmentStatus
        returnStatus
        createdAt
        sourceName
        poNumber
        tags
        totalPriceSet { shopMoney { amount currencyCode } }
        lineItems(first: 10) {
          edges { node { id quantity product { id } variant { id } } }
        }
        returns(first: 5) { edges { node { id name status } } }
      }
    }
  }
}
"""

BASELINE_1001 = {
    "order_gid": "gid://shopify/Order/7990366601503",
    "name": "#1001",
    "currency": "USD",
    "financial": "PAID",
    "fulfillment": "FULFILLED",
    "return_status": "NO_RETURN",
    "line_quantity": 1,
    "product_gid": "gid://shopify/Product/10622982422815",
    "variant_gid": "gid://shopify/ProductVariant/52923834990879",
    "po_number": None,
    "source_name": "shopify_draft_order",
}


def _flag(label: str, value: object) -> None:
    print(f"{label}: {json.dumps(value, default=str)}")


def _merchant_channel_state() -> dict:
    """
    What the merchant side could actually do, in this environment.

    The point is the boundary, not the credential: a channel that would fall back to RESOLVE's
    settings is a channel that has already broken it, so `load_config` is asked what it sees.
    """
    state: dict = {"root_visible": bool(MERCHANT_CHANNEL_ROOT)}
    if not state["root_visible"]:
        state["error"] = "MERCHANT_CHANNEL_ROOT not set — the merchant-side package is not mounted"
        return state
    sys.path.insert(0, MERCHANT_CHANNEL_ROOT)
    from merchant_channel.config import ChannelConfigError, load_config

    try:
        config = load_config(dict(os.environ))
        state.update(
            {
                "loaded": True,
                "store_domain_configured": bool(config.store_domain),
                "api_version": config.api_version or None,
                "access_token_configured": config.access_token.configured,
                "linkage_secret_configured": config.linkage_secret.configured,
                "resolve_base_url_configured": bool(config.resolve_base_url),
                "expected_currency": config.expected_currency,
                "expected_shop_name": config.expected_shop_name or None,
                "channel_configured_end_to_end": config.configured,
            }
        )
    except ChannelConfigError as exc:
        # The refusal itself is the finding: RESOLVE's own credential names are present in this
        # container, and the channel will not run on them.
        state.update({"loaded": False, "refused": str(exc)[:200]})
    return state


async def _resolve_side() -> dict:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.routers import missions

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    try:
        async with engine.connect() as conn:
            orders = (await conn.execute(text("select count(*) from resolve_orders"))).scalar()
            linked = (
                await conn.execute(
                    text("select count(*) from resolve_orders where provider_order_reference is not null")
                )
            ).scalar()
            events = (await conn.execute(text("select count(*) from integration_events"))).scalar()
            identities = (
                await conn.execute(
                    text(
                        "select order_id, mission_id, product_id, currency, amount, "
                        "shopify_product_gid, shopify_variant_gid, shopify_store_domain, "
                        "provider_order_reference from resolve_orders order by order_id"
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()
    return {
        "payment_currency_constant": missions.PAYMENT_CURRENCY,
        "return_provider": settings.return_provider,
        "replacement_provider": settings.replacement_provider,
        "linkage_secret_configured": bool(settings.linkage_integration_secret),
        "resolve_shopify_credential_configured": bool(
            settings.shopify_client_id and settings.shopify_client_secret
        ),
        "resolve_store_domain": settings.shopify_store_domain,
        "resolve_api_version": settings.shopify_api_version,
        "resolve_orders": orders,
        "linked_orders": linked,
        "integration_events": events,
        "order_rows": [dict(row) for row in identities],
    }


async def _shopify_side() -> dict:
    client = get_shopify_client()
    client.validate_configuration()

    async def ask(document: str) -> dict:
        SENT.append(document.strip().split()[0])
        return await client.execute_graphql(document)

    scopes_payload = await ask(SCOPES)
    granted = sorted(
        scope.get("handle")
        for scope in ((scopes_payload.get("currentAppInstallation") or {}).get("accessScopes") or [])
    )
    store = await ask(SHOP_AND_CATALOG)
    orders_payload = await ask(ORDERS)

    shop = store.get("shop") or {}
    catalog = []
    for node in [e.get("node") or {} for e in ((store.get("products") or {}).get("edges") or [])]:
        # `ProductVariant.price` is a bare String in this schema version — measured, not assumed —
        # so the only currency a catalog price can be read with is the published money field beside
        # it. A number with no currency is not a price, and this audit will not treat it as one.
        published = ((node.get("priceRangeV2") or {}).get("minVariantPrice") or {})
        for variant in [
            e.get("node") or {} for e in ((node.get("variants") or {}).get("edges") or [])
        ]:
            catalog.append(
                {
                    "product_gid": node.get("id"),
                    "product_status": node.get("status"),
                    "variant_gid": variant.get("id"),
                    "price": variant.get("price"),
                    "published_price": published.get("amount"),
                    "published_currency": published.get("currencyCode"),
                    "available_for_sale": variant.get("availableForSale"),
                    "sku": variant.get("sku"),
                    "selected_options": [
                        (o.get("name"), o.get("value")) for o in (variant.get("selectedOptions") or [])
                    ],
                }
            )

    orders = [e.get("node") or {} for e in ((orders_payload.get("orders") or {}).get("edges") or [])]
    snapshot_1001 = None
    drift: list[str] = []
    returns = 0
    for order in orders:
        lines = [e.get("node") or {} for e in ((order.get("lineItems") or {}).get("edges") or [])]
        order_returns = (order.get("returns") or {}).get("edges") or []
        returns += len(order_returns)
        money = ((order.get("totalPriceSet") or {}).get("shopMoney") or {})
        observed = {
            "order_gid": order.get("id"),
            "name": order.get("name"),
            "currency": order.get("currencyCode"),
            "financial": order.get("displayFinancialStatus"),
            "fulfillment": order.get("displayFulfillmentStatus"),
            "return_status": order.get("returnStatus"),
            "line_quantity": lines[0].get("quantity") if lines else None,
            "product_gid": (lines[0].get("product") or {}).get("id") if lines else None,
            "variant_gid": (lines[0].get("variant") or {}).get("id") if lines else None,
            "po_number": order.get("poNumber"),
            "source_name": order.get("sourceName"),
            "total": f"{money.get('amount')} {money.get('currencyCode')}",
            "returns": len(order_returns),
            "created_at": order.get("createdAt"),
            "tags": order.get("tags"),
        }
        if observed["order_gid"] == BASELINE_1001["order_gid"]:
            snapshot_1001 = observed
            for key, expected in BASELINE_1001.items():
                if observed.get(key) != expected:
                    drift.append(f"{key}: baseline {expected!r} now {observed.get(key)!r}")
    return {
        "granted_scopes": granted,
        "write_orders_present": "write_orders" in granted,
        "forbidden_write_scopes_present": [
            scope
            for scope in (
                "write_orders",
                "write_products",
                "write_merchant_managed_fulfillment_orders",
                "write_fulfillments",
            )
            if scope in granted
        ],
        "shop_name": shop.get("name"),
        "store_domain": getattr(client, "store_domain", None) or os.environ.get("SHOPIFY_STORE_DOMAIN"),
        "shop_currency_code": shop.get("currencyCode"),
        "order_count": len(orders),
        "return_count": returns,
        "order_names": [order.get("name") for order in orders],
        "catalog": catalog,
        "order_1001": snapshot_1001,
        "order_1001_drift": drift,
    }


def _verdict(resolve: dict, shopify: dict, channel: dict) -> list[tuple[bool, str]]:
    catalog = [
        item
        for item in shopify["catalog"]
        if item["product_status"] == "ACTIVE" and item["available_for_sale"]
    ]
    inr_variants = [item for item in catalog if item.get("published_currency") == "INR"]
    return [
        (
            resolve["payment_currency_constant"] == "INR",
            f"RESOLVE payment currency is INR ({resolve['payment_currency_constant']})",
        ),
        (
            channel.get("expected_currency") == "INR",
            f"merchant channel expects INR (expected_currency={channel.get('expected_currency', 'not loaded')})",
        ),
        (
            shopify["shop_currency_code"] == "INR",
            f"connected store publishes INR (shop.currencyCode == {shopify['shop_currency_code']})",
        ),
        (
            bool(inr_variants),
            "an ACTIVE, available-for-sale variant is published in INR (measured through "
            f"`priceRangeV2.minVariantPrice.currencyCode`: {len(inr_variants)} of {len(catalog)} "
            "sellable variants)",
        ),
        (
            bool(channel.get("access_token_configured")),
            "merchant-side Shopify credential is configured (MERCHANT_SHOPIFY_ACCESS_TOKEN)",
        ),
        (
            bool(channel.get("linkage_secret_configured"))
            and resolve["linkage_secret_configured"],
            "linkage secret configured on both sides "
            f"(RESOLVE={resolve['linkage_secret_configured']}, "
            f"channel={channel.get('linkage_secret_configured', False)})",
        ),
        (
            not resolve["order_rows"]
            or any(row.get("shopify_variant_gid") for row in resolve["order_rows"]),
            "a RESOLVE order row already carries canonical Shopify Product/Variant identity",
        ),
        (
            not shopify["write_orders_present"],
            "RESOLVE still lacks write_orders",
        ),
        (resolve["return_provider"] == "none", "RETURN_PROVIDER is none"),
    ]


async def main() -> int:
    channel = _merchant_channel_state()
    resolve = await _resolve_side()
    shopify = await _shopify_side()

    print("=== 14C.7J PREFLIGHT (read-only) ===")
    print("RESOLVE:", json.dumps(resolve, indent=2, default=str))
    print("SHOPIFY:", json.dumps(shopify, indent=2, default=str))
    print("MERCHANT_CHANNEL:", json.dumps(channel, indent=2, default=str))

    checks = _verdict(resolve, shopify, channel)
    print("=== PREREQUISITES ===")
    for satisfied, label in checks:
        print(f"  [{'ok ' if satisfied else 'FAIL'}] {label}")
    unmet = [label for satisfied, label in checks if not satisfied]
    print("DOCUMENTS SENT:", json.dumps({"count": len(SENT), "kinds": sorted(set(SENT))}))
    if any(kind.lower().startswith("mutation") for kind in SENT):
        print("REFUSAL: a mutation was sent from a pre-flight script.")
        return 1
    if unmet:
        print(f"VERDICT: STOP BEFORE CREATE — {len(unmet)} prerequisite(s) unmet; first: {unmet[0]}")
        return 3
    print("VERDICT: all prerequisites satisfied")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print("PREFLIGHT FAILED:", redact(str(exc))[:400])
        raise SystemExit(2)
