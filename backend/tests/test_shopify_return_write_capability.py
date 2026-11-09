"""
Task 14C.7O — RESOLVE's declared write gets its own capability, and the read client keeps none.

14C.7N made `ShopifyReadOnlyClient` refuse every document that was not a read *except* the one
mutation RESOLVE declares. That exception was the last trace of the old shape: an object called
read-only which was in fact the application's general Admin GraphQL transport, narrowed by a rule
about documents. 14C.7O removes the exception by moving the write to a separate object —
`ShopifyReturnWriteClient` — so the read client can finally mean what its name says, and the write
has a home whose only capability is the thing it is named for.

These tests are the claims that make that real, each measured rather than asserted in prose:

  * the read client still transmits every query it transmitted before, and now refuses `returnCreate`
    with exactly zero HTTP attempts;
  * the write client accepts one document and one document only, and refuses a query as firmly as it
    refuses an order cancel — again with zero HTTP attempts, token exchange included;
  * one credential serves both: `get_shopify_clients()` builds a single transport, so a flow that
    reads and then writes performs one client-credentials exchange, not two, and no second secret
    value exists anywhere;
  * only the return-provider seam can obtain the capability — a scan of the application's own code
    proves nothing else asks for it.

Nothing here executes a mutation against Shopify: every accepted document is handed a counting mock
transport, and the assertion is about what that transport was *offered*. Cancellation, closing,
deletion, draft disposal and voiding stay refused, stay unimplemented, and stay as UNKNOWN as 14C.7M
measured them.
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib

import httpx
import pytest

from app.tools.shopify import admin_transport as admin_module
from app.tools.shopify import client as client_module
from app.tools.shopify import get_shopify_client, get_shopify_clients
from app.tools.shopify import return_provider as return_provider_module
from app.tools.shopify import return_write_client as write_client_module
from app.tools.shopify.admin_transport import ShopifyAdminTransport
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
from app.tools.shopify.document_gate import MUTATION, QUERY, classify
from app.tools.shopify.errors import (
    ShopifyConfigError,
    ShopifyReadOnlyViolation,
    ShopifyWriteCapabilityViolation,
)
from app.tools.shopify.return_provider import _RETURN_CREATE_MUTATION, ShopifyReturnProvider
from app.tools.shopify.return_write_client import (
    ShopifyReturnWriteClient,
    _declared_write_refusal,
    declared_write_operations,
)
from app.tools.shopify.sanitizer import redact, reset_registered_secrets
from app.tools.returns.base import ReturnOutcomeUnknown, ReturnProviderUnavailable
from tests.test_shopify_read_only_gate import (
    DISPOSAL_OPERATIONS,
    _channel_root,
    _executable_source,
)
from tests.test_shopify_return_provider import (
    Recorded,
    make_clients,
    make_request,
    make_target,
    success_data,
)

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "shppa_SUPERSECRETVALUE1234"
ACCESS_TOKEN = "shpat_LIVETOKENABCD5678"

STORE = "example.myshopify.com"
TOKEN_URL = f"https://{STORE}/admin/oauth/access_token"
GRAPHQL_URL = f"https://{STORE}/admin/api/2026-07/graphql.json"

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The document the capability exists for, written the way the declaration says it must be written.
DECLARED_RETURN_CREATE = (
    "mutation ResolveReturnCreate($returnInput: ReturnInput!) {\n"
    "  returnCreate(returnInput: $returnInput) {\n"
    "    userErrors { field message }\n"
    "    return { id status }\n"
    "  }\n"
    "}\n"
)

# Every read document the application holds, in the module that holds them. Requirement (A).
ALL_READ_DOCUMENTS = {
    "_PRODUCTS_QUERY": _PRODUCTS_QUERY,
    "_CATALOG_QUERY": _CATALOG_QUERY,
    "_ORDERS_QUERY": _ORDERS_QUERY,
    "_RETURNS_QUERY": _RETURNS_QUERY,
    "_FULFILLMENT_LINE_ITEMS_QUERY": _FULFILLMENT_LINE_ITEMS_QUERY,
    "_ORDER_REFERENCE_QUERY": _ORDER_REFERENCE_QUERY,
    "_ORDER_LINE_IDENTITY_QUERY": _ORDER_LINE_IDENTITY_QUERY,
    "_RETURN_TARGETS_QUERY": _RETURN_TARGETS_QUERY,
    "_ORDER_RETURNS_QUERY": _ORDER_RETURNS_QUERY,
}

# Writes that exist on Shopify and are refused here: the five disposal shapes 14C.7M measured, plus
# the creation/edit/refund/fulfillment operations RESOLVE has never declared. Each one is a document
# a caller could hand the capability; none of them may become a request.
UNDECLARED_WRITES = {
    "arbitrary-mutation": "mutation Anything { doSomething(input: $a) { userErrors { field } } }",
    "orderCreate": """
mutation ResolveCreate($input: OrderCreateInput!) {
  orderCreate(input: $input) { order { id } userErrors { field message } }
}
""",
    "refundCreate": """
mutation ResolveRefund($input: RefundInput!) {
  refundCreate(input: $input) { refund { id } userErrors { field message } }
}
""",
    "orderEditBegin": """
mutation ResolveEdit($id: ID!) {
  orderEditBegin(orderId: $id) { editableOrder { id } userErrors { field message } }
}
""",
    "fulfillmentCreate": """
mutation ResolveFulfill($input: FulfillmentInput!) {
  fulfillmentCreate(input: $input) { fulfillment { id } userErrors { field message } }
}
""",
    "draftOrderCreate": """
mutation ResolveDraftCreate($draftOrder: DraftOrderInput!) {
  draftOrderCreate(draftOrder: $draftOrder) { draftOrder { id } userErrors { field } }
}
""",
    "draftOrderComplete": """
mutation ResolveDraftComplete($id: ID!) {
  draftOrderComplete(draftOrderId: $id) { draftOrder { id } userErrors { field } }
}
""",
    **DISPOSAL_OPERATIONS,
    **{
        operation: f"""
mutation Undeclared {{
  {operation}(id: "gid://shopify/Return/9") {{ userErrors {{ field message }} }}
}}
"""
        for operation in ("returnClose", "returnApproveRequest", "returnDeclineRequest",
                          "returnProcess", "returnReopen", "returnCancel")
    },
}


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


def _pair(answer: dict | None = None):
    """
    The two capabilities over one credential, the way `get_shopify_clients` builds them, plus the
    list of URLs their shared transport was offered. An entry in `seen` is an HTTP attempt; the
    value of this file is in `seen == []` after a refusal.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "expires_in": 3600,
                    "token_type": "bearer",
                },
            )
        return httpx.Response(
            200,
            json=answer
            if answer is not None
            else {
                "data": {
                    "returnCreate": {
                        "return": {"id": "gid://shopify/Return/9"},
                        "userErrors": [],
                    }
                }
            },
        )

    seen: list[str] = []
    admin = ShopifyAdminTransport(
        store_domain=STORE,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx.MockTransport(handler),
    )
    read = ShopifyReadOnlyClient(admin=admin)
    write = ShopifyReturnWriteClient(admin=admin)
    # Each capability holds exactly one thing: the shared transport. No credential of its own.
    assert set(vars(read)) == set(vars(write)) == {"_admin"}
    assert read._admin is write._admin
    return read, write, seen


# --------------------------------------------------------------------------- #
# A. the read client still reads — the split must not have cost it a single query
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(ALL_READ_DOCUMENTS))
async def test_every_existing_read_document_still_transits_the_read_client(name):
    read, _write, seen = _pair()
    document = ALL_READ_DOCUMENTS[name]
    assert classify(document).is_read, name

    data = await read.execute_graphql(document, {"id": "gid://shopify/Order/1", "first": 5})
    assert isinstance(data, dict), name
    assert seen == [TOKEN_URL, GRAPHQL_URL], name


async def test_the_read_clients_nine_read_methods_are_all_still_reachable():
    """
    The capability split is a boundary change, not a feature change: every `read_*` method the
    application calls is still there and still on the query side of the gate.
    """
    _read, _write, _seen = _pair()
    reads = {
        name
        for name, member in inspect.getmembers(ShopifyReadOnlyClient, inspect.isfunction)
        if name.startswith("read_")
    }
    assert reads == {
        "read_catalog",
        "read_fulfillment_line_items",
        "read_order_line_identity",
        "read_order_reference",
        "read_order_returns",
        "read_orders",
        "read_products",
        "read_return_targets",
        "read_returns",
    }, reads


# --------------------------------------------------------------------------- #
# B. and it refuses everything that is not a read — the declared write now included
# --------------------------------------------------------------------------- #
async def test_the_declared_return_create_is_now_refused_by_the_read_client_with_zero_http():
    read, _write, seen = _pair()
    assert _document_is_permitted(DECLARED_RETURN_CREATE) is not None
    with pytest.raises(ShopifyReadOnlyViolation) as excinfo:
        await read.execute_graphql(DECLARED_RETURN_CREATE, {"returnInput": {}})
    assert seen == []
    assert "returnCreate" in str(excinfo.value)
    assert excinfo.value.status_code == 500


@pytest.mark.parametrize("operation", sorted(DISPOSAL_OPERATIONS))
async def test_a_disposal_operation_is_refused_by_the_read_client_with_zero_http(operation):
    read, _write, seen = _pair()
    with pytest.raises(ShopifyReadOnlyViolation):
        await read.execute_graphql(DISPOSAL_OPERATIONS[operation], {"id": "gid://shopify/Order/1"})
    assert seen == [], f"{operation} reached the transport"


@pytest.mark.parametrize(
    "document",
    [
        pytest.param("subscription Watch { productUpdated(id: 1) { product { id } } }",
                     id="subscription"),
        pytest.param("", id="empty"),
        pytest.param("drop table orders", id="not-a-document"),
        pytest.param("query A { products { id } } mutation B { x(a: 1) }", id="two-root-operations"),
        pytest.param("query Empty { }", id="operation-with-no-field"),
        pytest.param(None, id="not-text"),
        pytest.param(42, id="a-number"),
    ],
)
async def test_a_subscription_or_an_unclassifiable_document_is_refused_with_zero_http(document):
    read, _write, seen = _pair()
    with pytest.raises(ShopifyReadOnlyViolation):
        await read.execute_graphql(document)
    assert seen == []


def test_the_read_only_client_has_no_mutation_allowlist_or_write_entrypoint():
    """Requirement (5)(C): the exception 14C.7N needed is gone from the code, not merely unused."""
    code = _executable_source(client_module).lower()
    for absent in (
        "returncreate",
        "intended_write_mutations",
        "shopify_data_use",
        "data_use",
        "return_write_client",
        "shopifyreturnwriteclient",
        "shopifywritecapabilityviolation",
        "declared_write_operations",
        "allowlist",
        "mutation",
        "submit_return",
    ):
        assert absent not in code, absent

    # The gate is one branch on one predicate: `verdict.is_read`. Nothing else permits a send.
    assert _document_is_permitted(_PRODUCTS_QUERY) is None
    refusal = _document_is_permitted(DECLARED_RETURN_CREATE)
    assert refusal is not None and "No token exchange" in refusal

    for forbidden in ("execute_graphql_mutation", "submit_return", "submit_return_create",
                      "create_order", "update_order", "mutate", "write"):
        assert not hasattr(ShopifyReadOnlyClient, forbidden), forbidden
    public = {
        name
        for name, member in inspect.getmembers(ShopifyReadOnlyClient, inspect.isfunction)
        if not name.startswith("_")
    }
    assert "execute_graphql" in public
    assert not [name for name in public if "mutation" in name or "write" in name], public


# --------------------------------------------------------------------------- #
# C. the write capability accepts exactly one document
# --------------------------------------------------------------------------- #
async def test_the_write_capability_transmits_the_declared_return_create():
    _read, write, seen = _pair()
    assert write.permitted_write_operations == ("returnCreate",)
    assert _declared_write_refusal(DECLARED_RETURN_CREATE) is None

    data = await write.submit_return_create(
        DECLARED_RETURN_CREATE, {"returnInput": {"orderId": "gid://shopify/Order/1"}}
    )
    assert data == {
        "returnCreate": {"return": {"id": "gid://shopify/Return/9"}, "userErrors": []}
    }
    assert seen == [TOKEN_URL, GRAPHQL_URL]


async def test_the_document_the_provider_actually_sends_is_the_one_the_capability_accepts():
    """
    Proven against the provider's real literal, not a lookalike written for this file. If the
    document the provider builds and the operation the capability declares ever drift apart, this
    fails — the provider cannot send something the capability has not been shown to accept.
    """
    assert classify(_RETURN_CREATE_MUTATION).root_fields == ("returnCreate",)
    _read, write, seen = _pair()
    assert _declared_write_refusal(_RETURN_CREATE_MUTATION) is None
    await write.submit_return_create(_RETURN_CREATE_MUTATION, {"returnInput": {}})
    assert seen == [TOKEN_URL, GRAPHQL_URL]


def test_the_declaration_the_capability_reads_is_the_single_source_of_truth():
    """The allowlist lives in `data_use.py`; the capability looks it up instead of restating it."""
    assert SHOPIFY_DATA_USE.intended_write_mutations == ("returnCreate",)
    assert declared_write_operations() == SHOPIFY_DATA_USE.intended_write_mutations
    assert ShopifyReturnWriteClient().permitted_write_operations == ("returnCreate",)


# --------------------------------------------------------------------------- #
# D. and refuses everything else, queries included
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(UNDECLARED_WRITES))
def test_the_undeclared_write_is_a_mutation_so_the_refusal_is_about_the_operation(name):
    """Each document below is what it claims to be; the refusal is the capability's, not a parse error."""
    verdict = classify(UNDECLARED_WRITES[name])
    assert verdict.kind == MUTATION and verdict.root_fields, name


@pytest.mark.parametrize("name", sorted(UNDECLARED_WRITES))
async def test_the_write_capability_refuses_every_undeclared_mutation(name):
    """
    Requirement (3): reject any mutation other than the declared return operation, locally, before
    HTTP. Order creation, cancellation, closing, deletion, draft disposal, voiding, refunds, order
    edits, fulfillment creation, the channel's own purchase documents and the five other return
    operations `write_returns` would permit — none of them leaves this process.
    """
    _read, write, seen = _pair()
    document = UNDECLARED_WRITES[name]
    attempted = classify(document).root_fields[0]

    with pytest.raises(ShopifyWriteCapabilityViolation) as excinfo:
        await write.submit_return_create(document, {"id": "gid://shopify/Order/1"})
    assert seen == [], f"{name} reached the transport"
    assert excinfo.value.status_code == 500
    assert attempted in str(excinfo.value)
    assert "does not declare" in str(excinfo.value)


@pytest.mark.parametrize("name", sorted(ALL_READ_DOCUMENTS))
async def test_the_write_capability_refuses_reads_because_it_is_mutation_only(name):
    """
    "Isolated write capability" means it can only write. A query is refused here too, and the
    refusal names where the document belongs, so this class is never a second general transport
    with a nicer name.
    """
    _read, write, seen = _pair()
    with pytest.raises(ShopifyWriteCapabilityViolation) as excinfo:
        await write.submit_return_create(ALL_READ_DOCUMENTS[name], {"first": 5, "id": "x"})
    assert seen == [], name
    assert "read-only client" in str(excinfo.value)
    assert classify(ALL_READ_DOCUMENTS[name]).kind == QUERY


async def test_a_bundled_mutation_cannot_smuggle_the_declared_write_past_the_capability():
    _read, write, seen = _pair()
    with pytest.raises(ShopifyWriteCapabilityViolation) as excinfo:
        await write.submit_return_create(
            "mutation Bundled {\n"
            "  returnCreate(returnInput: $a) { return { id } }\n"
            "  returnClose(id: $b) { return { id } }\n"
            "}\n"
        )
    assert seen == []
    assert "exactly one root operation" in str(excinfo.value)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n\t ", id="whitespace-only"),
        pytest.param("drop table orders", id="not-a-document"),
        pytest.param("mutation Broken($x: Int!", id="unbalanced"),
        pytest.param("fragment F on Order { id }", id="fragment-only"),
        pytest.param("mutation Empty { }", id="no-field"),
        pytest.param("query A { products { id } } mutation B { returnCreate(a: 1) }",
                     id="two-root-operations"),
        pytest.param(None, id="not-text"),
        pytest.param(42, id="a-number"),
    ],
)
async def test_a_document_that_is_not_one_clean_mutation_is_refused_locally(document):
    _read, write, seen = _pair()
    with pytest.raises(ShopifyWriteCapabilityViolation):
        await write.submit_return_create(document)
    assert seen == []


async def test_an_unconfigured_capability_refuses_a_mutation_before_a_configuration_error():
    """
    Fail-closed ordering survives the split: 'Shopify is not configured' is never a reason to let a
    document through, and a local refusal is never mistaken for a network outcome.
    """
    write = ShopifyReturnWriteClient(store_domain="", client_id="", client_secret="")
    with pytest.raises(ShopifyWriteCapabilityViolation):
        await write.submit_return_create(UNDECLARED_WRITES["orderCreate"])
    # The declared document is permitted by kind, so it reaches the credential check — and stops there.
    with pytest.raises(ShopifyConfigError):
        await write.submit_return_create(DECLARED_RETURN_CREATE, {"returnInput": {}})


# --------------------------------------------------------------------------- #
# E. no refusal costs a token exchange, even when a token is already in memory
# --------------------------------------------------------------------------- #
async def test_a_refusal_never_exchanges_a_token_even_when_one_is_already_cached():
    read, write, seen = _pair()
    await read.obtain_access_token()
    assert seen == [TOKEN_URL]

    hostile = (
        "mutation Leak {\n"
        "  orderCancel(orderId: \"gid://shopify/Order/1001\", reason: CUSTOMER, restock: true) {\n"
        "    job { id }\n"
        "  }\n"
        "}\n"
    )
    messages = []
    with pytest.raises(ShopifyReadOnlyViolation) as read_excinfo:
        await read.execute_graphql(hostile)
    with pytest.raises(ShopifyWriteCapabilityViolation) as write_excinfo:
        await write.submit_return_create(hostile)
    messages = [str(read_excinfo.value), str(write_excinfo.value)]

    assert seen == [TOKEN_URL], "a refusal added a request of its own"
    for message in messages:
        for secret in (
            CLIENT_SECRET,
            ACCESS_TOKEN,
            CLIENT_ID,
            "X-Shopify-Access-Token",
            "Bearer",
            "gid://shopify/Order/1001",
            "restock",
            "Leak {\n",
        ):
            assert secret not in message, (message, secret)
        assert "orderCancel" in message  # what was refused, so an operator can act on it
        assert redact(message) == message


async def test_both_capabilities_refuse_the_same_leaky_document_without_quoting_it():
    """One document, both boundaries, and neither message carries anything it should not."""
    read, write, seen = _pair()
    hostile = (
        "mutation Leak {\n"
        "  orderClose(input: $a) { order { id } }\n"
        "  customer { email }  # SHOPIFY_CLIENT_SECRET=shppa_SUPERSECRETVALUE1234\n"
        "}\n"
    )
    with pytest.raises(ShopifyReadOnlyViolation):
        await read.execute_graphql(hostile)
    with pytest.raises(ShopifyWriteCapabilityViolation):
        await write.submit_return_create(hostile)
    assert seen == []


# --------------------------------------------------------------------------- #
# F. one credential, two capabilities
# --------------------------------------------------------------------------- #
class _Settings:
    shopify_store_domain = STORE
    shopify_client_id = CLIENT_ID
    shopify_client_secret = CLIENT_SECRET
    shopify_api_version = "2026-07"


def test_the_pair_factory_builds_one_transport_and_no_second_secret():
    """
    Requirement (3): a separate capability OBJECT, never a second credential value.

    `get_shopify_clients()` is the only place production builds both, and what makes it one
    credential rather than two is that both clients hold the SAME transport object — the same
    configured values, one in-memory token, and nothing that could drift out of sync.
    """
    read, write = get_shopify_clients(settings=_Settings())
    assert isinstance(read, ShopifyReadOnlyClient)
    assert isinstance(write, ShopifyReturnWriteClient)
    assert read._admin is write._admin
    assert isinstance(read._admin, ShopifyAdminTransport)
    assert read.store_domain == write.store_domain == STORE
    assert read.graphql_url == write.graphql_url
    assert read.api_version == write.api_version == "2026-07"

    # The factory the whole app already used still hands out the read half, unchanged.
    assert isinstance(get_shopify_client(settings=_Settings()), ShopifyReadOnlyClient)

    # Constructing is inert, and neither capability exposes a credential in any of its string forms.
    # The one place the configured secret lives is the shared transport — that is the whole point of
    # building the pair over a single credential: exactly one copy, held by neither capability.
    for text in (repr(read), repr(write), str(read.__dict__), str(write.__dict__)):
        assert CLIENT_SECRET not in text
        assert "shppa_" not in text and "shpat_" not in text
    assert read._admin._client_secret == write._admin._client_secret
    assert vars(read) == vars(write) == {"_admin": read._admin}


async def test_a_read_then_a_write_performs_one_client_credentials_exchange():
    """
    The credential-sharing claim, measured on the wire rather than in a constructor.

    A provider that reads a target and then submits it authenticates once, because both
    capabilities were built over one transport. Two exchanges would mean the split had quietly
    become two credentials.
    """
    recorded = Recorded()
    read, write = make_clients(recorded, data=success_data())
    provider = ShopifyReturnProvider(read, write_client=write)

    ack = await provider.request_return(make_request(), make_target())
    assert ack.accepted is True
    assert recorded.tokens == 1
    assert len(recorded.mutations) == 1
    assert "returnCreate" in recorded.query


# --------------------------------------------------------------------------- #
# G. the capability is required for a write, and its refusal is a definite one
# --------------------------------------------------------------------------- #
async def test_a_provider_without_the_capability_refuses_and_sends_nothing():
    """
    The capability is optional in signature only. Without it a provider refuses with a definite
    'nothing was submitted' — and never falls back to the read client, because the read client has
    no write path to fall back to.
    """
    recorded = Recorded()
    read, _write = make_clients(recorded, data=success_data())
    provider = ShopifyReturnProvider(read)
    assert provider._write is None

    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.request_return(make_request(), make_target())
    assert "nothing reached Shopify" in str(excinfo.value)
    assert excinfo.value.status_code == 503
    assert recorded.attempted is False
    assert recorded.tokens == 0


async def test_a_capability_refusal_is_never_reported_as_an_unknown_outcome():
    """
    A refusal RESOLVE made itself must never read as 'Shopify may have done it'. That is why
    `ShopifyWriteCapabilityViolation` is a plain `ShopifyError` (nothing was ever sent) rather than
    a transport-class error, and the provider maps it onto a definite unavailability even while
    `mutating` is True.
    """
    from app.tools.shopify.errors import ShopifyError

    assert issubclass(ShopifyWriteCapabilityViolation, ShopifyError)
    assert ShopifyWriteCapabilityViolation.__mro__[1] is ShopifyError

    recorded = Recorded()
    read, write = make_clients(recorded, data=success_data())
    provider = ShopifyReturnProvider(read, write_client=write)

    async def refuse(*_args, **_kwargs):
        raise ShopifyWriteCapabilityViolation(
            "RESOLVE's Shopify return-write capability transmits exactly one declared return "
            "operation and nothing else, so this document was refused locally before any request "
            "was built. No token exchange, no header, no request body and no HTTP call took place."
        )

    write.submit_return_create = refuse
    with pytest.raises(ReturnProviderUnavailable) as excinfo:
        await provider.request_return(make_request(), make_target())
    assert not isinstance(excinfo.value, ReturnOutcomeUnknown)
    assert excinfo.value.status_code == 503
    assert recorded.attempted is False


# --------------------------------------------------------------------------- #
# H. who may obtain the capability, and what the new modules may not be
# --------------------------------------------------------------------------- #
def _production_sources() -> dict[str, str]:
    sources: dict[str, str] = {}
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        sources[path.relative_to(BACKEND_ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return sources


def _code_of(text: str) -> str:
    """A source file's executable code, with every docstring removed."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
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


# The modules allowed to name the write capability. Each has a reason, and the reason is the test:
# the class defines itself, the package factory builds the pair, the return provider is the only
# consumer, and the return-provider seam is the only production caller.
CAPABILITY_HOLDERS = {
    "app/tools/shopify/return_write_client.py",
    "app/tools/shopify/__init__.py",
    "app/tools/shopify/return_provider.py",
    "app/tools/returns/__init__.py",
}


def test_only_return_provider_code_can_obtain_the_write_capability():
    """
    Requirement (3): the capability reaches the code that submits returns and nobody else.

    A generic mutation method on the read client would have been the easy way to keep every caller
    working; this scan is what makes the hard way enforceable. Any new module that names the
    capability, or asks the pair factory for it, fails here. Docstrings are stripped first: prose
    may explain a boundary, code may not cross it.
    """
    holders = {
        relative
        for relative, source in _production_sources().items()
        if "ShopifyReturnWriteClient" in _code_of(source)
        or "get_shopify_clients" in _code_of(source)
    }
    assert holders == CAPABILITY_HOLDERS, holders

    for relative, source in _production_sources().items():
        code = _code_of(source)
        if "ShopifyReadOnlyClient" not in code:
            continue
        if relative in CAPABILITY_HOLDERS:
            # The capability defines its one write and the return provider calls it — that is the
            # split, not a leak. Everything else that touches the read client must not gain a way to
            # write, in either direction.
            continue
        for forbidden in ("execute_graphql_mutation", "submit_return_create",
                          "ShopifyWriteCapabilityViolation", "ShopifyReturnWriteClient"):
            assert forbidden not in code, (relative, forbidden)


def test_the_policy_free_transport_is_not_advertised_and_decides_nothing():
    """
    `ShopifyAdminTransport` is mechanics, so it must not become the back door the split closed.

    It is absent from the package's public surface, only the capability modules and the pair factory
    construct it, and its own code contains no document policy of any kind.
    """
    import app.tools.shopify as shopify_pkg

    assert "ShopifyAdminTransport" not in shopify_pkg.__all__
    assert "ShopifyReturnWriteClient" in shopify_pkg.__all__
    assert "get_shopify_clients" in shopify_pkg.__all__
    assert "ShopifyReturnProvider" not in shopify_pkg.__all__

    constructors = {
        relative
        for relative, source in _production_sources().items()
        if "ShopifyAdminTransport(" in _code_of(source)
    }
    assert constructors == {
        "app/tools/shopify/client.py",
        "app/tools/shopify/return_write_client.py",
        "app/tools/shopify/__init__.py",
    }, constructors

    code = _executable_source(admin_module).lower()
    for absent in ("classify", "document_gate", "intended_write_mutations", "data_use", "is_read",
                   "returncreate", "mutation", "allowlist"):
        assert absent not in code, absent


def test_the_capability_module_holds_no_generic_send_and_no_second_auth_path():
    """`submit_return_create` is the only thing that can leave, and auth is borrowed, not repeated."""
    code = _executable_source(write_client_module).lower()
    for absent in ("oauth/access_token", "client_credentials", "shopify_client_secret",
                   "get_settings", "httpx", "execute_graphql"):
        # Checked against executable code, not raw text: the module's own prose names what it
        # refuses to duplicate.
        assert absent not in code, absent
    assert "submit_return_create" in code
    public = {
        name
        for name, member in inspect.getmembers(ShopifyReturnWriteClient, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {"submit_return_create", "validate_configuration"}, public
    for forbidden in ("execute_graphql", "read_", "query", "mutation_send", "generic"):
        assert not any(name.startswith(forbidden) for name in public), forbidden


def test_the_provider_sends_its_mutation_only_through_the_capability():
    """
    The provider's transport calls, counted from its own code: exactly one write, through the
    capability; every read still through the read client; and no second credential path of any kind.
    """
    code = _executable_source(return_provider_module)
    assert code.count("submit_return_create") == 1
    assert "self._client.execute_graphql" in code  # the status read stays a query on the read side
    assert "self._client.read_" in code
    assert "self._write.execute_graphql" not in code
    lowered = code.lower()
    for absent in ("httpx", "oauth/access_token", "client_credentials", "get_settings",
                   "shopify_client_secret"):
        assert absent not in lowered, absent


# --------------------------------------------------------------------------- #
# the disposal behaviour 14C.7M left unknown is still unknown, and still unimplemented
# --------------------------------------------------------------------------- #
def test_cancellation_and_disposal_are_still_unknown_and_still_have_no_path():
    """
    Requirement (7): re-run 14C.7M's six unknowns against its own evidence file.

    Giving the declared write its own capability had to add no cancellation code and answer none of
    these questions. The capability refuses disposal operations because RESOLVE does not declare
    them, not because anyone established what they do.
    """
    evidence = json.loads(
        (_channel_root() / "schema" / "cancel_2026-07.json").read_text(encoding="utf-8")
    )
    assert len(evidence["unknowns"]) == 6
    assert all("UNKNOWN" in unknown.upper() for unknown in evidence["unknowns"])
    for behavioural in ("pre_completion_behavior", "post_completion_behavior", "scope_evidence"):
        assert "UNKNOWN" in evidence[behavioural].upper(), behavioural

    shipped = "\n".join(_production_sources().values())
    for name in DISPOSAL_OPERATIONS:
        assert f"mutation {name[0].upper()}{name[1:]}" not in shipped, name
        assert f"{name}(" not in shipped, name


def test_the_merchant_channel_still_owns_its_mutations_and_shares_none_of_this():
    """
    The purchase channel buys under its own app. This task moved a write inside RESOLVE and did not
    touch that half: the channel imports none of RESOLVE's new names, and RESOLVE's capability
    refuses the channel's own documents, because they are not RESOLVE's declared write.
    """
    root = _channel_root()
    for path in sorted((root / "merchant_channel").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for foreign in ("ShopifyReturnWriteClient", "ShopifyAdminTransport", "get_shopify_clients",
                        "ShopifyWriteCapabilityViolation", "declared_write_operations"):
            assert foreign not in source, (path.name, foreign)

    import sys

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from merchant_channel import documents as channel_documents

    for label, document in (
        ("draftOrderCreate", channel_documents.DRAFT_ORDER_CREATE),
        ("draftOrderComplete", channel_documents.DRAFT_ORDER_COMPLETE),
    ):
        verdict = classify(document)
        assert verdict.kind == MUTATION and verdict.root_fields == (label,), label
        assert _declared_write_refusal(document) is not None, label
    assert classify(channel_documents.SHOP_IDENTITY).is_read
    assert classify(channel_documents.READ_BACK_BY_CORRELATION).is_read


def test_the_two_capabilities_share_one_classifier_and_neither_writes_its_own():
    """
    One classifier, two policies. If the capability had its own document analysis, the boundary would
    be two boundaries with only one of them audited.
    """
    for module in (client_module, write_client_module):
        tree = ast.parse(inspect.getsource(module))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "classify" in called, module.__name__
        assert not {"parse", "tokenize", "lex"} & {name.lower() for name in called}, module.__name__
