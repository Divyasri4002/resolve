"""
The boundary itself: what the channel must not be able to touch, see, or say.

Phase B's whole claim is that the merchant-owned purchase channel sits outside RESOLVE's Shopify
write boundary. A claim like that is only worth what its checks are worth, so these tests look at
imports, environment names, storage contents and printed values — not at intentions.

Nothing here is about whether the channel works. It is about whether it could quietly become the
thing 14C.7G and 14C.7H refused to be.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sqlite3
from dataclasses import fields

import pytest
from conftest import ORDER_ID, PRODUCT_GID, SECRET, TOKEN, handoff, make_config
from conftest import FakeResolve

from merchant_channel import documents as docs
from merchant_channel.channel import PurchaseHandoff
from merchant_channel.config import RESOLVE_OWNED_ENV, ChannelConfigError, Secret, load_config
from merchant_channel.correlation import correlation_id
from merchant_channel.gateway import ShopifyAdminGateway
from merchant_channel.linkage import LINKAGE_ROUTE, SIGNATURE_HEADER, build_event, serialize_event
from merchant_channel.outbox import Outbox

PACKAGE = pathlib.Path(docs.__file__).resolve().parent


def _module_names() -> list[str]:
    return sorted(path.name for path in PACKAGE.glob("*.py"))


# ------------------------------------------------------------------ import isolation
def test_the_channel_never_imports_resolve():
    """
    One rule, checked across every module: nothing from the `app` package.

    If this import existed, the channel could read RESOLVE's Shopify token — a different app with
    different scopes — and the boundary this package exists to hold would be a comment.
    """
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        offending = [
            name
            for name in imported
            if name == "app" or name.startswith("app.") or name.endswith(".app")
        ]
        assert offending == [], f"{path.name} imports {offending}"
    assert _module_names(), "the package should have modules to scan"


def test_the_channel_reads_only_its_own_environment_names():
    environ = {
        "MERCHANT_SHOPIFY_STORE_DOMAIN": "somewhere.myshopify.com",
        "MERCHANT_SHOPIFY_API_VERSION": "2026-07",
        "MERCHANT_SHOPIFY_ACCESS_TOKEN": "shpua_merchant_side",
        "MERCHANT_LINKAGE_SECRET": "shared",
        "MERCHANT_RESOLVE_BASE_URL": "http://resolve.test",
    }
    config = load_config(environ)
    assert config.store_domain == "somewhere.myshopify.com"
    assert config.expected_currency == "INR"  # the payment currency is RESOLVE's decision, not a guess


def test_the_channel_refuses_to_run_on_a_resolve_credential():
    environ = {"SHOPIFY_ACCESS_TOKEN": "shpua_resolve_side", "MERCHANT_SHOPIFY_STORE_DOMAIN": "x"}
    with pytest.raises(ChannelConfigError) as refused:
        load_config(environ)
    assert "must not read RESOLVE's credentials" in str(refused.value)


def test_every_resolve_owned_name_is_a_secret_belonging_to_the_other_side():
    assert "LINKAGE_INTEGRATION_SECRET" in RESOLVE_OWNED_ENV
    assert "SHOPIFY_ACCESS_TOKEN" in RESOLVE_OWNED_ENV
    assert "RAZORPAY_KEY_SECRET" in RESOLVE_OWNED_ENV


def test_an_unconfigured_channel_refuses_instead_of_defaulting():
    config = make_config(store_domain="", access_token=Secret(""))
    with pytest.raises(ChannelConfigError):
        ShopifyAdminGateway(config)
    with pytest.raises(ChannelConfigError):
        ShopifyAdminGateway(make_config(access_token=Secret("")))


# ------------------------------------------------------------------ credentials never surface
def test_a_secret_never_renders_and_the_gateway_masks_it_everywhere_it_could_appear():
    secret = Secret(TOKEN)
    assert TOKEN not in repr(secret)
    assert TOKEN not in str(secret)
    assert "configured" in repr(secret) or "***" in repr(secret)

    gateway = ShopifyAdminGateway(make_config())
    assert TOKEN not in repr(gateway._config)
    assert TOKEN not in gateway._mask(f"header failed: {TOKEN}")
    assert gateway._mask("nothing to see") == "nothing to see"


def test_the_channel_config_repr_is_safe():
    text = repr(make_config())
    assert SECRET not in text
    assert TOKEN not in text
    assert "access_token='set'" in text and "linkage_secret='set'" in text


# ------------------------------------------------------------------ the pairing vocabulary
def test_the_event_is_the_existing_contract_and_nothing_more():
    event = build_event(
        correlation_id="RESOLVE-order_x-000000000000",
        shopify_order_id="gid://shopify/Order/900000000000001",
        resolve_order_id="order_x",
        issued_at="2026-09-28T12:00:00+00:00",
        source="merchant-purchase-channel",
    )
    assert set(event) == {
        "event_id",
        "event_type",
        "shopify_order_id",
        "issued_at",
        "resolve_order_id",
        "source",
    }
    assert event["event_type"] == "resolve.order_linkage.v1"
    assert serialize_event(event).startswith(b"{")
    assert json.loads(serialize_event(event)) == event


@pytest.mark.parametrize(
    "order_id",
    [
        "gid://shopify/Product/10622982422815",
        "gid://shopify/ProductVariant/52923834990879",
        "gid://shopify/DraftOrder/18989131727135",
        "gid://shopify/Order/abc123",
        "#1001",
        "",
    ],
    ids=["product-gid", "variant-gid", "draft-order-gid", "non-numeric", "name", "empty"],
)
def test_only_a_real_shopify_order_gid_can_be_claimed_as_a_pairing(order_id):
    from merchant_channel.linkage import LinkageContractError

    with pytest.raises(LinkageContractError):
        build_event(
            correlation_id="RESOLVE-order_y-000000000000",
            shopify_order_id=order_id,
            resolve_order_id="order_y",
            issued_at="2026-09-28T12:00:00+00:00",
            source="merchant-purchase-channel",
        )


def test_the_channel_targets_the_one_existing_route_and_invents_no_second_door():
    assert LINKAGE_ROUTE == "/integrations/shopify/order-linkage"
    assert SIGNATURE_HEADER == "x-resolve-signature"


# ------------------------------------------------------------------ no PII, by shape
def test_a_purchase_handoff_cannot_carry_a_customer():
    names = {field.name for field in fields(PurchaseHandoff)}
    assert names == {
        "resolve_order_id",
        "shopify_product_gid",
        "shopify_variant_gid",
        "quantity",
        "currency",
    }
    for forbidden in ("email", "phone", "name", "address", "customer", "user_id", "card"):
        assert not any(forbidden in name for name in names), forbidden


def test_the_stamped_correlation_is_a_reference_not_a_person():
    correlation = correlation_id(handoff())
    assert correlation.startswith("RESOLVE-")
    assert ORDER_ID in correlation
    assert "@" not in correlation and "+" not in correlation


@pytest.mark.parametrize(
    "document",
    [docs.DRAFT_ORDER_CREATE, docs.DRAFT_ORDER_COMPLETE, docs.READ_BACK_BY_CORRELATION, docs.SHOP_IDENTITY],
    ids=["create", "complete", "read-back", "shop"],
)
def test_no_customer_field_appears_in_any_channel_document(document):
    for forbidden in (
        "email",
        "phone",
        "billingAddress",
        "shippingAddress",
        "customer",
        "statusPageUrl",
        "landingPageUrl",
        "referrerUrl",
        "clientIp",
    ):
        assert forbidden not in document, forbidden


def test_the_channel_reads_only_what_a_pairing_needs():
    """
    `po_number:` filters; `custom_attribute:` does not — both measured live on 2026-07, which is why
    the read-back uses the field that can answer 0 / 1 / many deterministically.
    """
    data = docs.parse_read_back(
        {
            "orders": {
                "edges": [
                    {
                        "node": {
                            "id": "gid://shopify/Order/91",
                            "name": "#1091",
                            "poNumber": "RESOLVE-order_z-000000000000",
                            "currencyCode": "INR",
                            "sourceName": "merchant-purchase-channel",
                            "displayFinancialStatus": "PENDING",
                            "createdAt": "2026-09-28T00:00:00Z",
                            "lineItems": {
                                "edges": [
                                    {
                                        "node": {
                                            "id": "gid://shopify/LineItem/1",
                                            "quantity": 1,
                                            "product": {"id": PRODUCT_GID},
                                            "variant": {
                                                "id": "gid://shopify/ProductVariant/52923834990879"
                                            },
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }
    )
    assert len(data) == 1
    assert data[0].po_number == "RESOLVE-order_z-000000000000"
    assert data[0].lines[0].product_id == PRODUCT_GID
    assert data[0].financial_status == "PENDING"


def test_a_completed_draft_is_read_as_its_order_not_as_the_draft():
    """
    A DraftOrder GID in a linkage event would be an order id that is not an order.

    `draftOrderComplete` answers with the draft, whose `order` field is the purchase — so the parser
    goes one level down, and refuses to hand back the draft's own id.
    """
    order, errors = docs.parse_complete(
        {
            "draftOrderComplete": {
                "draftOrder": {
                    "id": "gid://shopify/DraftOrder/18989131727135",
                    "order": {
                        "id": "gid://shopify/Order/900000000000001",
                        "name": "#1001",
                        "poNumber": "RESOLVE-order_w-000000000000",
                        "currencyCode": "INR",
                        "lineItems": {"edges": []},
                    },
                },
                "userErrors": [],
            }
        }
    )
    assert errors == []
    assert order.order_id == "gid://shopify/Order/900000000000001"

    missing, _ = docs.parse_complete({"draftOrderComplete": {"draftOrder": {"id": "gid://shopify/DraftOrder/1"}, "userErrors": []}})
    assert missing is None


# ------------------------------------------------------------------ the channel's own storage
async def test_the_outbox_stores_no_credential_no_signature_and_nothing_that_could_be_replayed(outbox):
    from merchant_channel.channel import PurchaseChannel

    resolve = FakeResolve()
    channel = PurchaseChannel(make_config(), _RecordingStore(), outbox, http_client=resolve)
    await channel.fulfil(handoff())
    raw = sqlite3.connect(outbox._path).execute(
        "SELECT correlation_id, resolve_order_id, payload_hash, status, reconciliation_state, "
        "linkage, external_order_id, event_id, event_body, attempted_at, resolved_at, "
        "failure_reason FROM purchase_outbox"
    ).fetchall()
    text = json.dumps([[str(cell) for cell in row] for row in raw])
    assert SECRET not in text
    assert TOKEN not in text
    # The pairing it signed is its own record; the signature that wrapped it is not.
    assert "x-resolve-signature" not in text
    assert resolve.requests, "this test is only meaningful if a delivery actually happened"
    assert resolve.requests[0]["headers"][SIGNATURE_HEADER] not in text


def test_an_outbox_row_renders_without_its_signed_body():
    from merchant_channel.outbox import Record

    record = Record(
        "RESOLVE-order_v-000000000000",
        "order_v",
        "0" * 16,
        "CONFIRMED",
        "matched",
        "SENT",
        "gid://shopify/Order/9",
        "evt-abc",
        json.dumps({"event_id": "evt-abc", "source": SECRET}),
        "2026-09-28T00:00:00+00:00",
        None,
        None,
    )
    assert SECRET not in repr(record)


# ------------------------------------------------------------------ helpers
class _RecordingStore:
    def __init__(self) -> None:
        self.created = []
        self.orders = {}

    async def create_order(self, **kwargs):
        from merchant_channel.documents import MerchantOrder, OrderLine

        self.created.append(kwargs)
        order = MerchantOrder(
            order_id="gid://shopify/Order/900000000000007",
            name="#1007",
            po_number=kwargs["correlation_id"],
            currency="INR",
            source_name="merchant-purchase-channel",
            financial_status="PENDING",
            created_at="2026-09-28T00:00:00Z",
            lines=(
                OrderLine(
                    line_item_id="gid://shopify/LineItem/7",
                    product_id=PRODUCT_GID,
                    variant_id="gid://shopify/ProductVariant/52923834990879",
                    quantity=kwargs["quantity"],
                ),
            ),
        )
        # A store that forgets its own orders would let the channel's read-back rule go untested.
        self.orders[kwargs["correlation_id"]] = [order]
        return order

    async def read_back(self, correlation_id):
        return list(self.orders.get(correlation_id, []))

    async def shop_identity(self):
        return "RESOLVE Commerce Lab", "USD"
