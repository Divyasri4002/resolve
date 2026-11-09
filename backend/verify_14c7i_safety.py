"""
Task 14C.7I B10/B12 — read-only safety snapshot of the connected store and the live deployment.

Every document below is a `query`. Nothing here creates, completes, marks paid, cancels or edits
anything: the merchant-side channel built in 14C.7I is never pointed at this store, and this script
exists to prove the store is exactly as it was found.

Prints no credential, no token, no secret. Redacts defensively anyway.
"""
from __future__ import annotations

import asyncio
import json

from app.tools.shopify import get_shopify_client
from app.tools.shopify.sanitizer import redact

SENT: list[str] = []

SCOPES = """
query Verify14c7iScopes {
  currentAppInstallation { accessScopes { handle } }
}
"""

SHOP = """
query Verify14c7iShop {
  shop { name currencyCode }
}
"""

# Order #1001 as 14C.7G/14C.7H last measured it. The point of printing these is the comparison:
# if any value moves, something wrote to the store and the task's safety claim is false.
BASELINE = {
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

SNAPSHOT = """
query Verify14c7iSnapshot {
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
        note
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


def _kind(document: str) -> str:
    body = document.strip()
    return body.split()[0][:8]


async def _ask(client, document: str, variables=None) -> dict:
    SENT.append(_kind(document))
    return await client.execute_graphql(document, variables or {})


def _edges(container: dict, field: str) -> list[dict]:
    node = (container.get(field) or {}) if isinstance(container, dict) else {}
    return [e.get("node") or {} for e in (node.get("edges") or [])]


async def main() -> int:
    client = get_shopify_client()
    client.validate_configuration()

    scopes = (await _ask(client, SCOPES)).get("currentAppInstallation") or {}
    granted = sorted(scope.get("handle") for scope in (scopes.get("accessScopes") or []))
    print("GRANTED SCOPES:", json.dumps(granted))
    for forbidden in ("write_orders", "write_products", "write_merchant_managed_fulfillment_orders",
                      "write_fulfillments"):
        print(f"  {forbidden} present: {forbidden in granted}")

    shop = (await _ask(client, SHOP)).get("shop") or {}
    print("SHOP:", json.dumps({"name": shop.get("name"), "currencyCode": shop.get("currencyCode")}))

    data = await _ask(client, SNAPSHOT)
    orders = _edges(data, "orders")
    print("ORDER COUNT:", len(orders))
    drift: list[str] = []
    total_returns = 0
    for order in orders:
        lines = _edges(order, "lineItems")
        returns = _edges(order, "returns")
        total_returns += len(returns)
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
        }
        print("ORDER:", json.dumps({
            "name": observed["name"],
            "gid": observed["order_gid"],
            "total": f"{money.get('amount')} {money.get('currencyCode')}",
            "financial": observed["financial"],
            "fulfillment": observed["fulfillment"],
            "returnStatus": observed["return_status"],
            "returns": len(returns),
            "lines": len(lines),
            "line_quantity": observed["line_quantity"],
            "poNumber": observed["po_number"],
            "sourceName": observed["source_name"],
            "tags": order.get("tags"),
            "createdAt": order.get("createdAt"),
        }))
        if observed["order_gid"] == BASELINE["order_gid"]:
            for key, expected in BASELINE.items():
                if observed.get(key) != expected:
                    drift.append(f"{key}: baseline {expected!r} now {observed.get(key)!r}")
    print("TOTAL RETURNS ON ALL ORDERS:", total_returns)
    print("DRIFT VS BASELINE:", json.dumps(drift) if drift else "none — order #1001 is byte-identical")

    print("DOCUMENTS SENT:", json.dumps({"count": len(SENT), "kinds": sorted(set(SENT))}))
    if any(kind.lower().startswith("mutation") for kind in SENT):
        print("REFUSAL: a mutation was sent. This script must stay read-only.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:  # noqa: BLE001
        print("VERIFY FAILED:", redact(str(exc))[:400])
        raise SystemExit(2)
