"""
Task 14C.7N — the read-only boundary is a property of code, and these tests are the proof.

Before this task, "RESOLVE's Shopify client is read-only" was a description of what the client's
methods happened to pass to `execute_graphql`. Nothing checked the document itself. These tests
therefore do not ask whether a mutation would fail at Shopify — they ask how many HTTP requests
were *attempted*, and require the answer to be exactly zero. The token exchange is counted too:
it is an HTTP call, and a refusal that happens after it has already leaked a credential request
and proved nothing.

The other half of the job is to prove the gate is narrow. It refuses everything RESOLVE must not
do, and it must not refuse the reads RESOLVE depends on, nor reach into the merchant-owned purchase
channel, whose write capability belongs to a different app and is not RESOLVE's boundary to enforce.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import pathlib
import sys

import httpx
import pytest

from app.tools.shopify import client as client_module
from app.tools.shopify import get_shopify_client
from app.tools.shopify.client import (
    _CATALOG_QUERY,
    _FULFILLMENT_LINE_ITEMS_QUERY,
    _ORDERS_QUERY,
    _ORDER_LINE_IDENTITY_QUERY,
    _ORDER_REFERENCE_QUERY,
    _ORDER_RETURNS_QUERY,
    _PRODUCTS_QUERY,
    _RETURN_TARGETS_QUERY,
    _RETURNS_QUERY,
    ShopifyReadOnlyClient,
    _document_is_permitted,
)
from app.tools.shopify.data_use import SHOPIFY_DATA_USE
from app.tools.shopify.document_gate import MUTATION, classify
from app.tools.shopify.errors import ShopifyConfigError, ShopifyReadOnlyViolation
from app.tools.shopify.sanitizer import redact, reset_registered_secrets


def _channel_root() -> pathlib.Path:
    """
    Where the merchant-owned package lives, or a loud failure — never a silent skip.

    Two tests here depend on it: one re-reads the 2026-07 disposal evidence measured in 14C.7M
    instead of remembering it, the other proves the channel's own documents are still intact. A
    skipped version of either would let this file claim a boundary it had not checked.
    """
    configured = os.environ.get("MERCHANT_CHANNEL_ROOT")
    candidates = ([pathlib.Path(configured)] if configured else []) + [
        pathlib.Path(__file__).resolve().parents[2] / "merchant-channel"
    ]
    for path in candidates:
        if (path / "merchant_channel" / "channel.py").is_file():
            return path
    raise AssertionError(
        "the merchant-channel package is not visible to this run; set MERCHANT_CHANNEL_ROOT and "
        "mount it read-only, so the disposal operations verified in 14C.7M are re-read from its "
        "schema evidence rather than recalled from memory."
    )


_CHANNEL_ROOT = _channel_root()
if str(_CHANNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(_CHANNEL_ROOT))

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "shppa_SUPERSECRETVALUE1234"
ACCESS_TOKEN = "shpat_LIVETOKENABCD5678"
HMAC_SIGNATURE = "deadbeef" * 8

TOKEN_URL = "https://example.myshopify.com/admin/oauth/access_token"

# The five disposal shapes 14C.7M found by read-only introspection of API 2026-07
# (`merchant-channel/schema/cancel_2026-07.json`), each written the way its verified argument
# types say it must be written. They exist on Shopify. None of them exists in RESOLVE, and this
# file is what makes "none of them exists in RESOLVE" checkable rather than aspirational.
DISPOSAL_OPERATIONS = {
    "orderCancel": """
mutation ResolveCancel($id: ID!) {
  orderCancel(orderId: $id, reason: CUSTOMER, restock: true) {
    job { id }
    orderCancelUserErrors { field message }
  }
}
""",
    "orderClose": """
mutation ResolveClose($input: OrderCloseInput!) {
  orderClose(input: $input) { order { id } userErrors { field message } }
}
""",
    "orderDelete": """
mutation ResolveDelete($id: ID!) {
  orderDelete(orderId: $id) { deletedId userErrors { field message } }
}
""",
    "draftOrderDelete": """
mutation ResolveDraftDelete($input: DraftOrderDeleteInput!) {
  draftOrderDelete(input: $input) { deletedId userErrors { field message } }
}
""",
    "transactionVoid": """
mutation ResolveVoid($id: ID!) {
  transactionVoid(parentTransactionId: $id) { transaction { id } userErrors { field message } }
}
""",
}

# Operations `write_returns` would permit but RESOLVE does not declare: the gate must refuse these
# locally too, because the scope grant is not the limit.
UNDECLARED_RETURN_WRITES = ("returnClose", "returnApproveRequest", "returnDeclineRequest",
                            "returnProcess", "returnReopen", "returnCancel")

MALFORMED = [
    pytest.param("", id="empty"),
    pytest.param("   \n\t ", id="whitespace-only"),
    pytest.param("drop table orders", id="not-a-document"),
    pytest.param("query Broken($x: Int!", id="unbalanced-braces"),
    pytest.param("fragment F on Order { id }", id="fragment-definition-only"),
    pytest.param("query Empty { }", id="operation-with-no-field"),
    pytest.param("query A { products { id } } mutation B { orderCancel(orderId: 1) }", id="two-root-operations"),
    pytest.param("{", id="lone-brace"),
    pytest.param(None, id="not-text"),
    pytest.param(42, id="a-number"),
]


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


def _source(module) -> str:
    return inspect.getsource(module)


def _executable_source(module) -> str:
    """
    The module's CODE, with every docstring removed.

    Prose in this project explains boundaries by naming what sits on the other side of them, so a
    scan over raw text would either fail on an honest comment or have to be weakened. Stripping
    docstrings keeps the claim where it matters: nothing the interpreter executes may know about a
    write it is not supposed to refuse.
    """
    tree = ast.parse(_source(module))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
    return ast.unparse(tree)


def _counting_transport(answer: dict | None = None):
    """
    A transport that records every request it is even *offered*.

    Anything in `seen` is an HTTP call that left RESOLVE; the assertion these tests are built on is
    that a refused document leaves this list empty.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": ACCESS_TOKEN, "expires_in": 3600, "token_type": "bearer"}
            )
        return httpx.Response(200, json=answer if answer is not None else {"data": {"shop": {"name": "RESOLVE Commerce Lab", "currencyCode": "INR"}}})

    client = ShopifyReadOnlyClient(
        store_domain="example.myshopify.com",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx.MockTransport(handler),
    )
    return client, seen


# --------------------------------------------------------------------------- #
# 1. a mutation handed to the read-only client dies locally
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("operation", sorted(DISPOSAL_OPERATIONS))
async def test_a_cancellation_or_void_mutation_is_refused_with_exactly_zero_http_calls(
    operation,
):
    client, seen = _counting_transport()
    document = DISPOSAL_OPERATIONS[operation]

    with pytest.raises(ShopifyReadOnlyViolation) as excinfo:
        await client.execute_graphql(document, {"id": "gid://shopify/Order/1001"})

    assert seen == [], f"{operation} reached the transport"
    assert excinfo.value.status_code == 500
    assert operation in str(excinfo.value)
    # A refusal that has not been registered as a secret could still print one; the message is the
    # surface an operator reads, so it must be credential-free in itself.
    assert CLIENT_SECRET not in str(excinfo.value)
    assert ACCESS_TOKEN not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 2. subscriptions are not reads either
# --------------------------------------------------------------------------- #
async def test_a_subscription_document_is_refused_before_the_transport():
    client, seen = _counting_transport()
    with pytest.raises(ShopifyReadOnlyViolation) as excinfo:
        await client.execute_graphql(
            "subscription ResolveWatchOrders {\n  orderUpdated(orderId: \"gid://shopify/Order/1\") { id }\n}\n"
        )
    assert seen == []
    assert "subscription" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 3. fail closed: an unreadable document is not a read
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("document", MALFORMED)
async def test_a_document_that_cannot_be_identified_as_one_read_is_refused(document):
    """The gate does not need to know what a dangerous document is; it only needs to fail to be sure."""
    client, seen = _counting_transport()
    with pytest.raises(ShopifyReadOnlyViolation):
        await client.execute_graphql(document)
    assert seen == []


# --------------------------------------------------------------------------- #
# 4. the gate did not lock RESOLVE out of its own reads
# --------------------------------------------------------------------------- #
async def test_a_query_is_still_sent_and_the_call_count_is_not_zero():
    client, seen = _counting_transport({"data": {"products": {"edges": []}}})
    assert await client.execute_graphql(_PRODUCTS_QUERY, {"first": 5}) == {"products": {"edges": []}}
    assert seen == [TOKEN_URL, "https://example.myshopify.com/admin/api/2026-07/graphql.json"]


async def test_every_document_the_read_only_client_ships_passes_its_own_gate():
    """
    A boundary that refused RESOLVE's reads would be a new bug, not a safety gain.

    Each read method is exercised through the counting transport, so this proves both halves at
    once: the classifier accepts every shipped query, and every read path still reaches Shopify.
    """
    reads = {
        "read_products": (_PRODUCTS_QUERY, {"first": 5}),
        "read_catalog": (_CATALOG_QUERY, {"first": 5, "variantsPerProduct": 5, "searchQuery": None}),
        "read_orders": (_ORDERS_QUERY, {"first": 5}),
        "read_returns": (_RETURNS_QUERY, {"first": 5}),
        "read_fulfillment_line_items": (_FULFILLMENT_LINE_ITEMS_QUERY, {"id": "gid://shopify/Order/1"}),
        "read_order_reference": (_ORDER_REFERENCE_QUERY, {"id": "gid://shopify/Order/1"}),
        "read_order_line_identity": (_ORDER_LINE_IDENTITY_QUERY, {"id": "gid://shopify/Order/1"}),
        "read_return_targets": (_RETURN_TARGETS_QUERY, {"id": "gid://shopify/Order/1"}),
        "read_order_returns": (_ORDER_RETURNS_QUERY, {"id": "gid://shopify/Order/1"}),
    }
    for name, (document, variables) in reads.items():
        assert classify(document).is_read, name
        assert _document_is_permitted(document) is None, name
    assert len(reads) == 9

    client, seen = _counting_transport(
        {"data": {"orders": {"edges": []}, "products": {"edges": []}}}
    )
    for name in ("read_products", "read_orders", "read_returns"):
        assert await getattr(client, name)(first=5) == []
    assert len(seen) == 4  # one token exchange, then exactly three reads


# --------------------------------------------------------------------------- #
# 5. the 14C.7M schema evidence, checked against the gate rather than remembered
# --------------------------------------------------------------------------- #
async def test_the_disposal_operations_measured_in_2026_07_are_the_ones_this_gate_refuses():
    """
    The evidence file is the finding; this test is the enforcement of it.

    14C.7M introspected every cancellation/void/delete/close-shaped operation API 2026-07 offers
    and wrote the result down. Rather than remember that list, this reads it back and hands RESOLVE's
    client one document per operation it names. Every one must be refused locally, and none of them
    may be a document RESOLVE's own app could send.
    """
    evidence = json.loads(
        (_CHANNEL_ROOT / "schema" / "cancel_2026-07.json").read_text(encoding="utf-8")
    )
    assert evidence["api_version"] == "2026-07"
    assert evidence["read_only"] is True
    assert evidence["documents_sent"]["kinds"] == ["query"], "the evidence itself sent no mutation"

    measured = {entry["operation"] for entry in evidence["operations"]}
    assert {
        "orderCancel",
        "orderClose",
        "orderDelete",
        "draftOrderDelete",
        "transactionVoid",
    } <= measured, sorted(measured)
    assert "draftOrderCancel" not in measured  # 14C.7M: this version has no such operation

    client, seen = _counting_transport()
    for name in sorted(measured):
        arguments = evidence_operation_arguments(evidence, name)
        document = (
            f"mutation Probe{{{name}({arguments}){{ userErrors {{ field message }} }}}}\n"
        )
        verdict = classify(document)
        assert verdict.kind == MUTATION and verdict.root_fields == (name,), name
        assert not verdict.is_read, name
        with pytest.raises(ShopifyReadOnlyViolation):
            await client.execute_graphql(document)
    assert seen == [], "a disposal operation reached the transport"


def evidence_operation_arguments(evidence: dict, name: str) -> str:
    """`a: $a, b: $b` for one operation's verified argument names — placeholders, never values."""
    entry = next(item for item in evidence["operations"] if item["operation"] == name)
    return ", ".join(f"{field}: ${field}" for field in sorted(entry["arguments"]))


def test_cancellation_is_still_unverified_and_still_unimplemented():
    """
    Requirement 7 of this task, pinned: nothing here implemented cancel.

    The six behavioural unknowns 14C.7M recorded are still six, still unknown, and RESOLVE still
    has no cancellation code path — the gate refuses the operations because they are writes RESOLVE
    does not declare, not because anyone worked out what they do.
    """
    evidence = json.loads(
        (_CHANNEL_ROOT / "schema" / "cancel_2026-07.json").read_text(encoding="utf-8")
    )
    assert len(evidence["unknowns"]) == 6
    assert all("UNKNOWN" in unknown.upper() for unknown in evidence["unknowns"])
    for behavioural in ("pre_completion_behavior", "post_completion_behavior", "scope_evidence"):
        assert "UNKNOWN" in evidence[behavioural].upper(), behavioural

    backend = pathlib.Path(__file__).resolve().parents[1]
    shipped = [
        path.read_text(encoding="utf-8")
        for path in (backend / "app").rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    for name in ("orderCancel", "orderClose", "orderDelete", "draftOrderDelete", "transactionVoid"):
        assert not any(f"mutation {name[0].upper()}{name[1:]}" in source for source in shipped), name
        assert not any(f"{name}(" in source for source in shipped), name


# --------------------------------------------------------------------------- #
# 6. the refusal itself is safe
# --------------------------------------------------------------------------- #
async def test_a_refusal_names_no_credential_no_header_and_not_the_document():
    """
    A refused document is caller-supplied data. Quoting it back would put whatever it carried —
    an order id, a customer field, a token someone pasted into a query — into a log line and an
    exception message, which is the leak this project has been careful about everywhere else.
    """
    hostile = (
        "mutation Leak {\n"
        f"  orderCancel(orderId: \"gid://shopify/Order/1001\", reason: CUSTOMER, restock: true) {{\n"
        f"    job {{ id }}\n"
        f"    customer {{ email }}  # SHOPIFY_CLIENT_SECRET=shppa_SUPERSECRETVALUE1234\n"
        f"  }}\n"
        f"}}\n"
    )
    client, seen = _counting_transport()
    await client.obtain_access_token()  # so the real token exists and could be leaked

    with pytest.raises(ShopifyReadOnlyViolation) as excinfo:
        await client.execute_graphql(hostile)

    message = str(excinfo.value)
    assert seen == [TOKEN_URL], "the refusal must not add a request of its own"
    for secret in (CLIENT_SECRET, ACCESS_TOKEN, HMAC_SIGNATURE):
        assert secret not in message
    for shape in ("X-Shopify-Access-Token", "Authorization", "Bearer"):
        assert shape not in message
    # The document's own text: none of it is echoed. Only the operation name is.
    for leaked in ("gid://shopify/Order/1001", "restock", "customer", "email", "Leak {\n"):
        assert leaked not in message, leaked
    assert "orderCancel" in message  # named, so an operator can see what was refused
    assert redact(message) == message  # nothing in it needed scrubbing in the first place


# --------------------------------------------------------------------------- #
# 7. the gate runs before configuration, too
# --------------------------------------------------------------------------- #
async def test_an_unconfigured_client_still_refuses_a_mutation_locally():
    """`not configured` must never be a reason to let a write through, or to try one and fail later."""
    client = ShopifyReadOnlyClient(
        store_domain="", client_id="", client_secret="", transport=httpx.MockTransport(
            lambda request: pytest.fail("a network call was attempted")
        )
    )
    with pytest.raises(ShopifyReadOnlyViolation):
        await client.execute_graphql(DISPOSAL_OPERATIONS["orderCancel"], {"id": "gid://shopify/Order/1"})
    # The configuration error is still the answer for a read, so the two refusals stay distinguishable.
    with pytest.raises(ShopifyConfigError):
        await client.read_products()


# --------------------------------------------------------------------------- #
# 8. the declared write is no longer this client's business at all
# --------------------------------------------------------------------------- #
async def test_the_declared_return_write_is_refused_here_and_lives_on_its_own_capability():
    """
    14C.7N's exception is gone. The one write RESOLVE declares used to transit this client because
    this client was also its transport; 14C.7O moved it to `ShopifyReturnWriteClient`, so the
    read-only object now refuses `returnCreate` exactly like any other mutation — with zero HTTP.

    What has NOT moved is the narrowness: `write_returns` also permits `returnClose`,
    `returnApproveRequest` and `returnProcess`, and RESOLVE declares none of those either. The
    declaration lives in `data_use.py` and is consulted by the capability, never here.
    """
    assert SHOPIFY_DATA_USE.intended_write_mutations == ("returnCreate",)
    assert SHOPIFY_DATA_USE.enforces_read_only_transport is True

    declared = (
        "mutation ResolveReturnCreate($returnInput: ReturnInput!) {\n"
        "  returnCreate(returnInput: $returnInput) { userErrors { field message } return { id status } }\n"
        "}\n"
    )
    assert _document_is_permitted(declared) is not None  # refused, and the message says why

    client, seen = _counting_transport(
        {"data": {"returnCreate": {"return": {"id": "gid://shopify/Return/9"}, "userErrors": []}}}
    )
    with pytest.raises(ShopifyReadOnlyViolation):
        await client.execute_graphql(declared, {"returnInput": {}})
    assert seen == [], "the read-only client transmitted a write"

    for operation in UNDECLARED_RETURN_WRITES:
        with pytest.raises(ShopifyReadOnlyViolation):
            await client.execute_graphql(
                f"mutation Undeclared {{\n  {operation}(id: \"gid://shopify/Return/9\") {{ userErrors {{ field message }} }}\n}}\n"
            )
    # Seven writes offered — the declared one and six others — and not one request built.
    assert seen == []


def test_the_read_only_client_holds_no_write_allowlist_of_any_kind():
    """
    Requirement 2 of 14C.7O, pinned statically: ZERO mutation-exception behavior in this class.

    The check is on executable code, not prose — a comment is allowed to explain that `returnCreate`
    exists elsewhere, while the code is not allowed to know its name. So docstrings are stripped and
    what remains must contain no reference to the declaration, to the data-use record, to the write
    capability, or to a mutation of any kind. That is what "no future edit can quietly reopen the
    exception" means in a test.
    """
    code = _executable_source(client_module)
    for absent in (
        "returnCreate",
        "intended_write_mutations",
        "SHOPIFY_DATA_USE",
        "data_use",
        "ShopifyReturnWriteClient",
        "ShopifyWriteCapabilityViolation",
        "declared_write_operations",
        "allowlist",
        "mutation",
        "submit_return",
    ):
        assert absent not in code.lower(), absent

    imported = {
        node.module
        for node in ast.parse(_source(client_module)).body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.tools.shopify.data_use" not in imported
    assert "app.tools.shopify.return_write_client" not in imported
    assert not any(name.endswith("data_use") or name.endswith("return_write_client")
                   for name in imported), imported

    for forbidden in ("execute_graphql_mutation", "submit_return", "submit_return_create",
                      "create_order", "update_order", "write", "mutate"):
        assert not hasattr(ShopifyReadOnlyClient, forbidden), forbidden


async def test_a_mutation_that_bundles_two_root_fields_cannot_smuggle_a_declared_write():
    client, seen = _counting_transport()
    with pytest.raises(ShopifyReadOnlyViolation):
        await client.execute_graphql(
            "mutation Bundled {\n  returnCreate(returnInput: $a) { return { id } }\n  returnClose(id: $b) { return { id } }\n}\n"
        )
    assert seen == []


# --------------------------------------------------------------------------- #
# 9. the merchant-owned channel keeps its own capability — this is not a global ban
# --------------------------------------------------------------------------- #
def _channel_modules() -> list[pathlib.Path]:
    root = _channel_root() / "merchant_channel"
    return sorted(root.glob("*.py"))


def test_the_read_only_gate_was_never_installed_in_the_merchant_channel():
    """
    The channel buys through a different app, and its right to write is not RESOLVE's to revoke.

    What belongs on that side is its own credential and its own gateway. This asserts the boundary
    stayed on RESOLVE's side of the line: nothing in the channel imports the classifier or the
    violation, and its purchase documents are still the mutations they were.
    """
    for path in _channel_modules():
        source = path.read_text(encoding="utf-8")
        for foreign in ("document_gate", "ShopifyReadOnlyViolation", "ShopifyReadOnlyClient",
                        "_document_is_permitted",
                        # 14C.7O's two new modules: RESOLVE's credential plumbing and RESOLVE's
                        # return capability are no more the channel's to borrow than the gate is.
                        "ShopifyAdminTransport", "ShopifyReturnWriteClient", "admin_transport",
                        "return_write_client", "ShopifyWriteCapabilityViolation",
                        "get_shopify_clients", "SHOPIFY_DATA_USE"):
            assert foreign not in source, (path.name, foreign)

    from merchant_channel import documents as channel_documents

    assert classify(channel_documents.DRAFT_ORDER_CREATE).root_fields == ("draftOrderCreate",)
    assert classify(channel_documents.DRAFT_ORDER_COMPLETE).kind == "mutation"
    assert classify(channel_documents.READ_BACK_BY_CORRELATION).is_read
    assert classify(channel_documents.SHOP_IDENTITY).is_read


async def test_the_channel_gateway_still_sends_its_authorised_create_mutation():
    """
    Not a declaration — a transport. If a RESOLVE-side boundary had quietly widened, this is the
    call that would stop happening, and a purchase channel that cannot buy is not a channel.

    The channel is pointed at a scripted transport here, exactly as its own suite does, and the
    assertions are about what left the process: its URL, its header, its create document.
    """
    from merchant_channel.config import ChannelConfig, Secret
    from merchant_channel.gateway import ShopifyAdminGateway

    channel_token = "shpua_channel-sided-is-not-resolves-token"
    config = ChannelConfig(
        store_domain="resolve-commerce-lab.myshopify.com",
        api_version="2026-07",
        access_token=Secret(channel_token),
        linkage_secret=Secret("a-channel-side-secret"),
        resolve_base_url="http://resolve.test",
        expected_currency="INR",
        expected_shop_name="RESOLVE Commerce Lab",
        source="merchant-purchase-channel",
    )
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append({"url": str(request.url), "query": body["query"], "headers": dict(request.headers)})
        if "draftOrderCreate" in body["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "draftOrderCreate": {
                            "draftOrder": {"id": "gid://shopify/DraftOrder/1"},
                            "userErrors": [],
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "draftOrderComplete": {
                        "draftOrder": {
                            "id": "gid://shopify/DraftOrder/1",
                            "order": {
                                "id": "gid://shopify/Order/900000000000001",
                                "name": "#1091",
                                "poNumber": "RESOLVE-order_t-000000000000",
                                "currencyCode": "INR",
                                "lineItems": {"edges": []},
                            },
                        },
                        "userErrors": [],
                    }
                }
            },
        )

    gateway = ShopifyAdminGateway(
        config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    order = await gateway.create_order(
        correlation_id="RESOLVE-order_t-000000000000",
        variant_gid="gid://shopify/ProductVariant/52923834990879",
        quantity=1,
    )
    assert order.order_id == "gid://shopify/Order/900000000000001"
    assert [classify(entry["query"]).kind for entry in seen] == ["mutation", "mutation"]
    assert seen[0]["headers"]["x-shopify-access-token"] == channel_token

    # And the same two documents, handed to RESOLVE's read-only client instead, never leave it.
    client, resolvable = _counting_transport()
    for entry in seen:
        with pytest.raises(ShopifyReadOnlyViolation):
            await client.execute_graphql(entry["query"])
    assert resolvable == [], "RESOLVE must not transmit the channel's purchase mutations"


class _Unconfigured:
    shopify_store_domain = ""
    shopify_client_id = ""
    shopify_client_secret = ""
    shopify_api_version = "2026-07"


async def test_the_client_the_whole_app_shares_already_carries_the_gate():
    """
    The boundary has to hold on the object production builds, not only on ones a test constructs.

    `get_shopify_client()` is the single factory RESOLVE uses for discovery, linkage and every
    read; its client refuses a disposal mutation without touching the network, and does so without
    even needing Shopify to be configured.
    """
    client = get_shopify_client(settings=_Unconfigured())
    assert isinstance(client, ShopifyReadOnlyClient)
    with pytest.raises(ShopifyReadOnlyViolation):
        await client.execute_graphql(DISPOSAL_OPERATIONS["transactionVoid"])
    # A read on an unconfigured store is still the configuration error, unchanged: the gate did not
    # swallow it, and the two refusals stay distinguishable.
    with pytest.raises(ShopifyConfigError):
        await client.read_orders()
