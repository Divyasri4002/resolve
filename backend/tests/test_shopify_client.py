"""
Tests for Task 14B.3: the read-only Shopify client.

Hermetic by construction: every HTTP exchange runs through httpx.MockTransport — the suite
NEVER contacts Shopify or any external host. The contract kept honest here is: authenticate
with client-credentials, read and normalize products/orders/returns, fail with typed
credential-free errors — and never let a secret or an access token into a log line, an
exception message, or a returned value.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

import httpx
import pytest

from app.tools.shopify import (
    ShopifyAuthError,
    ShopifyConfigError,
    ShopifyError,
    ShopifyGraphQLError,
    ShopifyMalformedResponseError,
    ShopifyOrder,
    ShopifyProduct,
    ShopifyRateLimitError,
    ShopifyReadOnlyClient,
    ShopifyReturn,
    get_shopify_client,
)
from app.tools.shopify.client import (  # query text is asserted, never logged outward
    _FULFILLMENT_LINE_ITEMS_QUERY,
    _ORDERS_QUERY,
    _ORDER_REFERENCE_QUERY,
    _ORDER_RETURNS_QUERY,
    _RETURN_TARGETS_QUERY,
)
from app.tools.shopify.models import (
    ShopifyFulfillmentLineItem,
    ShopifyOrderLineRef,
    ShopifyReturnLine,
    ShopifyShippedFulfillmentLine,
)
from app.tools.shopify.sanitizer import (
    redact,
    register_secret,
    registered_secret_values,
    reset_registered_secrets,
)

CLIENT_ID = "test-client-id"
# Test-only stand-ins shaped like real Shopify credentials (so the pattern-based redaction is
# exercised too); they are not, and can never be, live values.
CLIENT_SECRET = "shppa_SUPERSECRETVALUE1234"
ACCESS_TOKEN = "shpat_LIVETOKENABCD5678"

TOKEN_URL = "https://example.myshopify.com/admin/oauth/access_token"
GRAPHQL_URL = "https://example.myshopify.com/admin/api/2026-07/graphql.json"


@pytest.fixture(autouse=True)
def _clean_secrets():
    # The sanitizer registry is process-global; tests must not leak secrets into each other.
    reset_registered_secrets()
    yield
    reset_registered_secrets()


def make_client(handler) -> ShopifyReadOnlyClient:
    return ShopifyReadOnlyClient(
        store_domain="example.myshopify.com",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx.MockTransport(handler),
    )


def json_response(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


AUTH_OK = lambda request: json_response(  # noqa: E731
    {"access_token": ACCESS_TOKEN, "expires_in": 3600, "token_type": "bearer"}
)


# --------------------------------------------------------------------------- #
# 1. successful client-credentials authentication
# --------------------------------------------------------------------------- #
async def test_client_credentials_auth_success():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url == TOKEN_URL:
            return json_response(
                {"access_token": ACCESS_TOKEN, "expires_in": 3600, "token_type": "bearer"}
            )
        return json_response({"data": {"products": {"edges": []}}})

    client = make_client(handler)
    result = await client.obtain_access_token()
    assert result is True  # success indicator only — the token itself is never returned
    assert seen[0].url == TOKEN_URL
    assert seen[0].headers["content-type"] == "application/x-www-form-urlencoded"
    body = seen[0].content.decode()
    assert "grant_type=client_credentials" in body
    assert f"client_id={CLIENT_ID}" in body

    # The token is used on API calls but never returned by the client's public surface.
    products = await client.read_products()
    assert products == []
    # An opaque (non-JWT) store token is presented via X-Shopify-Access-Token, not Bearer —
    # the exact behavior verified live against the Dev store.
    assert seen[-1].headers["x-shopify-access-token"] == ACCESS_TOKEN
    assert "authorization" not in seen[-1].headers
    # Repeat GraphQL calls reuse the cached token — exactly one token exchange happened.
    await client.read_products()
    assert sum(1 for r in seen if r.url == TOKEN_URL) == 1


# --------------------------------------------------------------------------- #
# 2. authentication failure
# --------------------------------------------------------------------------- #
async def test_jwt_token_is_presented_as_bearer():
    jwt_token = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJnbGFkIiwidmVyIjoyfQ.abcdefENDSIG123"

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url == TOKEN_URL:
            return json_response({"access_token": jwt_token, "expires_in": 3600})
        return json_response({"data": {"products": {"edges": []}}})

    client = make_client(handler)
    await client.read_products()
    # Managed-API JWTs go as Authorization: Bearer (and never as the legacy header).
    assert seen[-1].headers["authorization"] == f"Bearer {jwt_token}"
    assert "x-shopify-access-token" not in seen[-1].headers


async def test_auth_failure_raises_without_leaking_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid client_secret {CLIENT_SECRET}")

    client = make_client(handler)
    with pytest.raises(ShopifyAuthError) as excinfo:
        await client.obtain_access_token()
    assert CLIENT_SECRET not in str(excinfo.value)
    assert ACCESS_TOKEN not in str(excinfo.value)
    assert "401" in excinfo.value.message


# --------------------------------------------------------------------------- #
# 3. GraphQL success + 4. GraphQL errors
# --------------------------------------------------------------------------- #
async def test_graphql_success_returns_data():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response({"data": {"products": {"edges": []}}})

    client = make_client(handler)
    data = await client.execute_graphql("query { products(first: 1) { edges { node { id } } } }")
    assert data == {"products": {"edges": []}}


async def test_graphql_level_errors_raise_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {
                "data": None,
                "errors": [
                    {
                        "message": "Access denied for returns field.",
                        "extensions": {"code": "ACCESS_DENIED"},
                    }
                ],
            }
        )

    client = make_client(handler)
    with pytest.raises(ShopifyGraphQLError) as excinfo:
        await client.execute_graphql("query { returns(first: 1) { edges { node { id } } } }")
    assert "Access denied for returns field." in excinfo.value.message
    assert excinfo.value.extensions[0]["code"] == "ACCESS_DENIED"


# --------------------------------------------------------------------------- #
# 5. missing credentials
# --------------------------------------------------------------------------- #
async def test_missing_credentials_fail_before_any_network_call():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("a network call was attempted with missing credentials")

    client = ShopifyReadOnlyClient(
        store_domain="", client_id="", client_secret="", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ShopifyConfigError) as excinfo:
        await client.obtain_access_token()
    # Names are reported; there are no values to leak, but assert the message is sane anyway.
    assert "SHOPIFY_STORE_DOMAIN" in excinfo.value.message
    assert "SHOPIFY_CLIENT_ID" in excinfo.value.message
    assert "SHOPIFY_CLIENT_SECRET" in excinfo.value.message

    # The same refusal is reachable through the public, network-free check and any read.
    with pytest.raises(ShopifyConfigError):
        client.validate_configuration()
    with pytest.raises(ShopifyConfigError):
        await client.read_products()


def test_factory_builds_unconfigured_client_safely():
    class _Cfg:
        shopify_store_domain = ""
        shopify_client_id = ""
        shopify_client_secret = ""
        shopify_api_version = "2026-07"

    client = get_shopify_client(settings=_Cfg())
    assert isinstance(client, ShopifyReadOnlyClient)
    with pytest.raises(ShopifyConfigError):
        client.validate_configuration()


# --------------------------------------------------------------------------- #
# 6. network failure / 7. timeout / 429 / malformed
# --------------------------------------------------------------------------- #
async def test_network_failure_is_typed_and_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused for {CLIENT_SECRET}")

    client = make_client(handler)
    with pytest.raises(ShopifyError) as excinfo:
        await client.obtain_access_token()
    assert CLIENT_SECRET not in str(excinfo.value)
    assert excinfo.value.status_code == 502


async def test_timeout_maps_to_504():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = make_client(handler)
    with pytest.raises(ShopifyError) as excinfo:
        await client.obtain_access_token()
    assert excinfo.value.status_code == 504
    assert "timed out" in excinfo.value.message.lower()


async def test_rate_limit_on_graphql():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return httpx.Response(429, text="Too many requests")

    client = make_client(handler)
    with pytest.raises(ShopifyRateLimitError):
        await client.read_products()


async def test_malformed_responses_are_typed():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text="not json at all")
        return json_response({"data": "a string, not an object"})

    client = make_client(handler)
    with pytest.raises(ShopifyMalformedResponseError):
        await client.read_products()
    with pytest.raises(ShopifyMalformedResponseError):
        await client.read_products()


async def test_unauthenticated_graphql_response_forces_reauth():
    state = {"tokens": 0, "first_graphql": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            state["tokens"] += 1
            return AUTH_OK(request)
        if state["first_graphql"]:
            state["first_graphql"] = False
            return httpx.Response(401, text="session expired")
        return json_response({"data": {"products": {"edges": []}}})

    client = make_client(handler)
    # First call authenticates (token #1) then Shopify 401s: the client clears the token and
    # surfaces a typed auth error.
    with pytest.raises(ShopifyAuthError):
        await client.read_products()
    assert state["tokens"] == 1
    # Second call must re-authenticate (token #2) rather than reuse the rejected one, and then
    # the (now non-401) GraphQL call succeeds.
    assert await client.read_products() == []
    assert state["tokens"] == 2


# --------------------------------------------------------------------------- #
# 8. the access token never reaches logs or exception text
# --------------------------------------------------------------------------- #
async def test_token_and_secret_never_logged(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return httpx.Response(500, text=f"server exploded (token was {ACCESS_TOKEN})")

    client = make_client(handler)
    with caplog.at_level(logging.DEBUG, logger="app.tools.shopify"):
        await client.obtain_access_token()
        with pytest.raises(ShopifyError):
            await client.read_products()

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert ACCESS_TOKEN not in logged
    assert CLIENT_SECRET not in logged
    # Even the exception carries no token: HTTP 5xx bodies are not echoed at all.
    assert "[redacted-token]" not in logged  # the 500 body never made it into a message


# --------------------------------------------------------------------------- #
# 9. product query normalization
# --------------------------------------------------------------------------- #
async def test_products_normalized_with_decimal_money():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {
                "data": {
                    "products": {
                        "edges": [
                            {
                                "node": {
                                    "id": "gid://shopify/Product/1",
                                    "title": "Head & Shoulders Anti-Hairfall Shampoo 360ml",
                                    "handle": "hs-360",
                                    "status": "ACTIVE",
                                    "priceRangeV2": {
                                        "minVariantPrice": {"amount": "410.00", "currencyCode": "INR"}
                                    },
                                }
                            },
                            {"node": {"title": "no id — skipped"}},
                            {"node": {"id": "gid://shopify/Product/2", "priceV2": {"amount": "n/a"}}},
                        ]
                    }
                }
            }
        )

    client = make_client(handler)
    products = await client.read_products(first=10)
    assert len(products) == 2
    first = products[0]
    assert isinstance(first, ShopifyProduct)
    assert first.title == "Head & Shoulders Anti-Hairfall Shampoo 360ml"
    assert first.price == Decimal("410.00")
    assert first.currency == "INR"
    assert products[1].price is None  # unreadable money normalizes to None, never a guess


# --------------------------------------------------------------------------- #
# 10. order query normalization
# --------------------------------------------------------------------------- #
async def test_orders_normalized_with_line_items():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {
                "data": {
                    "orders": {
                        "edges": [
                            {
                                "node": {
                                    "id": "gid://shopify/Order/1001",
                                    "name": "#1001",
                                    "createdAt": "2026-09-20T10:00:00Z",
                                    "displayFinancialStatus": "PAID",
                                    "displayFulfillmentStatus": "FULFILLED",
                                    "currentTotalPriceSet": {
                                        "shopMoney": {"amount": "410.00", "currencyCode": "INR"}
                                    },
                                    "lineItems": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "id": "gid://shopify/LineItem/7001",
                                                    "title": "Head & Shoulders Anti-Hairfall Shampoo 360ml",
                                                    "quantity": 1,
                                                    "sku": "HS-360",
                                                    "product": {"id": "gid://shopify/Product/1"},
                                                    "variant": {"id": "gid://shopify/ProductVariant/9"},
                                                }
                                            }
                                        ]
                                    },
                                }
                            }
                        ]
                    }
                }
            }
        )

    client = make_client(handler)
    orders = await client.read_orders(first=5)
    assert len(orders) == 1
    order = orders[0]
    assert isinstance(order, ShopifyOrder)
    assert order.name == "#1001"
    assert order.financial_status == "PAID"
    assert order.fulfillment_status == "FULFILLED"
    assert order.total == Decimal("410.00")
    assert order.currency == "INR"
    assert order.line_items[0].quantity == 1
    assert order.line_items[0].line_item_gid == "gid://shopify/LineItem/7001"
    assert order.line_items[0].product_id == "gid://shopify/Product/1"
    assert order.line_items[0].variant_id == "gid://shopify/ProductVariant/9"
    # ShopifyOrder carries no fulfillment data: that read is separate by design (see below).
    assert not hasattr(order, "fulfillment_line_items")


# --------------------------------------------------------------------------- #
# 10b. fulfillment line item read (Task 14C.1): its own query, read-only
# --------------------------------------------------------------------------- #
async def test_fulfillment_line_items_are_read_from_shopify_not_derived():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        seen.append(json.loads(request.content.decode("utf-8")))
        return json_response(
            {
                "data": {
                    "order": {
                        "id": "gid://shopify/Order/1001",
                        "fulfillmentOrders": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "gid://shopify/FulfillmentOrder/3001",
                                        "status": "CLOSED",
                                        "lineItems": {
                                            "edges": [
                                                {
                                                    "node": {
                                                        "id": "gid://shopify/FulfillmentOrderLineItem/8001",
                                                        "remainingQuantity": 1,
                                                        "lineItem": {
                                                            "id": "gid://shopify/LineItem/7001"
                                                        },
                                                    }
                                                },
                                                {
                                                    # A line answered without a quantity:
                                                    # normalized to None, never guessed as 0.
                                                    "node": {
                                                        "id": "gid://shopify/FulfillmentOrderLineItem/8002",
                                                        "lineItem": {
                                                            "id": "gid://shopify/LineItem/7002"
                                                        },
                                                    },
                                                },
                                            ]
                                        },
                                    }
                                }
                            ]
                        },
                    }
                }
            }
        )

    client = make_client(handler)
    items = await client.read_fulfillment_line_items("gid://shopify/Order/1001")
    assert items == [
        ShopifyFulfillmentLineItem(
            fulfillment_line_item_id="gid://shopify/FulfillmentOrderLineItem/8001",
            remaining_quantity=1,
            fulfillment_order_id="gid://shopify/FulfillmentOrder/3001",
            order_line_item_id="gid://shopify/LineItem/7001",
        ),
        ShopifyFulfillmentLineItem(
            fulfillment_line_item_id="gid://shopify/FulfillmentOrderLineItem/8002",
            remaining_quantity=None,
            fulfillment_order_id="gid://shopify/FulfillmentOrder/3001",
            order_line_item_id="gid://shopify/LineItem/7002",
        ),
    ]
    # A READ, aimed at one order — never a mutation, and never a bulk orders query.
    assert "mutation" not in seen[0]["query"]
    assert seen[0]["variables"] == {"id": "gid://shopify/Order/1001"}


async def test_fulfillment_line_item_read_reports_refusal_honestly():
    """
    This store can refuse `Order.fulfillmentOrders` even with read_orders granted (observed
    live). The client must surface Shopify's own sanitized refusal — not an empty list, which
    would read as "this order has nothing to return".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {
                "errors": [
                    {
                        "message": "Access denied for fulfillmentOrders field.",
                        "extensions": {"code": "ACCESS_DENIED"},
                    }
                ]
            }
        )

    client = make_client(handler)
    with pytest.raises(ShopifyGraphQLError) as excinfo:
        await client.read_fulfillment_line_items("gid://shopify/Order/1001")
    assert "fulfillmentOrders" in excinfo.value.message


async def test_fulfillment_line_item_read_of_an_unknown_order_is_not_faked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response({"data": {"order": None}})

    client = make_client(handler)
    with pytest.raises(ShopifyMalformedResponseError):
        await client.read_fulfillment_line_items("gid://shopify/Order/0000")


async def test_fulfillment_line_item_read_accepts_an_empty_fulfillment_set():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {"data": {"order": {"id": "gid://shopify/Order/1003", "fulfillmentOrders": {"edges": []}}}}
        )

    client = make_client(handler)
    assert await client.read_fulfillment_line_items("gid://shopify/Order/1003") == []


def test_fulfillment_reads_stay_out_of_the_general_orders_query():
    """
    Regression guard for the live finding of Task 14C.1: `fulfillmentOrders` is authorized
    separately from the rest of the order, so folding it into _ORDERS_QUERY would let a refusal
    there break the orders read that everything else depends on.
    """
    assert "fulfillmentOrders" not in _ORDERS_QUERY
    assert "id title quantity sku" in _ORDERS_QUERY  # the line item id read is still there
    # The dedicated read uses the field names the live 2026-07 schema actually exposes:
    # FulfillmentOrderLineItem has remainingQuantity and NO quantity field.
    assert "remainingQuantity" in _FULFILLMENT_LINE_ITEMS_QUERY
    fulfillment_block = _FULFILLMENT_LINE_ITEMS_QUERY.split("fulfillmentOrders", 1)[1]
    assert "quantity" not in fulfillment_block.replace("remainingQuantity", "")
    assert "mutation" not in _FULFILLMENT_LINE_ITEMS_QUERY


# --------------------------------------------------------------------------- #
# 11. returns query handling — read only; scope/API absence surfaces honestly
# --------------------------------------------------------------------------- #
async def test_returns_success_path():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        # 2026-07 shape: returns are read through their parent order (no top-level connection).
        return json_response(
            {
                "data": {
                    "orders": {
                        "edges": [
                            {
                                "node": {
                                    "id": "gid://shopify/Order/1001",
                                    "name": "#1001",
                                    "returns": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "id": "gid://shopify/Return/5",
                                                    "status": "IN_PROGRESS",
                                                    "createdAt": "2026-09-21T08:00:00Z",
                                                }
                                            }
                                        ]
                                    },
                                }
                            },
                            {"node": {"id": "gid://shopify/Order/1002", "name": "#1002", "returns": {"edges": []}}},
                        ]
                    }
                }
            }
        )

    client = make_client(handler)
    returns = await client.read_returns(first=5)
    assert returns == [
        ShopifyReturn(
            return_id="gid://shopify/Return/5",
            status="IN_PROGRESS",
            order_id="gid://shopify/Order/1001",
            order_name="#1001",
            created_at="2026-09-21T08:00:00Z",
        )
    ]


async def test_returns_scope_error_propagates_without_faking_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {"errors": [{"message": "Field 'returns' doesn't exist on type 'Query'",
                         "extensions": {"code": "GRAPHQL_PARSE_FAILED"}}]}
        )

    client = make_client(handler)
    with pytest.raises(ShopifyGraphQLError):
        await client.read_returns()


# --------------------------------------------------------------------------- #
# Task 14C.4: the shipped-fulfillment read (what a return must reference) and the per-order
# returns read (how a lost answer is reconciled)
# --------------------------------------------------------------------------- #
async def test_return_target_read_returns_the_canonical_line_and_its_shipped_quantity():
    """
    `returnCreate` names a FulfillmentLineItem, so that is the object this read must produce —
    joined to the order line it belongs to, with the units that actually shipped.
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        seen.append(json.loads(request.content.decode("utf-8")))
        return json_response(
            {
                "data": {
                    "order": {
                        "id": "gid://shopify/Order/1001",
                        "returnStatus": "NO_RETURN",
                        "lineItems": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "gid://shopify/LineItem/7001",
                                        "product": {"id": "gid://shopify/Product/4101"},
                                    }
                                }
                            ]
                        },
                        "fulfillments": [
                            {
                                "id": "gid://shopify/Fulfillment/5001",
                                "status": "SUCCESS",
                                "fulfillmentLineItems": {
                                    "edges": [
                                        {
                                            "node": {
                                                "id": "gid://shopify/FulfillmentLineItem/9001",
                                                "quantity": 1,
                                                "lineItem": {
                                                    "id": "gid://shopify/LineItem/7001"
                                                },
                                            }
                                        },
                                        {
                                            # A line answered without a quantity stays None: the
                                            # caller must refuse, never assume a returnable count.
                                            "node": {
                                                "id": "gid://shopify/FulfillmentLineItem/9002",
                                                "lineItem": {
                                                    "id": "gid://shopify/LineItem/7002"
                                                },
                                            },
                                        },
                                    ]
                                },
                            }
                        ],
                    }
                }
            }
        )

    client = make_client(handler)
    state = await client.read_return_targets("gid://shopify/Order/1001")

    assert state.order_id == "gid://shopify/Order/1001"
    assert state.return_status == "NO_RETURN"
    assert state.order_lines == [
        ShopifyOrderLineRef(
            order_line_item_id="gid://shopify/LineItem/7001",
            product_id="gid://shopify/Product/4101",
        )
    ]
    assert state.shipped_lines == [
        ShopifyShippedFulfillmentLine(
            fulfillment_line_item_id="gid://shopify/FulfillmentLineItem/9001",
            quantity=1,
            order_line_item_id="gid://shopify/LineItem/7001",
            fulfillment_id="gid://shopify/Fulfillment/5001",
            fulfillment_status="SUCCESS",
        ),
        ShopifyShippedFulfillmentLine(
            fulfillment_line_item_id="gid://shopify/FulfillmentLineItem/9002",
            quantity=None,
            order_line_item_id="gid://shopify/LineItem/7002",
            fulfillment_id="gid://shopify/Fulfillment/5001",
            fulfillment_status="SUCCESS",
        ),
    ]
    # A READ aimed at one order. Never a mutation, and never the fulfillmentOrders path.
    assert "mutation" not in seen[0]["query"]
    assert seen[0]["variables"] == {"id": "gid://shopify/Order/1001"}


async def test_return_target_read_reports_an_order_with_nothing_shipped():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {
                "data": {
                    "order": {
                        "id": "gid://shopify/Order/1002",
                        "returnStatus": "NO_RETURN",
                        "lineItems": {"edges": []},
                        "fulfillments": [],
                    }
                }
            }
        )

    client = make_client(handler)
    state = await client.read_return_targets("gid://shopify/Order/1002")
    assert state.shipped_lines == []
    assert state.order_lines == []


async def test_return_target_read_of_an_unknown_order_is_not_faked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response({"data": {"order": None}})

    client = make_client(handler)
    with pytest.raises(ShopifyMalformedResponseError):
        await client.read_return_targets("gid://shopify/Order/0000")


async def test_return_target_read_reports_a_refusal_honestly():
    """A denied field must surface as Shopify's error, not as an empty 'nothing was shipped'."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {
                "errors": [
                    {
                        "message": "Access denied for fulfillments field.",
                        "extensions": {"code": "ACCESS_DENIED"},
                    }
                ]
            }
        )

    client = make_client(handler)
    with pytest.raises(ShopifyGraphQLError) as excinfo:
        await client.read_return_targets("gid://shopify/Order/1001")
    assert "fulfillments" in excinfo.value.message


async def test_order_returns_read_carries_the_line_quantity_and_reason_note():
    """
    Reconciling a lost submission needs the per-return line quantity and the reason note RESOLVE
    sent — and nothing else. Both are read from the order's own returns connection.
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        seen.append(json.loads(request.content.decode("utf-8")))
        return json_response(
            {
                "data": {
                    "order": {
                        "id": "gid://shopify/Order/1001",
                        "name": "#1001",
                        "returns": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "gid://shopify/Return/6001",
                                        "status": "REQUESTED",
                                        "createdAt": "2026-09-26T12:00:30Z",
                                        "returnLineItems": {
                                            "edges": [
                                                {
                                                    "node": {
                                                        "quantity": 1,
                                                        "returnReasonNote": "DEFECTIVE",
                                                    }
                                                }
                                            ]
                                        },
                                    }
                                }
                            ]
                        },
                    }
                }
            }
        )

    client = make_client(handler)
    returns = await client.read_order_returns("gid://shopify/Order/1001")
    assert returns == [
        ShopifyReturn(
            return_id="gid://shopify/Return/6001",
            status="REQUESTED",
            order_id="gid://shopify/Order/1001",
            order_name="#1001",
            created_at="2026-09-26T12:00:30Z",
            lines=[ShopifyReturnLine(quantity=1, reason_note="DEFECTIVE")],
        )
    ]
    assert "mutation" not in seen[0]["query"]


async def test_order_returns_read_of_an_order_with_no_returns_is_empty_not_invented():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {
                "data": {
                    "order": {
                        "id": "gid://shopify/Order/1001",
                        "name": "#1001",
                        "returns": {"edges": []},
                    }
                }
            }
        )

    client = make_client(handler)
    assert await client.read_order_returns("gid://shopify/Order/1001") == []


def test_the_new_reads_ask_for_no_customer_data_and_no_extra_objects():
    """
    The Task 14C.4 reads and the Task 14C.5 order-reference read stay inside
    read_orders / read_returns: object ids, integer counts, ISO timestamps and a reason token. No
    name, email, phone, address, shipping or tracking field appears in any of them.
    """
    for query in (_RETURN_TARGETS_QUERY, _ORDER_RETURNS_QUERY, _ORDER_REFERENCE_QUERY):
        # The reason token is stripped first: it is a fixed enum word RESOLVE itself sends, and
        # it is the only field here whose name could be mistaken for free text from a person.
        lowered = query.lower().replace("returnreasonnote", "")
        assert "mutation" not in lowered
        for forbidden in (
            "customer",
            "firstname",
            "lastname",
            "email",
            "phone",
            "address",
            "shipping",
            "tracking",
            "label",
            "note",
            "totalPriceSet",
            "refund",
        ):
            assert forbidden not in lowered, forbidden


# --------------------------------------------------------------------------- #
# Task 14C.5: the minimal order-reference read behind the server-side linkage
# --------------------------------------------------------------------------- #
async def test_order_reference_read_returns_exactly_what_shopify_names():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {"data": {"order": {"id": "gid://shopify/Order/7990366601503", "name": "#1001"}}}
        )

    client = make_client(handler)
    reference = await client.read_order_reference("gid://shopify/Order/7990366601503")
    assert reference.order_id == "gid://shopify/Order/7990366601503"
    assert reference.name == "#1001"


async def test_order_reference_read_asks_only_for_existence_and_name():
    """
    This read exists to answer one question — does Shopify know this order — so its document asks
    for two fields and nothing else. A wider read here would be a data-mining read dressed up as
    a check.
    """
    lowered = _ORDER_REFERENCE_QUERY.lower()
    assert "mutation" not in lowered
    for field in ("email", "phone", "name", "address", "customer", "totalprice", "lineitems",
                  "returns", "note", "shipping", "tax"):
        if field == "name":
            continue  # the order's own #1001-style number, not a person's name
        assert field not in lowered, field
    assert lowered.count("name") == 1


async def test_order_reference_read_of_an_unknown_order_is_not_faked():
    """Shopify answering `order: null` is a refusal to link, never a licence to store anyway."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response({"data": {"order": None}})

    client = make_client(handler)
    with pytest.raises(ShopifyMalformedResponseError):
        await client.read_order_reference("gid://shopify/Order/1")


async def test_order_reference_read_reports_a_refusal_honestly():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            return AUTH_OK(request)
        return json_response(
            {"data": None, "errors": [{"message": "could not be found", "extensions": {}}]}
        )

    client = make_client(handler)
    with pytest.raises(ShopifyError):
        await client.read_order_reference("gid://shopify/Order/1")


# --------------------------------------------------------------------------- #
# sanitizer behavior itself
# --------------------------------------------------------------------------- #
def test_redact_scrubs_registered_values_and_shapes():
    register_secret(CLIENT_SECRET)
    assert CLIENT_SECRET not in redact(f"body said: {CLIENT_SECRET}")
    assert "[redacted]" in redact(f"body said: {CLIENT_SECRET}")
    assert "[redacted-token]" in redact("token shpat_ABC123 leaked")
    assert "[redacted-token]" in redact("token shpua_ABC123 leaked")
    assert "[redacted]" in redact("Authorization: Bearer abc.def.ghi")
    assert "[redacted]" in redact(f"client_secret={CLIENT_SECRET}")
    # A message with nothing sensitive passes through unchanged.
    assert redact("plain error") == "plain error"
    # Registered values are readable only via the test-only accessor; assert it exists so the
    # secret registry itself is observable to tests but never surfaced by the client.
    assert CLIENT_SECRET in registered_secret_values()
