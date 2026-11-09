"""
Task 14C.7O PART 11 — live, read-only preflight of the capability split.

14C.7O moved RESOLVE's one declared write off `ShopifyReadOnlyClient` and onto
`ShopifyReturnWriteClient`, so the read client could become genuinely zero-mutation. Two things can
only be measured with the real store reachable:

  1. the reads still work. A boundary that locked RESOLVE out of its own catalog, orders, return
     targets and app-installation scopes is not a boundary, it is an outage; and
  2. a refusal is local. Both capabilities are wrapped in a transport that FORWARDS to Shopify, so
     "the counter did not move" means "no HTTP was attempted while the network was available" —
     which is the only version of that claim worth making.

(3) is the part this task must not do: the accepted `returnCreate` document is never sent. The
capability's acceptance of it is proven against a gate call and a MOCK transport, and the live
counting transport is checked to have recorded zero mutation bodies. `orderCancel`, `orderCreate`,
`draftOrderCreate`, `refundCreate`, subscriptions and malformed documents are offered only to be
refused, and cancellation/disposal behaviour stays exactly as 14C.7M measured it: UNKNOWN.

Printed fields are safe ones: store domain, API version, order name and id, counts, scope tokens.
Never a client id, secret, access token, authorization header, or raw request body.
Exit 0 = both capabilities bounded correctly and the reads worked; 2 = Shopify is not configured;
any other non-zero = a real problem, reported readably.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.tools.shopify import get_shopify_clients  # noqa: E402
from app.tools.shopify.admin_transport import ShopifyAdminTransport  # noqa: E402
from app.tools.shopify.client import ShopifyReadOnlyClient  # noqa: E402
from app.tools.shopify.data_use import SHOPIFY_DATA_USE  # noqa: E402
from app.tools.shopify.document_gate import classify  # noqa: E402
from app.tools.shopify.errors import (  # noqa: E402
    ShopifyConfigError,
    ShopifyError,
    ShopifyReadOnlyViolation,
    ShopifyWriteCapabilityViolation,
)
from app.tools.shopify.return_provider import _RETURN_CREATE_MUTATION  # noqa: E402
from app.tools.shopify.return_write_client import (  # noqa: E402
    ShopifyReturnWriteClient,
    _declared_write_refusal,
)
from app.tools.shopify.sanitizer import redact  # noqa: E402

TARGET_ORDER_NAME = "#1001"

# The five disposal operations 14C.7M measured, the merchant channel's own purchase write, an order
# creation, a refund, and the declared return write. Every one of them is offered to the READ client,
# which must refuse all of them now — the declared one included.
READ_REFUSALS = {
    "orderCancel (disposal, behaviour still UNKNOWN)": """
mutation Preflight14c7oCancelProbe($orderId: ID!) {
  orderCancel(orderId: $orderId, reason: CUSTOMER, restock: false) {
    job { id }
    orderCancelUserErrors { field message }
  }
}
""",
    "orderClose (disposal)": """
mutation Preflight14c7oCloseProbe($input: OrderCloseInput!) {
  orderClose(input: $input) { order { id } userErrors { field message } }
}
""",
    "orderDelete (disposal)": """
mutation Preflight14c7oDeleteProbe($orderId: ID!) {
  orderDelete(orderId: $orderId) { deletedId userErrors { field message } }
}
""",
    "draftOrderDelete (disposal)": """
mutation Preflight14c7oDraftDeleteProbe($input: DraftOrderDeleteInput!) {
  draftOrderDelete(input: $input) { deletedId userErrors { field message } }
}
""",
    "transactionVoid (disposal)": """
mutation Preflight14c7oVoidProbe($parentTransactionId: ID!) {
  transactionVoid(parentTransactionId: $parentTransactionId) {
    transaction { id }
    userErrors { field message }
  }
}
""",
    "draftOrderCreate (the merchant channel's own write)": """
mutation Preflight14c7oDraftCreateProbe($input: DraftOrderInput!) {
  draftOrderCreate(draftOrder: $input) { draftOrder { id } userErrors { field message } }
}
""",
    "orderCreate": """
mutation Preflight14c7oCreateProbe($input: OrderCreateInput!) {
  orderCreate(input: $input) { order { id } userErrors { field message } }
}
""",
    "refundCreate": """
mutation Preflight14c7oRefundProbe($input: RefundInput!) {
  refundCreate(input: $input) { refund { id } userErrors { field message } }
}
""",
    "returnCreate (RESOLVE's declared write — now refused here, not transmitted)": """
mutation Preflight14c7oReturnProbe($returnInput: ReturnInput!) {
  returnCreate(returnInput: $returnInput) { return { id status } userErrors { field message } }
}
""",
    "subscription": "subscription Preflight14c7oWatchProbe { reviewEvent { id } }",
    "malformed: not-a-document": "describe shopify orders",
    "malformed: unbalanced": "query Broken($x: Int!",
    "malformed: two-root-operations": "query A { shop { name } } mutation B { orderDelete(orderId: 1) }",
}

# What the write capability must refuse. A query is on this list because the capability is
# mutation-only, and five return operations are on it because `write_returns` permits more than
# RESOLVE declares.
WRITE_REFUSALS = {
    "orderCancel": READ_REFUSALS["orderCancel (disposal, behaviour still UNKNOWN)"],
    "orderClose": READ_REFUSALS["orderClose (disposal)"],
    "orderDelete": READ_REFUSALS["orderDelete (disposal)"],
    "draftOrderDelete": READ_REFUSALS["draftOrderDelete (disposal)"],
    "transactionVoid": READ_REFUSALS["transactionVoid (disposal)"],
    "orderCreate": READ_REFUSALS["orderCreate"],
    "draftOrderCreate": READ_REFUSALS["draftOrderCreate (the merchant channel's own write)"],
    "refundCreate": READ_REFUSALS["refundCreate"],
    "returnClose (permitted by write_returns, not declared)": """
mutation Preflight14c7oReturnCloseProbe($id: ID!) {
  returnClose(returnId: $id) { return { id status } userErrors { field message } }
}
""",
    "returnApproveRequest (permitted by write_returns, not declared)": """
mutation Preflight14c7oApproveProbe($returnLineItems: [ReturnLineItemInput!]!) {
  returnApproveRequest(returnLineItems: $returnLineItems) { return { id } userErrors { field } }
}
""",
    "a read query (this capability cannot read)": "query Preflight14c7oReadProbe { shop { name } }",
    "bundled mutation (two root operations)": """
mutation Preflight14c7oBundledProbe($a: ReturnInput!, $b: ID!) {
  returnCreate(returnInput: $a) { return { id } }
  returnClose(returnId: $b) { return { id } }
}
""",
    "malformed: no-field mutation": "mutation Preflight14c7oEmptyProbe { }",
}

APP_SCOPE_QUERY = """
query Preflight14c7oScopeProbe {
  currentAppInstallation { accessScopes { handle } }
}
"""


def _line(label: str, ok: bool | None, detail: str = "") -> bool:
    mark = {True: "SUCCESS", False: "FAILED", None: "SKIPPED"}[ok]
    print(f"{label}: {mark}" + (f" — {redact(detail)}" if detail else ""))
    return bool(ok)


class CountingTransport(httpx.AsyncBaseTransport):
    """
    A real transport with a memory: it forwards everything it is handed and records the URL and the
    document of each call, so the script can tell "refused locally" from "sent and Shopify answered".
    """

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.calls: list[dict] = []
        # `calls` is a measuring window a check may reset; `lifetime` is what the whole preflight
        # actually put on the wire, and nothing resets it.
        self.lifetime: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        record = {"method": request.method, "url": str(request.url), "document": ""}
        if request.content:
            try:
                payload = json.loads(request.content.decode("utf-8"))
                record["document"] = str(payload.get("query") or "")
            except (ValueError, UnicodeDecodeError):
                record["document"] = "<unreadable body>"
        self.calls.append(record)
        self.lifetime.append(record)
        return await self._inner.handle_async_request(request)

    @property
    def graphql_calls(self) -> list[dict]:
        return [call for call in self.calls if "/admin/api/" in call["url"]]

    @property
    def mutation_calls(self) -> list[dict]:
        return [c for c in self.graphql_calls if c["document"].lstrip().startswith("mutation")]

    @property
    def lifetime_mutations(self) -> list[dict]:
        return [
            c
            for c in self.lifetime
            if "/admin/api/" in c["url"] and c["document"].lstrip().startswith("mutation")
        ]

    def reset(self) -> None:
        self.calls.clear()


async def _refused(send, calls: CountingTransport, document: str, label: str, error) -> bool:
    """
    Offer one document to one capability and require a local refusal that made no request.

    `send` is the capability's own entry point, so the gate that runs is the one production uses.
    """
    before = len(calls.calls)
    try:
        await send(document)
    except error as exc:
        added = len(calls.calls) - before
        message = redact(str(exc))
        quoted = any(fragment in message for fragment in ("Preflight14c7o", "$orderId", "restock:"))
        _line(f"local refusal: {label}", added == 0 and not quoted,
              f"HTTP attempts added: {added}" + (" — refusal quoted the document" if quoted else ""))
        return added == 0 and not quoted
    except ShopifyError as exc:
        _line(f"local refusal: {label}", False, f"the document was transmitted: {exc.message}")
        return False
    _line(f"local refusal: {label}", False, "the capability accepted a document it must refuse")
    return False


async def _live_state(read: ShopifyReadOnlyClient, label: str) -> tuple[int, int, str]:
    """One read-only snapshot: orders on the store, returns on the target order, its returnStatus."""
    orders = await read.read_orders(first=25)
    target = next((o for o in orders if o.name.strip() == TARGET_ORDER_NAME), None)
    returns = 0
    return_status = "unread"
    if target is not None:
        targets = await read.read_return_targets(target.order_id)
        return_status = str(targets.return_status)
        existing = await read.read_order_returns(target.order_id)
        returns = len(existing)
    print(
        f"{label}: orders={len(orders)} "
        f"{TARGET_ORDER_NAME}={'present' if target else 'absent'} "
        f"shopify_returns_on_{TARGET_ORDER_NAME.lstrip('#')}={returns} "
        f"returnStatus={return_status}"
    )
    return len(orders), returns, return_status


async def run_checks() -> int:
    settings = get_settings()
    calls = CountingTransport()
    # One configured credential, one transport, two capabilities — the production shape, observed.
    admin = ShopifyAdminTransport(
        store_domain=settings.shopify_store_domain,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
        api_version=settings.shopify_api_version or "2026-07",
        transport=calls,
    )
    read = ShopifyReadOnlyClient(admin=admin)
    write = ShopifyReturnWriteClient(admin=admin)
    try:
        read.validate_configuration()
    except ShopifyConfigError as exc:
        _line("Shopify configuration", False, str(exc))
        print(
            "\nNo Shopify credentials on this machine. Populate SHOPIFY_CLIENT_ID / "
            "SHOPIFY_CLIENT_SECRET in backend/.env — never paste their values into chat, code or "
            "logs. The capability checks below still need to be run against a configured store."
        )
        return 2

    print(f"Store domain: {read.store_domain}")
    print(f"Admin API version: {read.api_version}")
    print(f"Declared write operations: {', '.join(SHOPIFY_DATA_USE.intended_write_mutations)}")
    print(f"Read capability: {type(read).__name__} · write capability: {type(write).__name__}")
    print(f"One shared credential transport: {read._admin is write._admin}")

    ok = True

    # ---- 1. the factories hand out one credential, two capabilities -------------------
    pair_read, pair_write = get_shopify_clients(settings)
    same = pair_read._admin is pair_write._admin
    ok = _line("get_shopify_clients builds one transport for both capabilities", same) and ok
    ok = _line(
        "get_shopify_client still returns the read capability",
        isinstance(pair_read, ShopifyReadOnlyClient) and not isinstance(pair_read,
                                                                       ShopifyReturnWriteClient),
    ) and ok

    # ---- 2. Shopify state BEFORE, read-only ------------------------------------------
    try:
        before = await _live_state(read, "state before")
    except ShopifyError as exc:
        _line("state before", False, exc.message)
        before = (-1, -1, "unread")
        ok = False

    # ---- 3. the reads still work, through the absolute gate --------------------------
    try:
        catalog = await read.read_catalog(search_query="shampoo", first=10)
        _line("live read: catalog", True, f"{len(catalog.products)} product(s)")
        print(f"shop name: {catalog.shop_name}")
        print(f"store currency: {catalog.currency}")
    except ShopifyError as exc:
        _line("live read: catalog", False, exc.message)
        ok = False

    order_gid = None
    try:
        orders = await read.read_orders(first=25)
        found = next((o for o in orders if o.name.strip() == TARGET_ORDER_NAME), None)
        if found is None:
            _line(f"live read: order {TARGET_ORDER_NAME}", False, "not in the orders read")
            ok = False
        else:
            order_gid = found.order_id
            _line(f"live read: order {TARGET_ORDER_NAME}", True)
            print(f"order id: {order_gid}")
            print(f"financial status: {found.financial_status}")
            print(f"fulfillment status: {found.fulfillment_status}")
    except ShopifyError as exc:
        _line("live read: orders", False, exc.message)
        ok = False

    if order_gid:
        for label, method in (
            ("order reference", read.read_order_reference),
            ("line identity", read.read_order_line_identity),
            ("return targets", read.read_return_targets),
            ("order returns", read.read_order_returns),
        ):
            try:
                result = await method(order_gid)
                _line(f"live read: {label}", True, type(result).__name__)
            except ShopifyError as exc:
                _line(f"live read: {label}", False, exc.message)
                ok = False

    # The scopes the app actually holds, read live: the split must not have changed the grant.
    try:
        data = await read.execute_graphql(APP_SCOPE_QUERY)
        installation = data.get("currentAppInstallation") or {}
        scopes = sorted(
            str(item.get("handle"))
            for item in (installation.get("accessScopes") or [])
            if isinstance(item, dict)
        )
        declared = sorted(SHOPIFY_DATA_USE.requested_scopes)
        ok = _line("live read: currentAppInstallation.accessScopes", True, ", ".join(scopes)) and ok
        ok = _line("held scopes equal the declared scopes", scopes == declared,
                   f"held={scopes} declared={declared}" if scopes != declared else "") and ok
        ok = _line(
            "write_orders is absent",
            "write_orders" not in scopes,
            "a write scope on orders appeared" if "write_orders" in scopes else "",
        ) and ok
    except ShopifyError as exc:
        _line("live read: currentAppInstallation.accessScopes", False, exc.message)
        ok = False

    # ---- 4. every document that reached Shopify so far was a query -------------------
    transmitted = [call["document"] for call in calls.graphql_calls]
    ok = _line("0 mutations transmitted", not calls.mutation_calls,
               f"{len(calls.mutation_calls)} mutation body(ies) reached the transport") and ok
    ok = _line("every transmitted document is a query",
               all(doc.lstrip().startswith("query ") for doc in transmitted)) and ok
    print(f"graphql calls transmitted so far: {len(transmitted)}")
    # ---- 5. the read client refuses every write, declared or not --------------------
    for label, document in READ_REFUSALS.items():
        ok = (
            await _refused(
                lambda doc: read.execute_graphql(doc, {"orderId": "gid://shopify/Order/0"}),
                calls,
                document,
                f"read capability / {label}",
                ShopifyReadOnlyViolation,
            )
        ) and ok

    # ---- 6. the write capability refuses everything except its one document ---------
    for label, document in WRITE_REFUSALS.items():
        ok = (
            await _refused(
                lambda doc: write.submit_return_create(doc, {"id": "gid://shopify/Order/0"}),
                calls,
                document,
                f"return-write capability / {label}",
                ShopifyWriteCapabilityViolation,
            )
        ) and ok

    # ---- 7. the accepted document: gate + MOCK transport, never the live one --------
    calls.reset()
    verdict = classify(_RETURN_CREATE_MUTATION)
    accepted_by_gate = verdict.kind == "mutation" and verdict.root_fields == ("returnCreate",)
    ok = _line(
        "the provider's document is the declared operation",
        accepted_by_gate and _declared_write_refusal(_RETURN_CREATE_MUTATION) is None,
        f"root fields: {', '.join(verdict.root_fields)}",
    ) and ok

    mock_seen: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        mock_seen.append(str(request.url))
        if str(request.url).endswith("/admin/oauth/access_token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "mock-value-never-a-credential",
                    "expires_in": 3600,
                    "token_type": "bearer",
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "returnCreate": {
                        "return": {"id": "gid://shopify/Return/0000000000000", "status": "REQUESTED"},
                        "userErrors": [],
                    }
                }
            },
        )

    mock_admin = ShopifyAdminTransport(
        store_domain="example.myshopify.com",
        client_id="mock-client-id",
        client_secret="mock-client-secret",
        transport=httpx.MockTransport(mock_handler),
    )
    mock_write = ShopifyReturnWriteClient(admin=mock_admin)
    data = await mock_write.submit_return_create(
        _RETURN_CREATE_MUTATION, {"returnInput": {"orderId": "gid://shopify/Order/0"}}
    )
    went_to_the_mock_store = all("example.myshopify.com" in url for url in mock_seen)
    ok = _line(
        "the accepted document transits a MOCK transport, never the live store",
        bool(data.get("returnCreate")) and went_to_the_mock_store,
        f"{len(mock_seen)} mock call(s), none to {read.store_domain}",
    ) and ok
    live_mutations = calls.mutation_calls
    ok = _line(
        "0 HTTP attempts against the real store for ANY mutation document",
        not live_mutations and not calls.calls,
        f"live attempts after the mock proof: {len(calls.calls)}",
    ) and ok
    ok = _line(
        "0 mutation bodies reached the live transport across the whole preflight",
        not calls.lifetime_mutations,
        f"{len(calls.lifetime_mutations)} mutation body(ies)",
    ) and ok

    # ---- 8. Shopify state AFTER, read-only ------------------------------------------
    try:
        after = await _live_state(read, "state after")
        ok = _line(
            f"{TARGET_ORDER_NAME} unchanged, and no Shopify return created",
            before == after and after[1] == before[1],
            f"before={before} after={after}" if before != after else "",
        ) and ok
    except ShopifyError as exc:
        _line("state after", False, exc.message)
        ok = False

    print(f"total HTTP attempts this preflight made: {len(calls.lifetime)}")
    print(f"mutation bodies offered to the live transport: {len(calls.lifetime_mutations)}")
    print(f"token exchanges: {sum(1 for c in calls.lifetime if c['url'].endswith('/oauth/access_token'))}")
    print(f"graphql reads transmitted: {sum(1 for c in calls.lifetime if '/admin/api/' in c['url'])}")
    print("credential fields included in any printed line: none")
    _line("14C.7O capability-split preflight", ok)
    return 0 if ok else 1


def main() -> None:
    try:
        code = asyncio.run(run_checks())
    except KeyboardInterrupt:
        print("Interrupted.")
        code = 130
    except Exception as exc:  # never leak an unredacted traceback
        print(f"UNEXPECTED ERROR: {redact(str(exc))[:400]}")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
