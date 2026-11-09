"""
Task 14B.3 — Shopify READ-ONLY connectivity verification (manual, live).

Run from the backend directory (or anywhere):

    python backend/verify_shopify_read.py

What it does, and nothing more: authenticate with the client-credentials grant, run three
READ queries (products, orders, returns) against the Admin GraphQL API, and confirm that the
real Dev-Store test data is visible:

    product  "Head & Shoulders Anti-Hairfall Shampoo 360ml"
    order    #1001 — expected paid, fulfilled, quantity 1, total approximately ₹410

The product name, order name, and the ~₹410 figure are VERIFICATION TARGETS only — they are
never stored or treated as authoritative application data, and the actual price comes from
whatever Shopify reports.

The script prints only safe fields. It never prints the client secret, the access token,
authorization headers, cookies, or the environment. Exit code 0 = verification succeeded;
2 = configuration/credentials missing (the script says which variables to populate, never
their values); any other non-zero = a connectivity or data problem, reported readably.
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402  (pydantic-settings ships this dependency)

# Load backend/.env explicitly so the script behaves the same from any working directory.
load_dotenv(BASE_DIR / ".env")

from app.tools.shopify import (  # noqa: E402
    ShopifyConfigError,
    ShopifyError,
    ShopifyGraphQLError,
    get_shopify_client,
)
from app.tools.shopify.sanitizer import redact  # noqa: E402

# Verification targets — NOT authoritative data. Prices/totals below are compared against
# what Shopify actually reports; nothing here is written anywhere.
TARGET_PRODUCT_TITLE = "Head & Shoulders Anti-Hairfall Shampoo 360ml"
TARGET_ORDER_NAME = "#1001"
EXPECTED_TOTAL_INR = Decimal("410")
TOTAL_TOLERANCE_INR = Decimal("15")  # "approximately INR 410"
PAID_STATUSES = {"paid", "partially_paid"}
FULFILLED_STATUSES = {"fulfilled"}


def _line(label: str, ok: bool | None, detail: str = "") -> bool:
    mark = {True: "SUCCESS", False: "FAILED", None: "SKIPPED"}[ok]
    print(f"{label}: {mark}" + (f" — {redact(detail)}" if detail else ""))
    return bool(ok)


async def run_checks() -> int:
    try:
        client = get_shopify_client()
        client.validate_configuration()  # fail before any network call if config is absent
    except ShopifyConfigError as exc:
        _line("Shopify configuration", False, str(exc))
        print(
            "\nThis machine has no Shopify credentials yet. Populate these environment "
            "variables (e.g. in backend/.env) — never paste their values into chat, code, "
            "or logs:\n  SHOPIFY_CLIENT_ID\n  SHOPIFY_CLIENT_SECRET\n"
            "(SHOPIFY_STORE_DOMAIN already defaults to resolve-commerce-lab.myshopify.com.)"
        )
        return 2

    print(f"Store domain: {client.store_domain}")
    print(f"Admin API version: {client.api_version}")

    try:
        await client.obtain_access_token()
    except ShopifyError as exc:
        _line("Shopify authentication", False, exc.message)
        return 1
    _line("Shopify authentication", True, "client-credentials token held in memory only")

    # ---- products ----------------------------------------------------------
    try:
        products = await client.read_products(first=100)
    except ShopifyError as exc:
        _line("API connectivity (products query)", False, exc.message)
        return 1
    _line("API connectivity (products query)", True)
    print(f"products returned: {len(products)}")
    product_found = None
    for product in products:
        if product.title.strip().casefold() == TARGET_PRODUCT_TITLE.casefold():
            product_found = product
            print(f"product title: {product.title}")
            print(f"product id: {product.product_id}")
            if product.price is not None:
                print(f"product price (as reported by Shopify): {product.price} {product.currency}")
    _line(
        f"test product found ({TARGET_PRODUCT_TITLE})",
        product_found is not None,
        "" if product_found else "not present in the products read",
    )

    # ---- orders ------------------------------------------------------------
    # An Order-read failure is reported but does NOT abort the run: the returns check is a
    # separate capability and the script's job is to report every boundary it hit.
    try:
        orders = await client.read_orders(first=25)
    except ShopifyError as exc:
        _line("API connectivity (orders query)", False, exc.message)
        orders = None
    checks_ok = product_found is not None
    if orders is None:
        _line(f"test order {TARGET_ORDER_NAME} checks", None, "orders read was refused")
        checks_ok = False
    else:
        _line("API connectivity (orders query)", True)
        print(f"orders returned: {len(orders)}")
        order_found = next((o for o in orders if o.name.strip() == TARGET_ORDER_NAME), None)
        if order_found is None:
            _line(
                f"test order {TARGET_ORDER_NAME} found", False,
                "not present in the orders read",
            )
            checks_ok = False
        else:
            print(f"order name: {order_found.name}")
            print(f"order id: {order_found.order_id}")
            print(f"fulfillment status: {order_found.fulfillment_status}")
            _line(f"test order {TARGET_ORDER_NAME} found", True)
            _line(
                "order is paid",
                (order_found.financial_status or "").casefold() in PAID_STATUSES,
                f"financial status: {order_found.financial_status}",
            )
            _line(
                "order is fulfilled",
                (order_found.fulfillment_status or "").casefold() in FULFILLED_STATUSES,
                f"fulfillment status: {order_found.fulfillment_status}",
            )
            matching_items = [
                item for item in order_found.line_items
                if TARGET_PRODUCT_TITLE.casefold() in item.title.casefold()
            ]
            for item in matching_items:
                print(f"line-item title: {item.title} · quantity {item.quantity}")
            quantity_ok = len(matching_items) == 1 and matching_items[0].quantity == 1
            _line(
                f"line item is exactly quantity 1 of {TARGET_PRODUCT_TITLE}",
                quantity_ok,
                f"{len(matching_items)} matching line item(s)",
            )
            total_ok = (
                order_found.total is not None
                and abs(order_found.total - EXPECTED_TOTAL_INR) <= TOTAL_TOLERANCE_INR
            )
            _line(
                f"order total approximately {EXPECTED_TOTAL_INR} (target from the task brief)",
                total_ok,
                f"as reported by Shopify: {order_found.total} {order_found.currency or ''}".strip(),
            )
            checks_ok = checks_ok and all(
                [
                    (order_found.financial_status or "").casefold() in PAID_STATUSES,
                    (order_found.fulfillment_status or "").casefold() in FULFILLED_STATUSES,
                    quantity_ok,
                    total_ok,
                ]
            )

    # ---- returns (read attempt; READ ONLY, nothing is ever created) ---------
    returns_status: bool | None = None
    try:
        returns = await client.read_returns(first=10)
        returns_status = True
        print(f"returns returned: {len(returns)}")
        for ret in returns[:5]:
            print(f"return {ret.return_id} · status {ret.status} · order {ret.order_name}")
    except ShopifyGraphQLError as exc:
        returns_status = False
        print(f"return query status: {redact(exc.message)}")
    except ShopifyError as exc:
        returns_status = False
        print(f"return query status: {redact(exc.message)}")
    _line("returns read access (read_returns scope)", returns_status)

    ok = checks_ok and returns_status is not None
    _line("Shopify read-only verification", ok)
    return 0 if ok else 1


def main() -> None:
    try:
        code = asyncio.run(run_checks())
    except KeyboardInterrupt:
        print("Interrupted.")
        code = 130
    except Exception as exc:  # last-resort guard: never leak an unredacted traceback
        print(f"UNEXPECTED ERROR: {redact(str(exc))}")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
