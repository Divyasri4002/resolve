"""
Task 14C.7H — the currency and payment boundary, tested rather than described.

This store sells in USD (measured live: `shopify.currencyCode == "USD"` on
resolve-commerce-lab.myshopify.com) and this deployment's payment rail charges INR. The gap between
those two facts is the commercial boundary RESOLVE refuses to cross by imagination, and every test
here pins one side of it:

  * a merchant's price is carried in the merchant's own currency, end to end, unscaled;
  * payment refuses a currency it was not configured for, before any provider call and without
    changing state;
  * no amount or currency a client sends can become authoritative — not at mission creation, not at
    payment, not at the order record;
  * no exchange rate exists anywhere in this codebase, so "it converted" is not possible;
  * the refund ceiling is INR-denominated, and what makes that safe is the payment gate upstream —
    not a currency check inside the Authority Engine;
  * the Google Shopping INR flow, the Shopify-native USD discovery flow, and every historical order
    row are exactly as they were.

What this file does NOT do is fix the mismatch. STEP 3 of the task forbids it: choosing an FX source,
a rounding rule and a refund-currency policy is its own decision, not a side effect of a test.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
from decimal import Decimal

from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import _SCHEMA_UPGRADES
from app.integrations.order_linkage import OrderLinkageEvent
from app.models.db_models import MissionRecord, ResolveOrderRecord
from app.routers import missions as missions_module
from app.routers.missions import PAYMENT_CURRENCY, _canonical_refund_amount
from app.tools.shopify import linkage as linkage_module

from tests.conftest import SAMPLE_DISCOVERY_PRODUCTS
from tests.test_order_linkage_integration import _frontend_root  # noqa: F401
from tests.test_refund_execution import _checkout_signature, restart_env  # noqa: F401
from tests.test_shopify_discovery_identity import (  # noqa: F401
    _mission_with,
    _pay_and_create_order,
    _shopify_result,
    mock_razorpay,
)

STORE = "resolve-commerce-lab.myshopify.com"
PRODUCT_GID = "gid://shopify/Product/10622982422815"
# `app/routers/missions.py` → parents[1] is the `app` package itself.
APP_ROOT = pathlib.Path(missions_module.__file__).resolve().parents[1]
GOOGLE_PRODUCT = SAMPLE_DISCOVERY_PRODUCTS[0]  # ₹549.00 from Amazon.in
PROMPT = "I want the shampoo from my own store under ₹600."

# A name or an expression that would mean "RESOLVE converts money" — matched against code, never
# against prose, so a comment about not converting cannot satisfy it and a real rate could hide.
CONVERSION_SHAPE = re.compile(
    r"(?i)(currency|money|price|amount).{0,24}(convert|conversion|exchange|rate|fx)"
    r"|(convert|conversion|exchange|rate|fx).{0,24}(currency|money|price|amount)"
)
FX_HOST_SHAPE = re.compile(
    r"(?i)exchangerate|fixer\.io|openexchangerates|coinbase|forex|ndfx|api\.yahoo"
)


def _usd_result() -> object:
    """The same catalog item, priced the way the connected store actually prices it."""
    return _shopify_result(currency="USD", price="USD 410.00")


def _app_sources() -> list[pathlib.Path]:
    return [p for p in sorted(APP_ROOT.rglob("*.py")) if "__pycache__" not in p.parts]


async def _confirmed_usd_checkout(client, install_mock_discovery, mock_razorpay, email: str) -> str:
    """Discovery → selection → checkout confirmation, in the store's own currency. No payment yet."""
    mission_id = await _mission_with(client, install_mock_discovery, [_usd_result()], email)
    assert (
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
    ).status_code == 200
    assert (
        await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
    ).status_code == 200
    assert mock_razorpay["n"] == 0
    return mission_id


async def _confirmed_inr_checkout(client, install_mock_discovery, email: str) -> str:
    mission_id = await _mission_with(client, install_mock_discovery, [_shopify_result()], email)
    assert (
        await client.post(
            f"/missions/{mission_id}/shopping/select-product", json={"product_id": PRODUCT_GID}
        )
    ).status_code == 200
    assert (
        await client.post(f"/missions/{mission_id}/shopping/checkout/confirm")
    ).status_code == 200
    return mission_id


# --------------------------------------------------------------------------- #
# 1 + 4 + 5 — a merchant price in USD never becomes an INR amount
# --------------------------------------------------------------------------- #
async def test_a_usd_price_stays_usd_and_payment_refuses_instead_of_converting(
    restart_env, install_mock_discovery, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _confirmed_usd_checkout(
            client, install_mock_discovery, mock_razorpay, "usd-boundary@shop.test"
        )

        checkout = (await client.get(f"/missions/{mission_id}/shopping/checkout")).json()
        # Carried through untouched: the store's own code, Shopify's own number, unscaled.
        assert checkout["checkout"]["currency"] == "USD"
        assert checkout["checkout"]["subtotal"] == "410.00"
        assert checkout["checkout"]["product"]["shopify_store_domain"] == STORE

        refused = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        assert refused.status_code == 400, refused.text
        detail = refused.json()["detail"]
        assert "USD" in detail and "INR" in detail and "does not convert" in detail
        assert mock_razorpay["n"] == 0  # no Razorpay order was created, so nothing could be charged

        # The order record cannot be reached around the refusal either.
        order_attempt = await client.post(f"/missions/{mission_id}/order", json={})
        assert order_attempt.status_code == 400, order_attempt.text
        assert "payment is verified" in order_attempt.json()["detail"]

    async with test_session() as session:
        assert (await session.scalars(select(ResolveOrderRecord))).all() == []
        record = await session.get(MissionRecord, mission_id)
        assert (record.shopping_intent or {}).get("payment") is None, (
            "a currency refusal must not leave a payment state behind"
        )


async def test_the_currency_refusal_is_not_a_state_change(
    restart_env, install_mock_discovery, mock_razorpay
):
    make_client, _test_session = restart_env
    async with make_client() as client:
        mission_id = await _confirmed_usd_checkout(
            client, install_mock_discovery, mock_razorpay, "usd-repeat@shop.test"
        )
        before = (await client.get(f"/missions/{mission_id}/shopping/checkout")).json()

        first = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        second = await client.post(f"/missions/{mission_id}/shopping/payment/create-order", json={})
        assert first.status_code == second.status_code == 400
        assert first.json() == second.json()

        after = (await client.get(f"/missions/{mission_id}/shopping/checkout")).json()
        assert after == before, "a refusal must leave the checkout exactly as it found it"
        assert after["confirmed"] is True
        assert mock_razorpay["n"] == 0


# --------------------------------------------------------------------------- #
# 2 + 3 — nothing a client sends about money is believed
# --------------------------------------------------------------------------- #
async def test_a_client_cannot_inject_currency_or_budget_when_creating_a_mission(
    restart_env, install_mock_discovery
):
    make_client, test_session = restart_env
    async with make_client() as client:
        created = await client.post(
            "/missions",
            json={
                "mission_type": "shopping",
                "prompt": PROMPT,
                # The client states its own money framing. None of this is read.
                "shopping_intent": {
                    "raw_prompt": "override",
                    "currency": "USD",
                    "budget": "99999",
                },
            },
        )
        assert created.status_code == 200, created.text
        mission_id = created.json()["mission_id"]

    async with test_session() as session:
        intent = (await session.get(MissionRecord, mission_id)).shopping_intent
        assert intent["currency"] != "USD"
        assert str(intent["budget"]) != "99999"


async def test_a_client_cannot_override_the_amount_or_currency_at_payment(
    restart_env, install_mock_discovery, mock_razorpay
):
    make_client, _test_session = restart_env
    async with make_client() as client:
        mission_id = await _confirmed_inr_checkout(client, install_mock_discovery, "pay-money@shop.test")
        created = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order",
            json={
                "amount": 9999,
                "amount_paise": 1,
                "currency": "USD",
                "razorpay_order_id": "order_FABRICATED",
            },
        )
        assert created.status_code == 200, created.text
        payment = created.json()["payment"]
        assert payment["currency"] == "INR"
        assert str(payment["amount"]) == "410.00"
        assert payment["amount_paise"] == 41000
        assert payment["razorpay_order_id"] == "order_TEST0001"  # the server's own id
        assert mock_razorpay["n"] == 1


async def test_a_client_cannot_override_the_order_currency_or_amount(
    restart_env, install_mock_discovery, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _confirmed_inr_checkout(client, install_mock_discovery, "order-money@shop.test")
        created = await client.post(
            f"/missions/{mission_id}/shopping/payment/create-order", json={}
        )
        razorpay_order_id = created.json()["payment"]["razorpay_order_id"]
        payment_id = "pay_TESTFORGE"
        assert (
            await client.post(
                f"/missions/{mission_id}/shopping/payment/verify",
                json={
                    "razorpay_order_id": razorpay_order_id,
                    "razorpay_payment_id": payment_id,
                    "razorpay_signature": _checkout_signature(razorpay_order_id, payment_id),
                },
            )
        ).status_code == 200

        ordered = await client.post(
            f"/missions/{mission_id}/order",
            json={"currency": "USD", "amount": 1, "merchant": "Someone Else", "product_id": "x"},
        )
        assert ordered.status_code == 200, ordered.text
        assert ordered.json()["currency"] == "INR"
        assert ordered.json()["amount"] == "410.00"

    async with test_session() as session:
        row = (await session.scalars(select(ResolveOrderRecord))).one()
        assert row.currency == "INR"
        assert row.amount == Decimal("410.00")
        assert row.merchant != "Someone Else"


# --------------------------------------------------------------------------- #
# 6 — no FX anywhere: neither a name, nor a calculation, nor a host
# --------------------------------------------------------------------------- #
def test_no_exchange_rate_or_currency_conversion_exists_anywhere_in_the_backend():
    offending_names: list[str] = []
    offending_math: list[str] = []
    for path in _app_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if CONVERSION_SHAPE.search(node.name):
                    offending_names.append(f"{path.name}:{node.name}")
            elif isinstance(node, ast.Name):
                if CONVERSION_SHAPE.search(node.id):
                    offending_names.append(f"{path.name}:{node.id}")
            elif isinstance(node, ast.arg):
                if CONVERSION_SHAPE.search(node.arg):
                    offending_names.append(f"{path.name}:{node.arg}")
            elif isinstance(node, ast.Attribute):
                if CONVERSION_SHAPE.search(node.attr):
                    offending_names.append(f"{path.name}:{node.attr}")
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult | ast.Div):
                text = ast.unparse(node)
                # ×100 / ÷100 is unit arithmetic within one currency (rupees↔paise), the only
                # scaling this system performs. Anything else multiplying money is a rate.
                if CONVERSION_SHAPE.search(text) and not re.fullmatch(
                    r".*\b(amount|subtotal|price|value)\b.*\* *100.*", text
                ):
                    offending_math.append(f"{path.name}:{text}")
    assert offending_names == []
    assert offending_math == []

    literals: list[str] = []
    for path in _app_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals.extend(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    assert [text for text in literals if FX_HOST_SHAPE.search(text)] == []


def test_the_payment_currency_is_one_declared_constant_and_not_an_environment_setting():
    """
    Exactly one place decides what currency this deployment charges in, and it is code. `Settings`
    carries no currency field, so no `.env` can quietly re-stamp a store's price as another currency.
    """
    assert PAYMENT_CURRENCY == "INR"
    assert [name for name in Settings.model_fields if "currency" in name.lower()] == []
    assert [name for name in Settings.model_fields if "exchange" in name.lower()] == []


def test_the_order_row_is_bounded_to_one_currency_column_it_cannot_be_guessed_from():
    columns = {column.name: column for column in ResolveOrderRecord.__table__.columns}
    assert columns["currency"].nullable is False
    assert columns["currency"].default.arg == "INR"
    assert str(columns["amount"].type) == "NUMERIC(12, 2)"
    assert columns["amount"].nullable is False


# --------------------------------------------------------------------------- #
# 7 — the authority ceiling's unit is guaranteed upstream, and that is load-bearing
# --------------------------------------------------------------------------- #
async def test_the_refund_ceiling_is_inr_denominated_because_the_payment_gate_makes_it_so(
    restart_env, install_mock_discovery, mock_razorpay
):
    """
    `shopping_refund_max_inr` is compared against a bare amount: the engine is handed
    `(amount, paise, currency)` and the currency is carried, not checked. Today that is safe for
    exactly one reason — the payment gate refuses to create a Razorpay order in any other currency,
    so no order row can exist whose amount is not INR. This test pins both halves, because the
    second one is what the first depends on.
    """
    assert get_settings().shopping_refund_max_inr == Decimal("500")
    assert Settings.model_fields["shopping_refund_max_inr"].default == Decimal("500")

    gate = inspect.getsource(missions_module.create_shopping_payment_order)
    assert "if currency != PAYMENT_CURRENCY:" in gate
    # The paise math sits AFTER the gate, so a foreign currency is never even scaled.
    assert gate.index("amount_paise = int(") > gate.index("if currency != PAYMENT_CURRENCY:")

    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _confirmed_inr_checkout(
            client, install_mock_discovery, "ceiling@shop.test"
        )
        order = await _pay_and_create_order(client, mission_id)
        assert order["currency"] == "INR"

    async with test_session() as session:
        row = (await session.scalars(select(ResolveOrderRecord))).one()
        payment_data = (await session.get(MissionRecord, mission_id)).shopping_intent["payment"]

    amount, paise, currency = _canonical_refund_amount(payment_data, row)
    assert currency == PAYMENT_CURRENCY == "INR"
    assert amount == Decimal("410.00") and paise == 41000
    # `shopping_refund_max_inr` is compared against a bare number like this one, in this same unit.
    # The gate asserted above is the only reason that comparison is safe.


# --------------------------------------------------------------------------- #
# 8 + 9 — the two flows that exist today, both unchanged
# --------------------------------------------------------------------------- #
async def test_the_google_shopping_inr_flow_is_unchanged(
    restart_env, install_mock_discovery, mock_razorpay
):
    make_client, test_session = restart_env
    async with make_client() as client:
        mission_id = await _mission_with(
            client, install_mock_discovery, [GOOGLE_PRODUCT], "google-inr@shop.test"
        )
        assert (
            await client.post(
                f"/missions/{mission_id}/shopping/select-product",
                json={"product_id": GOOGLE_PRODUCT.product_id},
            )
        ).status_code == 200
        checkout = (await client.get(f"/missions/{mission_id}/shopping/checkout")).json()
        assert checkout["checkout"]["currency"] == "INR"
        assert Decimal(checkout["checkout"]["subtotal"]) == GOOGLE_PRODUCT.extracted_price

        order = await _pay_and_create_order(client, mission_id)
        assert order["currency"] == "INR"
        assert Decimal(order["amount"]) == GOOGLE_PRODUCT.extracted_price

    async with test_session() as session:
        row = (await session.scalars(select(ResolveOrderRecord))).one()
        assert row.currency == "INR"
        assert isinstance(row.amount, Decimal)
        # A SerpApi result has no Shopify identity, and never acquires one.
        assert row.shopify_product_gid is None
        assert row.shopify_variant_gid is None
        assert row.shopify_store_domain is None
        assert row.provider_order_reference is None


async def test_the_shopify_native_usd_discovery_flow_is_unchanged(
    restart_env, install_mock_discovery, mock_razorpay
):
    """Identity and price reading keep working in the store's own currency; only payment stops."""
    make_client, _test_session = restart_env
    async with make_client() as client:
        mission_id = await _confirmed_usd_checkout(
            client, install_mock_discovery, mock_razorpay, "usd-discovery@shop.test"
        )
        product = (await client.get(f"/missions/{mission_id}/shopping/checkout")).json()["checkout"][
            "product"
        ]
        assert product["shopify_product_gid"] == PRODUCT_GID
        assert product["shopify_store_domain"] == STORE
        assert product["currency"] == "USD"
        assert product["requires_variant_clarification"] is False


# --------------------------------------------------------------------------- #
# 10 — nothing historical is rewritten
# --------------------------------------------------------------------------- #
def test_no_historical_order_row_is_migrated_or_rewritten():
    upgraded = {
        table: {name for name, _ddl in columns} for table, columns in _SCHEMA_UPGRADES.items()
    }
    assert "currency" not in upgraded["resolve_orders"]
    assert "amount" not in upgraded["resolve_orders"]
    for table, columns in _SCHEMA_UPGRADES.items():
        for name, ddl in columns:
            # The DDL entries are additive column definitions; none may be an UPDATE
            # statement. Column NAMES may legitimately contain "UPDATE" (review_updated_at),
            # so match at statement position, not as a substring.
            assert not ddl.upper().lstrip().startswith("UPDATE"), f"{table}.{name}"

    database_source = (APP_ROOT / "database.py").read_text(encoding="utf-8")
    assert not [
        line
        for line in database_source.splitlines()
        if "UPDATE" in line.upper() and "currency" in line.lower()
    ]


# --------------------------------------------------------------------------- #
# 11 — the client shows the server's reason, and a pairing carries no money
# --------------------------------------------------------------------------- #
def test_the_frontend_never_converts_and_surfaces_the_servers_reason():
    root = _frontend_root()
    assert root is not None, (
        "the frontend scan needs the client sources: run the suite with "
        "-v <repo>/frontend:/frontend:ro -e FRONTEND_ROOT=/frontend"
    )
    sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".next" not in path.parts
    }
    money_files = {
        name: text
        for name, text in sources.items()
        if name.startswith(("components/", "lib/")) and "currency" in text
    }
    assert money_files, "the money-bearing client files should be visible to this scan"
    for name, text in money_files.items():
        assert not FX_HOST_SHAPE.search(text), name
        # Display formatting only: an amount is never multiplied or divided by a number.
        assert not re.search(
            r"(amount|subtotal|price|value|paise)\w*\s*[*/]\s*[0-9]", text
        ), name

    assert "always server-derived" in sources["lib/razorpay.ts"]
    page = sources["app/workspace/page.tsx"]
    assert "data.payment.amount_paise" in page, "the client passes the server's own minor units"
    # The refusal reason the shopper reads is the server's own `detail`. It used to be lifted inside
    # this one page at eighteen call sites; it is now lifted once, at the seam every call passes
    # through, and the page shows whatever that seam threw. Same guarantee, checked where it lives —
    # and a call site that invents its own reason can no longer slip past it.
    seam = sources["lib/api.ts"]
    assert "?.detail" in seam, "the seam must read the server's refusal reason off the response"
    assert "messageOf(err)" in page, "the reason shown is the message the seam threw"


def test_a_signed_pairing_can_never_carry_money_or_currency():
    """
    Currency edition of the linkage boundary: the event names two references and nothing else, so
    admitting a pairing can never also restate what was paid, how much, or in which currency.
    """
    fields = set(OrderLinkageEvent.model_fields)
    assert fields == {
        "event_id",
        "event_type",
        "shopify_order_id",
        "issued_at",
        "resolve_order_id",
        "mission_id",
        "source",
    }
    assert OrderLinkageEvent.model_config.get("extra") == "forbid"
    assert not fields & {"amount", "currency", "price", "merchant", "title"}

    writer = inspect.getsource(linkage_module)
    for money_column in ("amount", "currency"):
        assert f"order.{money_column} =" not in writer, money_column
