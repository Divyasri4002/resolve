"""
Tests for the real Shopify return provider: implemented, and wired to nothing.

Originating in Task 14C.1 and hardened in Task 14C.4, this file holds the provider to two
promises: it may only aim a return at what Shopify itself says can be returned, and it must never
let a lost answer become a blind second mutation.

Hermetic by construction: every exchange runs through httpx.MockTransport against
example.myshopify.com. No Shopify host is contacted, no return is created anywhere, and no order
state changes. What these tests hold shut:

  1. the return target is a FulfillmentLineItem id read from Shopify's own fulfilment lines — the
     wrong id family, a constructed id, and a caller-named id are all refused before any network
     call;
  2. `FulfillmentLineItem.quantity` (units shipped) is the local ceiling, and
     `FulfillmentOrderLineItem.remainingQuantity` (units awaiting fulfilment) never is;
  3. a target that cannot be resolved unambiguously stops the flow: no submission, no guess at
     which line the issue is about;
  4. Shopify userErrors and code-bearing GraphQL refusals are deterministic rejections; a lost
     answer, a server-side HTTP failure, an unintelligible response, or a no-id/no-error answer is
     an UNKNOWN outcome, never a failure dressed up as knowledge;
  5. reconciliation reads `Order.returns` and nothing else: exactly one match is adopted, zero or
     several are reported as such, and no mutation document appears anywhere in it;
  6. the reason mapping is deterministic and lands only on reasons the live 2026-07 schema
     actually supports (there is no DAMAGED enum value);
  7. no credential reaches an error message, a log line, or the acknowledgement;
  8. the provider touches no RESOLVE database, no Authority Engine, no reasoning model, no
     orchestrator — and nothing in production can reach the provider yet.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from app.routers import missions as missions_module
from app.resolution import orchestrator as orchestrator_module
from app.tools import returns as returns_pkg
from app.tools.returns import base as returns_base
from app.tools.returns import unconnected as unconnected_pkg
from app.tools.returns import get_return_provider
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
from app.tools.shopify import return_provider as return_provider_module
from app.tools.shopify import return_provider as shopify_return_provider_module
from app.tools.shopify.admin_transport import ShopifyAdminTransport
from app.tools.shopify.client import (
    _FULFILLMENT_LINE_ITEMS_QUERY,
    _ORDER_LINE_IDENTITY_QUERY,
    _ORDER_REFERENCE_QUERY,
    _ORDER_RETURNS_QUERY,
    _RETURN_TARGETS_QUERY,
    ShopifyReadOnlyClient,
)
from app.tools.shopify.errors import (
    ShopifyAuthError,
    ShopifyConfigError,
    ShopifyError,
    ShopifyGraphQLError,
    ShopifyMalformedResponseError,
    ShopifyRateLimitError,
    ShopifyServerError,
    ShopifyTransportError,
)
from app.tools.shopify.return_provider import (
    FULFILLMENT_LINE_ITEM_GID_PREFIX,
    FULFILLMENT_ORDER_LINE_ITEM_GID_PREFIX,
    RETURN_REASON_BY_ISSUE_TYPE,
    ShopifyReturnProvider,
    ShopifyReturnRequest,
)
from app.tools.shopify.return_write_client import ShopifyReturnWriteClient
from app.tools.shopify.sanitizer import reset_registered_secrets
from tests.test_shopify_client import (  # noqa: F401 - test-only credential shapes, reused
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    GRAPHQL_URL,
    TOKEN_URL,
)

ORDER_GID = "gid://shopify/Order/7990366601503"
# The identifier a 2026-07 return actually references (live-verified shape).
FULFILLMENT_LINE_GID = "gid://shopify/FulfillmentLineItem/16492059328799"
# A neighbouring, wrong-family id: nearly the same name, a different object.
FULFILLMENT_ORDER_LINE_GID = "gid://shopify/FulfillmentOrderLineItem/18989131727135"
RETURN_GID = "gid://shopify/Return/9988776655440"
DEFINITION_GID = "gid://shopify/ReturnReasonDefinition/5001"

PRODUCT_GID = "gid://shopify/Product/10622982422815"
VARIANT_GID = "gid://shopify/ProductVariant/52923834990879"
LINE_ITEM_GID = "gid://shopify/LineItem/18801909727519"
# A RESOLVE-internal surrogate id, to prove nothing is ever assembled from parts.
RESOLVE_ROW_ID = "44990366601503"


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


class Recorded:
    """Every exchange the provider attempted, in order. Empty means nothing left the process."""

    def __init__(self) -> None:
        self.graphql: list[dict] = []
        self.tokens = 0

    @property
    def attempted(self) -> bool:
        return bool(self.graphql) or self.tokens > 0

    @property
    def mutations(self) -> list[dict]:
        return [
            entry for entry in self.graphql if "mutation ResolveReturnCreate" in entry["query"]
        ]

    @property
    def payload(self) -> dict:
        assert len(self.graphql) == 1, f"expected exactly one GraphQL request, got {len(self.graphql)}"
        return self.graphql[0]

    @property
    def query(self) -> str:
        return self.payload["query"]

    @property
    def return_input(self) -> dict:
        return self.payload["variables"]["returnInput"]

    @property
    def line_item(self) -> dict:
        return self.return_input["returnLineItems"][0]


@pytest.fixture
def recorded() -> Recorded:
    return Recorded()


def make_clients(
    recorded: Recorded,
    *,
    data: dict | None = None,
    errors: list | None = None,
    http_status: int = 200,
    body: str | None = None,
    raise_exc: Exception | None = None,
    forbid_mutations: bool = False,
) -> tuple[ShopifyReadOnlyClient, ShopifyReturnWriteClient]:
    """
    RESOLVE's two Shopify capabilities over the ONE existing credential system, with a mock
    transport bolted onto the transport seam they share. No second auth path exists in this file
    because none exists in the code.

    Task 14C.7O shaped this deliberately: ONE `ShopifyAdminTransport`, handed to the read-only
    client and to the return-write client. So a test that reads and then submits sees a single
    client-credentials exchange (`recorded.tokens == 1`), which is the proof that splitting the
    capability did not duplicate the credential.

    `forbid_mutations` is the registration suite's tripwire: on that side a mutation must fail the
    test rather than be recorded, because nothing there is allowed to write.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            recorded.tokens += 1
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN, "expires_in": 3600, "token_type": "bearer"},
            )
        assert str(request.url) == GRAPHQL_URL, f"unexpected outbound URL: {request.url}"
        if raise_exc is not None:
            raise raise_exc
        parsed = json.loads(request.content.decode("utf-8"))
        query = parsed.get("query", "")
        if forbid_mutations:
            assert not query.lstrip().startswith("mutation"), (
                "a mutation was sent while merely registering / reading through the provider"
            )
        recorded.graphql.append(
            {
                "query": query,
                "variables": parsed.get("variables") or {},
                "headers": dict(request.headers),
            }
        )
        if body is not None:
            return httpx.Response(http_status, text=body)
        if errors:
            return httpx.Response(http_status, json={"data": None, "errors": errors})
        return httpx.Response(http_status, json={"data": data if data is not None else {}})

    admin = ShopifyAdminTransport(
        store_domain="example.myshopify.com",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx.MockTransport(handler),
    )
    return ShopifyReadOnlyClient(admin=admin), ShopifyReturnWriteClient(admin=admin)


def make_provider(
    recorded: Recorded,
    *,
    data: dict | None = None,
    errors: list | None = None,
    http_status: int = 200,
    body: str | None = None,
    raise_exc: Exception | None = None,
) -> ShopifyReturnProvider:
    """The provider over the two real capabilities, exactly as the registering factory builds it."""
    read, write = make_clients(
        recorded, data=data, errors=errors, http_status=http_status, body=body, raise_exc=raise_exc
    )
    return ShopifyReturnProvider(read, write_client=write)


def make_request(**overrides) -> ShopifyReturnRequest:
    """
    A canonical request, shaped like what RESOLVE assembles from persisted records.

    It carries canonical RESOLVE identity and the order's persisted merchant linkage — and,
    deliberately, no return target: no line id, no quantity, no reason.
    """
    fields = {
        "mission_id": "mission_1",
        "issue_id": "issue_1",
        "order_id": "ord_canonical_1",
        "return_id": "ret_canonical_1",
        "product_id": "prod_canonical_1",
        "title": "Head & Shoulders Anti-Hairfall Shampoo 360ml",
        "merchant": "Resolve Commerce Lab",
        "reason": "arrived cracked",
        "issue_type": "damaged",
        "provider_order_reference": ORDER_GID,
    }
    fields.update(overrides)
    return ShopifyReturnRequest(**fields)


def make_target(**overrides) -> ReturnTarget:
    """The target RESOLVE's own read of Shopify produced, as the executor hands it back."""
    fields = {
        "provider_order_reference": ORDER_GID,
        "provider_line_reference": FULFILLMENT_LINE_GID,
        "provider_quantity": 1,
        "provider_reason": "DEFECTIVE",
        "line_quantity": 1,
        "detail": None,
    }
    fields.update(overrides)
    return ReturnTarget(**fields)


def make_submission(**overrides) -> ReturnSubmission:
    fields = {
        "return_id": "ret_canonical_1",
        "issue_id": "issue_1",
        "provider_order_reference": ORDER_GID,
        "provider_line_reference": FULFILLMENT_LINE_GID,
        "provider_quantity": 1,
        "provider_reason": "DEFECTIVE",
        "submitted_at": datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    return ReturnSubmission(**fields)


def shipped(quantity=1, line_id=FULFILLMENT_LINE_GID, **kwargs) -> dict:
    return {
        "id": line_id,
        "quantity": quantity,
        "fulfillment_id": "gid://shopify/Fulfillment/7166055481631",
        **kwargs,
    }


def targets_data(*lines, order_lines=None, return_status="NO_RETURN") -> dict:
    """A `read_return_targets` answer: what shipped, and whose product each order line is."""
    return {
        "order": {
            "id": ORDER_GID,
            "returnStatus": return_status,
            "lineItems": {
                "edges": [
                    {"node": {"id": line_id, "product": {"id": product_id}}}
                    for line_id, product_id in (order_lines or [(LINE_ITEM_GID, PRODUCT_GID)])
                ]
            },
            "fulfillments": [
                {
                    "id": line["fulfillment_id"],
                    "status": line.get("fulfillment_status", "SUCCESS"),
                    "fulfillmentLineItems": {
                        "edges": [
                            {
                                "node": {
                                    "id": line["id"],
                                    "quantity": line.get("quantity"),
                                    "lineItem": {
                                        "id": line.get("line_item_id", LINE_ITEM_GID)
                                    },
                                }
                            }
                        ]
                    },
                }
                for line in lines
            ],
        }
    }


def order_returns_data(*returns) -> dict:
    """An `Order.returns` answer, with each return's own line quantities and reason notes."""
    return {
        "order": {
            "id": ORDER_GID,
            "name": "#1001",
            "returns": {
                "edges": [
                    {
                        "node": {
                            "id": item["id"],
                            "status": item.get("status", "REQUESTED"),
                            "createdAt": item.get("createdAt"),
                            "returnLineItems": {
                                "edges": [
                                    {"node": {"quantity": q, "returnReasonNote": note}}
                                    for q, note in item.get("lines", [])
                                ]
                            },
                        }
                    }
                    for item in returns
                ]
            },
        }
    }


_NO_TARGET = object()


async def submit(provider, request=None, target=_NO_TARGET, **request_overrides):
    """Send a return the way the executor does: with the target RESOLVE resolved for it."""
    return await provider.request_return(
        request or make_request(**request_overrides),
        make_target() if target is _NO_TARGET else target,
    )


def success_data(node: dict | None = None) -> dict:
    return {
        "returnCreate": {
            "userErrors": [],
            "return": node or {"id": RETURN_GID, "status": "REQUESTED"},
        }
    }


def user_errors(*pairs: tuple[str, str]) -> dict:
    return {
        "returnCreate": {
            "userErrors": [{"field": f, "message": m} for f, m in pairs],
            "return": None,
        }
    }


# --------------------------------------------------------------------------- #
# contract shape
# --------------------------------------------------------------------------- #
def test_provider_satisfies_the_existing_return_contract():
    provider = make_provider(Recorded())
    assert isinstance(provider, ReturnProvider)
    assert provider.name == "shopify"
    assert issubclass(ShopifyReturnRequest, ReturnRequest)
    assert ShopifyReturnRequest.__dataclass_params__.frozen is True
    assert ReturnRequest.__dataclass_params__.frozen is True
    base_fields = {f.name for f in dataclasses.fields(ReturnRequest)}
    assert {
        "mission_id",
        "issue_id",
        "order_id",
        "return_id",
        "product_id",
        "title",
        "merchant",
        "issue_type",
        "provider_order_reference",
    } <= base_fields
    # The Shopify request adds only an optional store reason definition. Everything that names a
    # return target is resolved from Shopify, not carried on a request.
    assert {f.name for f in dataclasses.fields(ShopifyReturnRequest)} == base_fields | {
        "return_reason_definition_id"
    }


def test_the_return_target_id_family_is_the_one_shopify_accepts():
    """Pinned against the live 2026-07 schema finding: the target is a FulfillmentLineItem id."""
    assert FULFILLMENT_LINE_ITEM_GID_PREFIX == "gid://shopify/FulfillmentLineItem/"
    assert FULFILLMENT_ORDER_LINE_ITEM_GID_PREFIX == "gid://shopify/FulfillmentOrderLineItem/"
    assert FULFILLMENT_LINE_ITEM_GID_PREFIX != FULFILLMENT_ORDER_LINE_ITEM_GID_PREFIX
    # And the read that produces it is the shipped-fulfilment path, not the fulfillmentOrders one.
    assert "fulfillmentLineItems" in _RETURN_TARGETS_QUERY
    assert "fulfillmentOrders" not in _RETURN_TARGETS_QUERY


# --------------------------------------------------------------------------- #
# 1. successful returnCreate: payload correctness + normalized ReturnAck
# --------------------------------------------------------------------------- #
async def test_successful_return_create_returns_accepted_ack(recorded):
    provider = make_provider(recorded, data=success_data())

    ack = await submit(provider)

    assert isinstance(ack, ReturnAck)
    assert ack.provider_reference == RETURN_GID
    assert ack.accepted is True  # "Shopify opened a return" — nothing more is claimed
    assert recorded.tokens == 1  # auth went through the existing client, exactly once
    assert len(recorded.mutations) == 1


async def test_mutation_payload_matches_the_live_2026_07_input_shape(recorded):
    provider = make_provider(recorded, data=success_data())

    await submit(
        provider,
        target=make_target(
            provider_quantity=2, line_quantity=2, provider_reason="WRONG_ITEM"
        ),
    )

    assert "mutation ResolveReturnCreate($returnInput: ReturnInput!)" in recorded.query
    assert "returnCreate(returnInput: $returnInput)" in recorded.query
    assert "userErrors { field message }" in recorded.query
    # Only fields proven to exist are read back: no label, tracking, approval, or money invented.
    assert "return { id status }" in recorded.query
    assert "label" not in recorded.query

    assert recorded.payload["variables"] == {
        "returnInput": {
            "orderId": ORDER_GID,
            "returnLineItems": [
                {
                    "fulfillmentLineItemId": FULFILLMENT_LINE_GID,
                    "quantity": 2,
                    "returnReasonNote": "WRONG_ITEM",
                }
            ],
            # 2026-07 declares exchangeLineItems non-null; RESOLVE does not exchange items.
            "exchangeLineItems": [],
        }
    }
    # The opaque store token rides the header the live store requires.
    assert recorded.payload["headers"]["x-shopify-access-token"] == ACCESS_TOKEN


async def test_optional_return_reason_definition_is_carried(recorded):
    provider = make_provider(recorded, data=success_data())
    await submit(provider, request=make_request(return_reason_definition_id=DEFINITION_GID))
    assert recorded.line_item["returnReasonDefinitionId"] == DEFINITION_GID


async def test_omitted_reason_definition_stays_omitted(recorded):
    provider = make_provider(recorded, data=success_data())
    await submit(provider)
    assert "returnReasonDefinitionId" not in recorded.line_item


async def test_no_resolve_identity_or_money_leaves_the_process(recorded):
    """
    Mission/issue/RESOLVE-record ids, the customer's own words, and every money field stay
    internal. The merchant is told which Shopify order and fulfillment line to take back, plus a
    reason token and a quantity — nothing else, and nothing that could carry an amount or an
    authority claim.
    """
    provider = make_provider(recorded, data=success_data())
    await submit(provider)

    sent = json.dumps(recorded.payload["variables"])
    for internal in (
        "mission_1",
        "issue_1",
        "ret_canonical_1",
        "ord_canonical_1",
        "prod_canonical_1",
        "arrived cracked",
        "Resolve Commerce Lab",
        "Head & Shoulders",
        "restockingFee",
        "pricePerItem",
        "amount",
        "currency",
        "refund",
        "authority",
    ):
        assert internal not in sent, internal
    assert set(recorded.return_input) == {"orderId", "returnLineItems", "exchangeLineItems"}
    assert set(recorded.line_item) == {
        "fulfillmentLineItemId",
        "quantity",
        "returnReasonNote",
    }


# --------------------------------------------------------------------------- #
# 2. Shopify userErrors normalize onto the existing error model
# --------------------------------------------------------------------------- #
async def test_user_errors_become_a_sanitized_rejection(recorded):
    provider = make_provider(
        recorded,
        data=user_errors(
            ("returnLineItems", "Fulfillment order not found for the given line item")
        ),
    )
    with pytest.raises(ReturnProviderRejected) as excinfo:
        await submit(provider)
    assert "Fulfillment order not found" in excinfo.value.message
    assert excinfo.value.status_code == 502


async def test_a_declining_response_never_produces_an_ack(recorded):
    provider = make_provider(recorded, data=user_errors(("orderId", "Order already returned")))
    with pytest.raises(ReturnError) as excinfo:
        await submit(provider)
    assert excinfo.value.status_code == 502
    # A deterministic 'no' is a rejection, never an unknown, and the half-answer is not adopted.
    assert not isinstance(excinfo.value, ReturnOutcomeUnknown)
    assert not hasattr(excinfo.value, "provider_reference")


# --------------------------------------------------------------------------- #
# 3. what Shopify said, versus what RESOLVE cannot know
# --------------------------------------------------------------------------- #
async def test_an_answer_with_no_return_id_and_no_refusal_is_unknown_not_failed(recorded):
    """Shopify neither refused nor named a return. 'failed' would assert it did not happen."""
    provider = make_provider(recorded, data={"returnCreate": {"userErrors": [], "return": None}})
    with pytest.raises(ReturnOutcomeUnknown) as excinfo:
        await submit(provider)
    assert excinfo.value.status_code == 503
    assert isinstance(excinfo.value, ReturnError)


async def test_an_empty_data_object_is_not_a_silent_success_or_a_false_failure(recorded):
    provider = make_provider(recorded, data={})
    with pytest.raises(ReturnOutcomeUnknown):
        await submit(provider)


@pytest.mark.parametrize(
    "exc",
    [
        ShopifyTransportError("Shopify GraphQL request timed out.", status_code=504),
        ShopifyTransportError("Network error while querying Shopify: reset", status_code=502),
        ShopifyServerError("Shopify returned HTTP 502.", status_code=502),
        ShopifyMalformedResponseError(
            "Shopify returned a non-JSON GraphQL response.", details={"phase": "graphql"}
        ),
    ],
)
async def test_an_exchange_that_produced_no_answer_is_outcome_unknown(
    recorded, monkeypatch, exc
):
    """The request may have landed. Nothing here is allowed to claim it did not."""
    provider = make_provider(recorded)

    async def blow_up(*_args, **_kwargs):
        raise exc

    # The fault is injected on the write capability, because that is where the mutation goes now.
    monkeypatch.setattr(provider._write, "submit_return_create", blow_up)
    with pytest.raises(ReturnOutcomeUnknown):
        await submit(provider)


@pytest.mark.parametrize(
    "code", ["INTERNAL_SERVER_ERROR", "TIMEOUT", "SERVICE_UNAVAILABLE", "GATEWAY_TIMEOUT"]
)
async def test_a_server_side_graphql_failure_leaves_the_outcome_unknown(recorded, code):
    provider = make_provider(
        recorded,
        errors=[
            {
                "message": "something went wrong while processing",
                "extensions": {"code": code},
            }
        ],
    )
    with pytest.raises(ReturnOutcomeUnknown):
        await submit(provider)


async def test_a_graphql_error_with_no_code_says_too_little_to_be_a_refusal(recorded):
    provider = make_provider(recorded, errors=[{"message": "error"}])
    with pytest.raises(ReturnOutcomeUnknown):
        await submit(provider)


@pytest.mark.parametrize(
    "shopify_error,expected",
    [
        # Shopify spoke about the request itself: deterministic, and nothing was created.
        (
            ShopifyGraphQLError(
                "Field 'returnCreate' doesn't exist on type 'Mutation'",
                extensions=[{"code": "GRAPHQL_VALIDATION_FAILED"}],
            ),
            ReturnProviderRejected,
        ),
        (
            ShopifyGraphQLError(
                "Missing write_returns scope for this app",
                extensions=[{"code": "ACCESS_DENIED"}],
            ),
            ReturnProviderRejected,
        ),
        (
            ShopifyGraphQLError(
                "quantity must not exceed the shipped amount",
                extensions=[{"code": "VALIDATION_ERROR"}],
            ),
            ReturnProviderRejected,
        ),
        (
            ShopifyGraphQLError(
                "user error: order not found", extensions=[{"code": "USER_ERROR"}]
            ),
            ReturnProviderRejected,
        ),
        # Nothing was ever transmitted to Shopify, so claiming uncertainty would overstate.
        (
            ShopifyAuthError("Shopify refused the request as unauthenticated (HTTP 401)."),
            ReturnProviderUnavailable,
        ),
        (ShopifyRateLimitError(), ReturnProviderUnavailable),
        (
            ShopifyConfigError(
                "Shopify is not configured — set SHOPIFY_CLIENT_SECRET in the environment."
            ),
            ReturnProviderUnavailable,
        ),
        (ShopifyMalformedResponseError(), ReturnProviderUnavailable),
        (
            ShopifyMalformedResponseError("bad auth body", details={"phase": "token"}),
            ReturnProviderUnavailable,
        ),
        (ShopifyError("Shopify returned HTTP 400.", status_code=502), ReturnProviderUnavailable),
    ],
)
async def test_shopify_errors_map_onto_return_errors(
    recorded, monkeypatch, shopify_error, expected
):
    provider = make_provider(recorded)

    async def blow_up(*_args, **_kwargs):
        raise shopify_error

    # The seam that fails is the one the mutation travels on, since 14C.7O.
    monkeypatch.setattr(provider._write, "submit_return_create", blow_up)
    with pytest.raises(expected) as excinfo:
        await submit(provider)
    assert isinstance(excinfo.value, ReturnError)
    assert excinfo.value.status_code in (429, 502, 503, 504)


async def test_network_failure_raises_a_return_error_not_an_httpx_error(recorded):
    provider = make_provider(recorded, raise_exc=httpx.ConnectError("connection refused"))
    with pytest.raises(ReturnOutcomeUnknown) as excinfo:
        await submit(provider)
    assert not isinstance(excinfo.value, httpx.HTTPError)


async def test_a_non_json_error_page_is_unknown_after_a_mutation(recorded):
    """A 502 HTML page arrived after the mutation was transmitted: nobody knows what it did."""
    provider = make_provider(recorded, body="<html>502 Bad Gateway</html>", http_status=502)
    with pytest.raises(ReturnOutcomeUnknown):
        await submit(provider)


async def test_a_read_never_becomes_an_unknown_outcome(recorded, monkeypatch):
    """
    The same transport failure on a READ is plainly 'unavailable': a read cannot have created a
    return, so claiming uncertainty there would be its own kind of falsehood.
    """
    provider = make_provider(recorded)

    async def blow_up(*_args, **_kwargs):
        raise ShopifyTransportError("Shopify GraphQL request timed out.", status_code=504)

    monkeypatch.setattr(provider._client, "execute_graphql", blow_up)
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.get_return_status(RETURN_GID)
    assert not isinstance(excinfo.value, ReturnOutcomeUnknown)


# --------------------------------------------------------------------------- #
# 4. the target is Shopify's, or there is no submission
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "target,label",
    [
        (None, "no target at all"),
        (make_target(provider_line_reference=None), "missing line id"),
        (make_target(provider_line_reference=""), "blank line id"),
        (
            make_target(provider_line_reference="FulfillmentLineItem/9"),
            "gid without the shopify prefix",
        ),
        (
            make_target(provider_line_reference=FULFILLMENT_ORDER_LINE_GID),
            "the wrong (order-line) family",
        ),
        (make_target(provider_line_reference=f"{FULFILLMENT_LINE_GID}/extra"), "trailing junk"),
        (
            make_target(provider_line_reference=LINE_ITEM_GID),
            "order line id used as fulfillment id",
        ),
        (make_target(provider_line_reference=PRODUCT_GID), "product id as fulfillment id"),
        (make_target(provider_line_reference=RESOLVE_ROW_ID), "a bare RESOLVE row id"),
        (make_target(provider_order_reference=None), "missing order id"),
        (make_target(provider_order_reference="ord_canonical_1"), "non-global order id"),
        (make_target(provider_order_reference=PRODUCT_GID), "product id passed as the order id"),
        (make_target(provider_order_reference="gid://shopify/Order/"), "prefix with no id"),
        (make_target(provider_reason="REFUND_NEEDED"), "an unsupported reason token"),
    ],
)
async def test_an_unusable_target_is_refused_before_any_network_call(recorded, target, label):
    provider = make_provider(recorded, data=success_data())
    with pytest.raises(ReturnError) as excinfo:
        await provider.request_return(make_request(), target)
    assert excinfo.value.status_code == 400, label
    assert recorded.attempted is False, f"a request left the process for {label}"


async def test_the_wrong_id_family_is_refused_by_name_not_by_a_confusing_payload(recorded):
    """
    A FulfillmentOrderLineItem id is the near-miss this project actually made once. It must be
    refused with an explanation, not forwarded for Shopify to answer about someone else's object.
    """
    provider = make_provider(recorded, data=success_data())
    with pytest.raises(ReturnError) as excinfo:
        await provider.request_return(
            make_request(), make_target(provider_line_reference=FULFILLMENT_ORDER_LINE_GID)
        )
    assert "FulfillmentOrderLineItem" in excinfo.value.message
    assert "FulfillmentLineItem" in excinfo.value.message
    assert recorded.attempted is False


async def test_a_dict_impostor_is_not_a_return_target(recorded):
    """The target must be the typed value the resolver produces, not a caller-shaped substitute."""
    provider = make_provider(recorded, data=success_data())
    with pytest.raises(ReturnError) as excinfo:
        await provider.request_return(
            make_request(),
            {
                "provider_order_reference": ORDER_GID,
                "provider_line_reference": FULFILLMENT_LINE_GID,
                "provider_quantity": 1,
                "provider_reason": "DEFECTIVE",
            },
        )
    assert excinfo.value.status_code == 400
    assert recorded.attempted is False


async def test_the_provider_constructs_no_identifier_of_its_own(recorded):
    """
    The hard rule, restated for the new model: the provider forwards ONLY the ids it resolved from
    Shopify and assembles nothing. A product id, variant id, order id or RESOLVE row id never
    becomes a fulfillment line id.
    """
    provider = make_provider(recorded, data=success_data())
    for bogus in (
        PRODUCT_GID,
        VARIANT_GID,
        ORDER_GID,
        LINE_ITEM_GID,
        RESOLVE_ROW_ID,
        FULFILLMENT_ORDER_LINE_GID,
    ):
        with pytest.raises(ReturnError) as excinfo:
            await provider.request_return(make_request(), make_target(provider_line_reference=bogus))
        assert excinfo.value.status_code == 400, bogus
    assert recorded.attempted is False


# --------------------------------------------------------------------------- #
# 5. quantity: the shipped count is the ceiling, and nothing else is
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_quantity", [0, -3, None, 1.5, "1", True, 0.0])
async def test_invalid_quantity_fails_before_the_network(recorded, bad_quantity):
    provider = make_provider(recorded, data=success_data())
    with pytest.raises(ReturnError) as excinfo:
        await provider.request_return(
            make_request(), make_target(provider_quantity=bad_quantity)
        )
    assert excinfo.value.status_code == 400
    assert recorded.attempted is False


async def test_valid_quantity_is_forwarded_as_an_int(recorded):
    provider = make_provider(recorded, data=success_data())
    await provider.request_return(
        make_request(), make_target(provider_quantity=3, line_quantity=3)
    )
    forwarded = recorded.line_item["quantity"]
    assert forwarded == 3
    assert isinstance(forwarded, int) and not isinstance(forwarded, bool)


async def test_returning_more_than_was_shipped_is_refused_locally(recorded):
    """The structural bound: no more units come back than Shopify says went out."""
    provider = make_provider(recorded, data=success_data())
    with pytest.raises(ReturnError) as excinfo:
        await provider.request_return(
            make_request(), make_target(provider_quantity=4, line_quantity=1)
        )
    assert excinfo.value.status_code == 400
    assert "exceeds the 1 units" in excinfo.value.message
    assert recorded.attempted is False


@pytest.mark.parametrize("bad_ceiling", [None, 0, -1, "2", 1.5, True])
async def test_a_target_without_a_shipped_count_submits_nothing(recorded, bad_ceiling):
    """No `FulfillmentLineItem.quantity` read means no bound, and no bound means no submission."""
    provider = make_provider(recorded, data=success_data())
    with pytest.raises(ReturnError) as excinfo:
        await provider.request_return(
            make_request(), make_target(provider_quantity=1, line_quantity=bad_ceiling)
        )
    assert excinfo.value.status_code == 400
    assert recorded.attempted is False


@pytest.mark.parametrize(
    "shipped_quantity,requested,refused",
    [(1, 1, False), (3, 3, False), (3, 4, True), (1, 2, True)],
)
async def test_the_canonical_ceiling_is_the_shipped_line_quantity(
    recorded, shipped_quantity, requested, refused
):
    provider = make_provider(recorded, data=success_data())
    target = make_target(provider_quantity=requested, line_quantity=shipped_quantity)
    if refused:
        with pytest.raises(ReturnError):
            await provider.request_return(make_request(), target)
        assert recorded.attempted is False
    else:
        await provider.request_return(make_request(), target)
        assert recorded.line_item["quantity"] == requested


def test_remaining_quantity_is_not_a_ceiling_and_is_not_even_read():
    """
    Live finding (Tasks 14C.2/14C.3): on a PAID + FULFILLED order the fulfillment ORDER line
    reports `remainingQuantity` 0, because that field counts units AWAITING fulfilment. Using it
    as a return ceiling would refuse every legitimate return of a delivered item, so the target
    read never requests the field and the provider never holds such a value.
    """
    assert "remainingQuantity" not in _RETURN_TARGETS_QUERY
    source = _read(return_provider_module)
    assert "remaining_quantity" not in source  # no such field, variable, or parameter exists


@pytest.mark.parametrize("outstanding", [0, 1, 2])
async def test_a_shipped_line_is_returnable_whatever_its_outstanding_count(
    recorded, outstanding
):
    """
    The behavioural half of the same rule: a line that shipped 1 unit is a valid return target
    even though its fulfillment order line has nothing left to fulfil (live: `outstanding` reads
    0 for exactly this order). The provider never sees that count, so it cannot be misled by it.
    """
    assert outstanding >= 0  # Shopify's fulfilment number, never RESOLVE's ceiling
    resolver = make_provider(recorded, data=targets_data(shipped(quantity=1)))
    target = await resolver.resolve_return_target(make_request())
    assert target.line_quantity == 1
    recorded.graphql.clear()
    submitter = make_provider(recorded, data=success_data())
    await submitter.request_return(make_request(), target)
    assert recorded.line_item["quantity"] == 1


# --------------------------------------------------------------------------- #
# 6. target resolution from Shopify's own reads
# --------------------------------------------------------------------------- #
async def test_resolution_asks_shopify_and_returns_the_canonical_coordinates(recorded):
    provider = make_provider(
        recorded, data=targets_data(shipped(quantity=1, line_id=FULFILLMENT_LINE_GID))
    )
    target = await provider.resolve_return_target(make_request())
    assert isinstance(target, ReturnTarget)
    assert target.provider_order_reference == ORDER_GID
    assert target.provider_line_reference == FULFILLMENT_LINE_GID
    assert target.provider_quantity == 1
    assert target.line_quantity == 1
    assert target.provider_reason == "DEFECTIVE"
    assert "mutation" not in recorded.query  # resolution reads; it never writes
    assert "returnCreate" not in recorded.query


@pytest.mark.parametrize("linkage", [None, "", "   "])
async def test_an_unlinked_order_resolves_to_nothing_and_reaches_no_merchant(
    recorded, linkage
):
    """
    Without a persisted canonical linkage there is no honest way to name a merchant order. The
    refusal is 'unavailable', and no read is even attempted.
    """
    provider = make_provider(recorded, data=targets_data(shipped()))
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.resolve_return_target(make_request(provider_order_reference=linkage))
    assert "guess" in excinfo.value.message
    assert recorded.attempted is False


@pytest.mark.parametrize("linkage", ["ord_canonical_1", PRODUCT_GID, "gid://shopify/Order/"])
async def test_a_persisted_linkage_that_is_not_canonical_is_refused_without_a_read(
    recorded, linkage
):
    """
    A RESOLVE order row that somehow holds a non-Shopify order id is a data problem, not an
    invitation to try it anyway: 400, nothing sent, nothing asked.
    """
    provider = make_provider(recorded, data=targets_data(shipped()))
    with pytest.raises(ReturnError) as excinfo:
        await provider.resolve_return_target(make_request(provider_order_reference=linkage))
    assert excinfo.value.status_code == 400
    assert recorded.attempted is False


async def test_resolution_refuses_a_malformed_persisted_linkage_before_any_read(recorded):
    provider = make_provider(recorded, data=targets_data(shipped()))
    with pytest.raises(ReturnError) as excinfo:
        await provider.resolve_return_target(
            make_request(provider_order_reference="gid://shopify/Order/../x")
        )
    assert excinfo.value.status_code == 400
    assert recorded.attempted is False


async def test_an_order_with_nothing_shipped_yields_no_target(recorded):
    provider = make_provider(recorded, data=targets_data())
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.resolve_return_target(make_request())
    assert "no return was submitted" in excinfo.value.message
    assert recorded.mutations == []


@pytest.mark.parametrize("quantity", [0, None, -1])
async def test_a_shipped_line_with_no_positive_quantity_is_not_a_target(recorded, quantity):
    provider = make_provider(recorded, data=targets_data(shipped(quantity=quantity)))
    with pytest.raises(ReturnProviderUnavailable):
        await provider.resolve_return_target(make_request())
    assert recorded.mutations == []


async def test_two_unattributable_shipped_lines_are_not_resolved_by_picking_one(recorded):
    provider = make_provider(
        recorded,
        data=targets_data(
            shipped(line_id="gid://shopify/FulfillmentLineItem/1", line_item_id=LINE_ITEM_GID),
            shipped(
                line_id="gid://shopify/FulfillmentLineItem/2",
                line_item_id="gid://shopify/LineItem/7002",
            ),
        ),
    )
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.resolve_return_target(make_request())  # product id is not a Shopify gid
    assert "2 candidate shipped lines" in excinfo.value.message
    assert recorded.mutations == []


async def test_a_shopify_product_id_narrows_to_the_right_line(recorded):
    """When the canonical product IS a Shopify product, the matching line is unambiguous."""
    other_line = "gid://shopify/LineItem/7002"
    provider = make_provider(
        recorded,
        data=targets_data(
            shipped(line_id="gid://shopify/FulfillmentLineItem/1", line_item_id=LINE_ITEM_GID),
            shipped(line_id="gid://shopify/FulfillmentLineItem/2", line_item_id=other_line),
            order_lines=[
                (LINE_ITEM_GID, PRODUCT_GID),
                (other_line, "gid://shopify/Product/999999"),
            ],
        ),
    )
    target = await provider.resolve_return_target(make_request(product_id=PRODUCT_GID))
    assert target.provider_line_reference == "gid://shopify/FulfillmentLineItem/1"


async def test_a_product_that_was_never_shipped_yields_no_target(recorded):
    provider = make_provider(
        recorded,
        data=targets_data(
            shipped(line_id="gid://shopify/FulfillmentLineItem/1"),
            order_lines=[("gid://shopify/LineItem/777", "gid://shopify/Product/999999")],
        ),
    )
    with pytest.raises(ReturnProviderUnavailable):
        await provider.resolve_return_target(make_request(product_id=PRODUCT_GID))
    assert recorded.mutations == []


async def test_resolution_reports_the_quantity_it_read_not_one_it_inferred(recorded):
    resolver = make_provider(recorded, data=targets_data(shipped(quantity=2)))
    target = await resolver.resolve_return_target(make_request())
    assert (target.provider_quantity, target.line_quantity) == (2, 2)
    # ...and the submission uses exactly that quantity, nothing else.
    recorded.graphql.clear()
    submitter = make_provider(recorded, data=success_data())
    await submitter.request_return(make_request(), target)
    assert recorded.line_item["quantity"] == 2


async def test_a_request_cannot_name_the_line_it_wants_returned(recorded):
    """
    Even a request stuffed with merchant-side references cannot steer the target: the provider
    reads Shopify's fulfilment lines and returns only what it found there.
    """
    provider = make_provider(recorded, data=targets_data(shipped()))
    request = make_request()
    object.__setattr__(request, "provider_line_reference", "gid://shopify/FulfillmentLineItem/999")
    target = await provider.resolve_return_target(request)
    assert target.provider_line_reference == FULFILLMENT_LINE_GID


async def test_a_read_failure_during_resolution_is_unavailable_not_unknown(recorded, monkeypatch):
    """Nothing was submitted, so 'unknown' would be wrong; 'unavailable' is exactly right."""
    provider = make_provider(recorded)

    async def blow_up(*_args, **_kwargs):
        raise ShopifyTransportError("Shopify GraphQL request timed out.", status_code=504)

    monkeypatch.setattr(provider._client, "execute_graphql", blow_up)
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.resolve_return_target(make_request())
    assert not isinstance(excinfo.value, ReturnOutcomeUnknown)


# --------------------------------------------------------------------------- #
# 7. deterministic reason mapping onto reasons the live schema supports
# --------------------------------------------------------------------------- #
# Introspected read-only from the live 2026-07 schema (Task 14C.1 prep). No DAMAGED value.
LIVE_2026_07_RETURN_REASONS = {
    "SIZE_TOO_SMALL",
    "SIZE_TOO_LARGE",
    "UNWANTED",
    "NOT_AS_DESCRIBED",
    "WRONG_ITEM",
    "DEFECTIVE",
    "STYLE",
    "COLOR",
    "OTHER",
    "UNKNOWN",
}


def test_reason_mapping_is_supported_by_the_live_schema():
    assert RETURN_REASON_BY_ISSUE_TYPE == {
        "damaged": "DEFECTIVE",
        "defective": "DEFECTIVE",
        "quality_issue": "DEFECTIVE",
        "wrong_item": "WRONG_ITEM",
        "missing_item": "OTHER",
        "not_received": "OTHER",
        "other": "OTHER",
    }
    assert set(RETURN_REASON_BY_ISSUE_TYPE.values()) <= LIVE_2026_07_RETURN_REASONS
    assert "DAMAGED" not in RETURN_REASON_BY_ISSUE_TYPE.values()


@pytest.mark.parametrize(
    "issue_type,expected",
    [
        *RETURN_REASON_BY_ISSUE_TYPE.items(),
        ("unmapped_category", "OTHER"),
        (None, "OTHER"),
        ("", "OTHER"),
        ("  DaMaGeD  ", "DEFECTIVE"),
    ],
)
async def test_every_issue_type_maps_to_one_supported_reason(recorded, issue_type, expected):
    resolver = make_provider(recorded, data=targets_data(shipped()))
    target = await resolver.resolve_return_target(make_request(issue_type=issue_type))
    assert target.provider_reason == expected
    recorded.graphql.clear()
    submitter = make_provider(recorded, data=success_data())
    await submitter.request_return(make_request(issue_type=issue_type), target)
    assert recorded.line_item["returnReasonNote"] == expected


async def test_reason_mapping_is_deterministic_across_calls(recorded):
    provider = make_provider(recorded, data=targets_data(shipped()))
    reasons = set()
    for _ in range(3):
        target = await provider.resolve_return_target(make_request(issue_type="damaged"))
        reasons.add(target.provider_reason)
    assert reasons == {"DEFECTIVE"}


@pytest.mark.parametrize("reason", [None, "", "DAMAGED", "refund please", "customer note"])
async def test_an_unsupported_reason_never_reaches_the_merchant(recorded, reason):
    provider = make_provider(recorded, data=success_data())
    with pytest.raises(ReturnError) as excinfo:
        await provider.request_return(make_request(), make_target(provider_reason=reason))
    assert excinfo.value.status_code == 400
    assert recorded.attempted is False


# --------------------------------------------------------------------------- #
# 8. reconciliation: reading, never re-sending
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status,outcome",
    [("REQUESTED", "accepted"), ("OPEN", "accepted"), ("CLOSED", "completed")],
)
async def test_a_single_match_is_adopted_by_reading(recorded, status, outcome):
    provider = make_provider(
        recorded,
        data=order_returns_data(
            {
                "id": RETURN_GID,
                "status": status,
                "createdAt": "2026-09-26T12:00:30Z",
                "lines": [(1, "DEFECTIVE")],
            }
        ),
    )
    result = await provider.reconcile_return(make_submission())
    assert isinstance(result, ReturnReconciliation)
    assert result.state == "matched"
    assert result.provider_reference == RETURN_GID
    assert result.outcome == outcome
    assert "mutation" not in recorded.query
    assert "returnCreate" not in recorded.query
    assert len(recorded.graphql) == 1  # one exchange, and it was a read


async def test_reconciliation_reads_only_the_order_it_submitted_against(recorded):
    provider = make_provider(recorded, data=order_returns_data())
    await provider.reconcile_return(make_submission())
    assert recorded.payload["variables"] == {"id": ORDER_GID}
    assert "returnLineItems" in recorded.query  # the correlation fields come from here


async def test_a_return_predating_the_claim_is_not_adopted(recorded):
    provider = make_provider(
        recorded,
        data=order_returns_data(
            {
                "id": RETURN_GID,
                "status": "CLOSED",
                "createdAt": "2026-01-01T00:00:00Z",
                "lines": [(1, "DEFECTIVE")],
            }
        ),
    )
    result = await provider.reconcile_return(make_submission())
    assert result.state == "no_match"
    assert result.provider_reference is None


@pytest.mark.parametrize(
    "lines,expected",
    [
        ([(2, "DEFECTIVE")], "no_match"),  # different quantity
        ([(1, "WRONG_ITEM")], "no_match"),  # different reason
        ([(1, "defective")], "matched"),  # same token, other case: still a match
        ([], "no_match"),  # a return whose lines say nothing is not evidence
    ],
)
async def test_matching_uses_the_sent_quantity_and_reason(recorded, lines, expected):
    provider = make_provider(
        recorded,
        data=order_returns_data(
            {
                "id": RETURN_GID,
                "status": "REQUESTED",
                "createdAt": "2026-09-26T12:00:30Z",
                "lines": lines,
            }
        ),
    )
    assert (await provider.reconcile_return(make_submission())).state == expected


async def test_the_newest_return_is_not_adopted_just_for_being_newest(recorded):
    """
    Shopify's own reason words are not RESOLVE's token, and a recent timestamp is not a match:
    without the sent quantity and reason there is nothing to correlate, so nothing is adopted.
    """
    provider = make_provider(
        recorded,
        data=order_returns_data(
            {
                "id": RETURN_GID,
                "status": "REQUESTED",
                "createdAt": "2026-09-26T12:04:00Z",
                "lines": [(1, "CUSTOMER CHANGED MIND")],
            }
        ),
    )
    assert (await provider.reconcile_return(make_submission())).state == "no_match"


async def test_an_unparseable_created_at_is_never_filtered_on(recorded):
    """
    Shopify answered with something unreadable as a time. Dropping the timestamp filter can only
    widen the candidate set, and a widened set that matches twice stays ambiguous — the safe
    direction, and never a wrong adoption.
    """
    provider = make_provider(
        recorded,
        data=order_returns_data(
            {
                "id": RETURN_GID,
                "status": "REQUESTED",
                "createdAt": "not a timestamp",
                "lines": [(1, "DEFECTIVE")],
            }
        ),
    )
    assert (await provider.reconcile_return(make_submission())).state == "matched"


async def test_a_submission_without_a_reason_matches_nothing(recorded):
    provider = make_provider(
        recorded,
        data=order_returns_data(
            {
                "id": RETURN_GID,
                "createdAt": "2026-09-26T12:00:30Z",
                "lines": [(1, "DEFECTIVE")],
            }
        ),
    )
    assert (
        await provider.reconcile_return(make_submission(provider_reason=None))
    ).state == "no_match"


async def test_two_matching_returns_are_left_for_a_human(recorded):
    provider = make_provider(
        recorded,
        data=order_returns_data(
            {
                "id": "gid://shopify/Return/1111111111",
                "status": "REQUESTED",
                "createdAt": "2026-09-26T12:01:00Z",
                "lines": [(1, "DEFECTIVE")],
            },
            {
                "id": "gid://shopify/Return/2222222222",
                "status": "OPEN",
                "createdAt": "2026-09-26T12:02:00Z",
                "lines": [(1, "DEFECTIVE")],
            },
        ),
    )
    result = await provider.reconcile_return(make_submission())
    assert result.state == "ambiguous"
    assert result.provider_reference is None
    assert "human" in (result.detail or "")
    assert recorded.mutations == []


async def test_a_matching_return_with_an_unrecognized_status_is_not_rounded_off(recorded):
    provider = make_provider(
        recorded,
        data=order_returns_data(
            {
                "id": RETURN_GID,
                "status": "SOMETHING_NEW",
                "createdAt": "2026-09-26T12:00:30Z",
                "lines": [(1, "DEFECTIVE")],
            }
        ),
    )
    result = await provider.reconcile_return(make_submission())
    assert result.state == "ambiguous"
    assert result.provider_reference is None


@pytest.mark.parametrize("reference", [None, "", "   "])
async def test_a_submission_with_no_merchant_order_is_not_reconciled(recorded, reference):
    provider = make_provider(recorded, data=order_returns_data())
    result = await provider.reconcile_return(
        make_submission(provider_order_reference=reference)
    )
    assert result.state == "no_match"
    assert "Nothing was re-submitted" in (result.detail or "")
    assert recorded.mutations == []


@pytest.mark.parametrize("reference", ["ret_canonical_1", PRODUCT_GID])
async def test_a_submission_naming_a_non_order_is_not_reconciled(recorded, reference):
    """A recorded reference that is not a canonical Shopify order cannot be searched either."""
    provider = make_provider(recorded, data=order_returns_data())
    with pytest.raises(ReturnError) as excinfo:
        await provider.reconcile_return(make_submission(provider_order_reference=reference))
    assert excinfo.value.status_code == 400
    assert recorded.mutations == []


async def test_reconciliation_of_a_failing_read_stays_in_the_providers_hands(
    recorded, monkeypatch
):
    provider = make_provider(recorded)

    async def blow_up(*_args, **_kwargs):
        raise ShopifyTransportError("Shopify GraphQL request timed out.", status_code=504)

    monkeypatch.setattr(provider._client, "execute_graphql", blow_up)
    # A read that fails is an unavailable answer, never an unknown-outcome claim.
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.reconcile_return(make_submission())
    assert not isinstance(excinfo.value, ReturnOutcomeUnknown)


async def test_a_provider_that_cannot_reconcile_never_invents_an_adoption():
    """The contract's own default declines to correlate, which keeps the row unknown."""

    class _Probe(returns_base.ReturnProvider):
        async def request_return(self, request, target=None) -> ReturnAck:
            raise ReturnProviderUnavailable()

        async def get_return_status(self, provider_reference) -> ReturnStatusInfo:
            raise ReturnProviderUnavailable()

    fallback = await _Probe().reconcile_return(make_submission())
    assert fallback.state == "no_match"
    assert fallback.provider_reference is None


# --------------------------------------------------------------------------- #
# 9. no credential ever escapes
# --------------------------------------------------------------------------- #
async def test_secrets_are_absent_from_errors_logs_and_the_ack(recorded, caplog):
    provider = make_provider(
        recorded,
        errors=[
            {
                "message": f"denied; token={ACCESS_TOKEN} secret={CLIENT_SECRET} "
                f"Authorization: Bearer {ACCESS_TOKEN}",
                "extensions": {"code": "ACCESS_DENIED"},
            }
        ],
    )
    with caplog.at_level(logging.DEBUG, logger="app.tools"):
        with pytest.raises(ReturnError) as excinfo:
            await submit(provider)

    for secret in (ACCESS_TOKEN, CLIENT_SECRET, CLIENT_ID):
        assert secret not in excinfo.value.message

    ok = make_provider(Recorded(), data=success_data())
    ack = await submit(ok)
    assert set(vars(ack)) == {"provider_reference", "accepted"}

    logged = "\n".join(r.getMessage() for r in caplog.records)
    provider_state = repr(vars(provider))
    for secret in (ACCESS_TOKEN, CLIENT_SECRET):
        assert secret not in logged
        assert secret not in str(excinfo.value)
        assert secret not in provider_state
    # The provider holds no credential of its own at all — only the two injected capabilities.
    assert list(vars(provider)) == ["_client", "_write"]


async def test_provider_reuses_the_single_existing_credential_system():
    """No second token/client-id/secret surface was added alongside the client."""
    attrs = {n for n in dir(ShopifyReturnProvider) if not n.startswith("__")}
    for forbidden in (
        "client_id",
        "client_secret",
        "access_token",
        "obtain_access_token",
        "token_url",
    ):
        assert forbidden not in attrs
    source = _read(return_provider_module)
    assert "oauth/access_token" not in source
    assert "client_credentials" not in source
    assert "shopify_client_secret" not in source
    assert "get_settings" not in source


# --------------------------------------------------------------------------- #
# 10. get_return_status reads Shopify's own word back
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "shopify_status,outcome",
    [
        ("REQUESTED", "accepted"),
        ("OPEN", "accepted"),
        ("CLOSED", "completed"),
        ("DECLINED", "declined"),
        ("CANCELED", "cancelled"),
    ],
)
async def test_status_read_back_maps_onto_the_contract(recorded, shopify_status, outcome):
    provider = make_provider(
        recorded, data={"return": {"id": RETURN_GID, "status": shopify_status}}
    )
    info = await provider.get_return_status(RETURN_GID)
    assert isinstance(info, ReturnStatusInfo)
    assert info.provider_reference == RETURN_GID
    assert info.outcome == outcome
    assert info.detail == shopify_status
    assert "returnCreate" not in recorded.query
    assert "mutation" not in recorded.query


async def test_an_unknown_shopify_status_is_not_rounded_into_a_known_one(recorded):
    provider = make_provider(
        recorded, data={"return": {"id": RETURN_GID, "status": "SOMETHING_NEW"}}
    )
    with pytest.raises(ReturnError) as excinfo:
        await provider.get_return_status(RETURN_GID)
    assert "recognize" in excinfo.value.message


async def test_a_status_read_for_an_absent_reference_is_not_invented(recorded):
    provider = make_provider(recorded, data={"return": None})
    with pytest.raises(ReturnProviderUnavailable):
        await provider.get_return_status(RETURN_GID)


async def test_status_read_validates_the_reference_shape(recorded):
    provider = make_provider(
        recorded, data={"return": {"id": RETURN_GID, "status": "OPEN"}}
    )
    with pytest.raises(ReturnError) as excinfo:
        await provider.get_return_status("ret_canonical_1")
    assert excinfo.value.status_code == 400
    assert recorded.attempted is False


# --------------------------------------------------------------------------- #
# 11. isolation: no Authority Engine, no reasoning model, no DB, no wiring
# --------------------------------------------------------------------------- #
def _read(module) -> str:
    with open(module.__file__, encoding="utf-8") as handle:
        return handle.read()


def _imported_names(module) -> set[str]:
    tree = ast.parse(_read(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _top_level_imports(module) -> set[str]:
    """Only what a module imports when it is imported — branch-local imports excluded."""
    found: set[str] = set()
    for node in ast.parse(_read(module)).body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_provider_import_allowlist_excludes_authority_models_db_and_model_ai():
    imports = _imported_names(return_provider_module)
    assert imports == {
        "__future__",
        "logging",
        "re",
        "dataclasses",
        "datetime",
        "app.tools.returns.base",
        "app.tools.shopify.client",
        "app.tools.shopify.errors",
        "app.tools.shopify.models",
        "app.tools.shopify.return_write_client",
        "app.tools.shopify.sanitizer",
    }, imports
    for name in imports:
        lowered = name.lower()
        for forbidden in (
            "authorit",
            "orchestrat",
            "gemini",
            "genai",
            "google",
            "vertexai",
            "sqlalchemy",
            "fastapi",
            "httpx",
            "app.database",
            "app.models",
            "app.routers",
            "app.resolution",
            "app.config",
            "razorpay",
            "replacement",
            "refund",
        ):
            assert forbidden not in lowered, name


def test_provider_names_no_database_authority_or_model_symbols():
    source = _read(return_provider_module)
    for token in (
        "async_session",
        "get_db",
        "sessionmaker",
        "ReturnRecord",
        "OrderIssueRecord",
        "select(",
        "evaluate(",
        "ProposedAction",
        "Decision.",
        "generate_content",
        "Gemini",
        "run_return_executor",
        "Razorpay",
        "refund",
    ):
        assert token not in source, token


async def test_requesting_a_return_needs_no_database_session(recorded, monkeypatch):
    """Behavioral guard: the whole flow completes with RESOLVE's sessionmaker sabotaged."""
    import app.database as database_module

    class _ExplodingSession:
        def __call__(self, *_a, **_k):
            raise AssertionError("the Shopify return provider opened a database session")

        async def __aenter__(self):
            raise AssertionError("the Shopify return provider opened a database session")

    monkeypatch.setattr(database_module, "async_session", _ExplodingSession())
    resolver = make_provider(recorded, data=targets_data(shipped()))
    target = await resolver.resolve_return_target(make_request())
    submitter = make_provider(recorded, data=success_data())
    ack = await submitter.request_return(make_request(), target)
    assert ack.accepted is True


def test_production_seams_are_untouched_by_this_task():
    """
    Isolation after Task 14C.7D: the factory can name a Shopify provider, and the discovery layer
    can carry Shopify's own product identity, and everything that was true before about who may NOT
    reach Shopify still holds.

    The return contract, the unconnected provider and the orchestrator still contain no Shopify
    reference at all; the provider still does not know the factory exists; and the factory still
    does not import Shopify at module scope — selecting 'shopify' is the only thing that pulls it
    in, so RESOLVE keeps importing and running with no Shopify present.

    The missions router is the one seam 14C.7D changed on purpose, and the check below is narrower
    and more mechanical than the word-match it replaces: the router may name the four identity
    symbols and NOTHING ELSE in code, and may hold no Shopify client, read, mutation or linkage
    writer of any kind. Carrying an identifier Shopify issued is not the same as talking to Shopify.
    """
    assert get_return_provider("none").name == "none"
    assert get_return_provider().name == "none"  # the configured default, unchanged
    assert unconnected_pkg.UnconnectedReturnProvider().name == "none"

    for label, module in {
        "returns contract": returns_base,
        "unconnected provider": unconnected_pkg,
        "orchestrator": orchestrator_module,
    }.items():
        source = _read(module)
        assert "ShopifyReturnProvider" not in source, label
        assert "shopify" not in source.lower(), label

    router_source = _read(missions_module)
    assert "ShopifyReturnProvider" not in router_source
    for banned in (
        "get_shopify_client",
        "ShopifyReadOnlyClient",
        "execute_graphql",
        "read_catalog",
        "read_order_reference",
        "link_order_to_shopify_order",
        "orderCreate",
        "draftOrderCreate",
        "returnCreate",
        "RETURN_PROVIDER",
        "PRODUCT_SEARCH_PROVIDER",
    ):
        assert banned not in router_source, banned
    # Every shopify-named identifier in the router's CODE (docstrings and comments excluded) is one
    # of the identity fields 14C.7D introduced, plus the one symbol 14C.7E legitimately added: the
    # variant-clarification endpoint has to know WHICH source a result came from before it will
    # re-ask a merchant catalog about it, and that switch is the `SHOPIFY_SOURCE` constant. Nothing
    # else may creep in unnoticed — the set is an equality, not a subset, on purpose.
    tree = ast.parse(router_source)
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            named.add(node.attr)
        elif isinstance(node, ast.Name):
            named.add(node.id)
        elif isinstance(node, ast.keyword) and node.arg:
            named.add(node.arg)
    assert {n for n in named if "shopify" in n.lower()} == {
        "SHOPIFY_SOURCE",
        "shopify_identity_problem",
        "shopify_product_gid",
        "shopify_variant_gid",
        "shopify_store_domain",
    }

    # The factory's Shopify import lives inside the configuration branch, never at module scope.
    assert _top_level_imports(returns_pkg) == {
        "__future__",
        "app.config",
        "app.tools.returns.base",
        "app.tools.returns.unconnected",
    }
    assert "app.tools.shopify" in _read(returns_pkg)  # reachable only from inside the branch

    import app.tools.shopify as shopify_pkg

    assert "ShopifyReturnProvider" not in shopify_pkg.__all__
    assert "get_return_provider" not in _imported_names(shopify_return_provider_module)


def test_write_capability_reaches_only_the_one_controlled_return_mutation():
    """
    Scope discipline after Task 14C.6: `write_returns` is now declared because the installed app
    really holds it, so the meaningful check is no longer "no write scope" but "the write surface
    is exactly one mutation document".

    Everything asserted here was true before 14C.6 and is still required: the read queries carry
    no mutation at all, the provider exposes exactly one `mutation …` document, and none of the
    other operations `write_returns` would permit appears anywhere in it.
    """
    from app.tools.shopify.client import _ORDERS_QUERY, _PRODUCTS_QUERY, _RETURNS_QUERY
    from app.tools.shopify.data_use import SHOPIFY_DATA_USE

    assert SHOPIFY_DATA_USE.requested_scopes[-1] == "write_returns"
    assert SHOPIFY_DATA_USE.intended_write_mutations == ("returnCreate",)
    assert sum(1 for s in SHOPIFY_DATA_USE.requested_scopes if not s.startswith("read_")) == 1
    reads = (
        _PRODUCTS_QUERY
        + _ORDERS_QUERY
        + _RETURNS_QUERY
        + _FULFILLMENT_LINE_ITEMS_QUERY
        + _RETURN_TARGETS_QUERY
        + _ORDER_RETURNS_QUERY
        + _ORDER_REFERENCE_QUERY
        + _ORDER_LINE_IDENTITY_QUERY
    ).lower()
    assert "mutation" not in reads
    provider_source = _read(return_provider_module)
    # The provider's only write document is the return mutation — nothing else. Checked against
    # the string literals (the GraphQL documents it can actually send), so a comment naming a
    # forbidden operation cannot trip it and an uncommented one still would.
    literals = [
        node.value
        for node in ast.walk(ast.parse(provider_source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert sum(1 for text in literals if text.lstrip().startswith("mutation ")) == 1
    for forbidden in (
        "fulfillmentCreate",
        "returnProcess",
        "returnApproveRequest",
        "returnDeclineRequest",
        "returnCancel",
        "returnClose",
        "returnReopen",
        "returnRequest(",
        "orderEdit",
        "refundCreate",
    ):
        assert not any(forbidden in text for text in literals), forbidden
    # Status reads use the singular `return(id:)`; the per-order returns read lives in the client.
    assert "returns(first:" not in provider_source
    assert "return(id: $id)" in provider_source
