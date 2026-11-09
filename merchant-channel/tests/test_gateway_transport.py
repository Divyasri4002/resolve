"""
The channel's Shopify gateway, exercised through an injected transport.

Why this file exists: the gateway is the one module in the package that would talk to a merchant's
store, and in this environment there is no merchant credential — the task forbids asking for one, and
B10 keeps the channel away from a real order regardless. Mocked transport is therefore the honest way
to test the parts that are facts about *our* request, not Shopify's: which URL, which header, which
document, which variables, and — most importantly — which failure means "nothing was created" versus
"we cannot tell".

The documents themselves are named and typed against the live 2026-07 schema (introspected read-only
in this task), so a field that does not exist would fail here the same way it would fail upstream.
"""
from __future__ import annotations

import json

import httpx
import pytest
from conftest import TOKEN, make_config

from merchant_channel.gateway import (
    GatewayDefiniteFailure,
    GatewayOutcomeUnknown,
    ShopifyAdminGateway,
)

DRAFT_ORDER_GID = "gid://shopify/DraftOrder/18989131727135"
CREATED_ORDER = {
    "id": "gid://shopify/Order/900000000000001",
    "name": "#1091",
    "poNumber": "RESOLVE-order_t-000000000000",
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
                    "product": {"id": "gid://shopify/Product/10622982422815"},
                    "variant": {"id": "gid://shopify/ProductVariant/52923834990879"},
                }
            }
        ]
    },
}


def _gateway(responses) -> tuple[ShopifyAdminGateway, list[dict]]:
    """A gateway whose transport records every request and answers from `responses` in order."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append({"headers": dict(request.headers), "body": body, "url": str(request.url)})
        answer = responses[len(seen) - 1] if len(seen) <= len(responses) else responses[-1]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json=answer)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ShopifyAdminGateway(make_config(), client=client), seen


async def test_the_request_shape_is_its_own_app_at_its_own_store():
    gateway, seen = _gateway(
        [
            {"data": {"draftOrderCreate": {"draftOrder": {"id": DRAFT_ORDER_GID}, "userErrors": []}}},
            {
                "data": {
                    "draftOrderComplete": {
                        "draftOrder": {"id": DRAFT_ORDER_GID, "order": CREATED_ORDER},
                        "userErrors": [],
                    }
                }
            },
        ]
    )
    order = await gateway.create_order(
        correlation_id="RESOLVE-order_t-000000000000",
        variant_gid="gid://shopify/ProductVariant/52923834990879",
        quantity=1,
    )
    assert order.order_id == CREATED_ORDER["id"]
    assert order.lines[0].product_id == "gid://shopify/Product/10622982422815"

    assert seen[0]["url"] == (
        "https://resolve-commerce-lab.myshopify.com/admin/api/2026-07/graphql.json"
    )
    assert seen[0]["headers"]["x-shopify-access-token"] == TOKEN
    assert seen[0]["headers"]["content-type"] == "application/json"
    # create then complete, and the completion is pointed at the draft the create named.
    assert "draftOrderCreate" in seen[0]["body"]["query"]
    assert "draftOrderComplete" in seen[1]["body"]["query"]
    assert seen[1]["body"]["variables"] == {"id": DRAFT_ORDER_GID}
    assert seen[0]["body"]["variables"]["input"] == {
        "poNumber": "RESOLVE-order_t-000000000000",
        "lineItems": [
            {"variantId": "gid://shopify/ProductVariant/52923834990879", "quantity": 1}
        ],
    }


async def test_a_read_back_asks_shopify_by_the_correlation_it_stamped():
    gateway, seen = _gateway([{"data": {"orders": {"edges": [{"node": CREATED_ORDER}]}}}])
    orders = await gateway.read_back("RESOLVE-order_t-000000000000")
    assert len(orders) == 1
    assert seen[0]["body"]["variables"] == {"query": "po_number:RESOLVE-order_t-000000000000"}


async def test_the_shop_identity_read_is_a_query_and_not_a_write():
    gateway, seen = _gateway([{"data": {"shop": {"name": "RESOLVE Commerce Lab", "currencyCode": "USD"}}}])
    assert await gateway.shop_identity() == ("RESOLVE Commerce Lab", "USD")
    assert seen[0]["body"]["query"].strip().startswith("query ")


@pytest.mark.parametrize(
    "status", [401, 403, 422], ids=["unauthorized", "forbidden", "unprocessable"]
)
async def test_a_refusal_before_the_mutation_is_definite_not_unknown(status):
    """
    Nothing can exist if Shopify never accepted the request.

    Getting this backwards would leave the channel permanently stuck on an order that was never made;
    getting it backwards the other way is the duplicate order.
    """
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={}))
    )
    gateway = ShopifyAdminGateway(make_config(), client=client)
    with pytest.raises(GatewayDefiniteFailure):
        await gateway.read_back("RESOLVE-order_t-000000000000")


@pytest.mark.parametrize("status", [429, 500, 503], ids=["rate-limited", "server", "unavailable"])
async def test_an_answer_we_cannot_trust_is_never_read_as_a_failure(status):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={}))
    )
    gateway = ShopifyAdminGateway(make_config(), client=client)
    with pytest.raises(GatewayOutcomeUnknown):
        await gateway.read_back("RESOLVE-order_t-000000000000")


async def test_a_timeout_is_unknown_and_says_nothing_about_the_order():
    gateway, _seen = _gateway([httpx.ReadTimeout("the answer never arrived")])
    with pytest.raises(GatewayOutcomeUnknown) as refused:
        await gateway.create_order(
            correlation_id="RESOLVE-order_t-000000000000",
            variant_gid="gid://shopify/ProductVariant/52923834990879",
            quantity=1,
        )
    assert "ReadTimeout" in str(refused.value)


async def test_shopify_saying_no_is_a_refusal_we_can_act_on():
    gateway, _seen = _gateway(
        [
            {
                "data": {
                    "draftOrderCreate": {
                        "draftOrder": None,
                        "userErrors": [{"field": "lineItems", "message": "variant not found"}],
                    }
                }
            }
        ]
    )
    with pytest.raises(GatewayDefiniteFailure) as refused:
        await gateway.create_order(
            correlation_id="RESOLVE-order_t-000000000000",
            variant_gid="gid://shopify/ProductVariant/52923834990879",
            quantity=1,
        )
    assert "variant not found" in str(refused.value)


async def test_a_completion_that_names_no_order_is_unknown_not_absent():
    gateway, _seen = _gateway(
        [
            {"data": {"draftOrderCreate": {"draftOrder": {"id": DRAFT_ORDER_GID}, "userErrors": []}}},
            {"data": {"draftOrderComplete": {"draftOrder": {"id": DRAFT_ORDER_GID, "order": None}, "userErrors": []}}},
        ]
    )
    with pytest.raises(GatewayOutcomeUnknown):
        await gateway.create_order(
            correlation_id="RESOLVE-order_t-000000000000",
            variant_gid="gid://shopify/ProductVariant/52923834990879",
            quantity=1,
        )


async def test_the_credential_never_appears_in_an_error_this_module_raises():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400, json={"errors": [{"message": f"bad token {TOKEN}"}]}
            )
        )
    )
    gateway = ShopifyAdminGateway(make_config(), client=client)
    with pytest.raises(GatewayDefiniteFailure) as refused:
        await gateway.read_back("RESOLVE-order_t-000000000000")
    assert TOKEN not in str(refused.value)


def test_a_gateway_without_a_credential_says_so_instead_of_trying_anyway():
    from merchant_channel.config import ChannelConfigError, Secret

    with pytest.raises(ChannelConfigError):
        ShopifyAdminGateway(make_config(access_token=Secret("")))


async def test_the_channel_keeps_its_write_capability_and_borrows_no_resolves_boundary():
    """
    Task 14C.7N put a document-kind gate on RESOLVE's read-only client and deliberately stopped at
    the package edge. This channel buys through a merchant-owned app with merchant-owned write
    scopes; a global mutation ban here would not make anything safer, it would only make the channel
    unable to do the one job it exists for, while RESOLVE's own boundary stayed exactly as strong.

    Checked both ways: the create documents are still mutations and still go out through the
    transport, and no module in this package mentions RESOLVE's client, its gate, or its violation.
    """
    from pathlib import Path

    from merchant_channel import documents

    assert documents.DRAFT_ORDER_CREATE.lstrip().startswith("mutation ")
    assert documents.DRAFT_ORDER_COMPLETE.lstrip().startswith("mutation ")
    for path in sorted(Path(__file__).resolve().parents[1].joinpath("merchant_channel").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for foreign in ("document_gate", "ShopifyReadOnlyViolation", "ShopifyReadOnlyClient"):
            assert foreign not in source, (path.name, foreign)

    order_json = {
        "id": "gid://shopify/Order/900000000000002",
        "name": "#1092",
        "poNumber": "RESOLVE-order_t-000000000000",
        "currencyCode": "INR",
        "lineItems": {"edges": []},
    }
    gateway, seen = _gateway(
        [
            {"data": {"draftOrderCreate": {"draftOrder": {"id": DRAFT_ORDER_GID}, "userErrors": []}}},
            {
                "data": {
                    "draftOrderComplete": {
                        "draftOrder": {"id": DRAFT_ORDER_GID, "order": order_json},
                        "userErrors": [],
                    }
                }
            },
        ]
    )
    assert (
        await gateway.create_order(
            correlation_id="RESOLVE-order_t-000000000000",
            variant_gid="gid://shopify/ProductVariant/52923834990879",
            quantity=1,
        )
    ).order_id == order_json["id"]
    assert len(seen) == 2
    assert "draftOrderCreate" in seen[0]["body"]["query"]
    assert "draftOrderComplete" in seen[1]["body"]["query"]
