"""
Task 14C.7N PART 10 — live, read-only preflight of the enforced Shopify boundary.

Two things have to be measured with the real store reachable, because neither is provable in a
hermetic suite:

  1. the document-kind gate did NOT lock RESOLVE out of its own reads — every read path still
     works against API 2026-07;
  2. a mutation handed to the read-only client is refused WITHOUT a request, while a request is
     genuinely available to be made.

(2) is why this script wraps a real transport instead of a mock one. The counting transport
forwards to Shopify for anything the client is allowed to send, so "the counter did not move"
means "no HTTP was attempted", not "the network was switched off first". Every document the
counter does see is checked to be a query; the script would report a failure if a mutation body
ever reached that layer.

Nothing here writes. It reads the catalog, one order's reference and one order's line identity and
return targets, asks the client to send a cancellation document, and prints safe fields only —
never a client id, secret, access token, authorization header, or raw request body. Exit 0 = the
boundary held and the reads worked; 2 = Shopify is not configured on this machine; any other
non-zero = a real problem, reported readably.
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
from app.tools.shopify import get_shopify_client  # noqa: E402
from app.tools.shopify.client import ShopifyReadOnlyClient  # noqa: E402
from app.tools.shopify.data_use import SHOPIFY_DATA_USE  # noqa: E402
from app.tools.shopify.errors import (  # noqa: E402
    ShopifyConfigError,
    ShopifyError,
    ShopifyReadOnlyViolation,
)
from app.tools.shopify.sanitizer import redact  # noqa: E402

TARGET_ORDER_NAME = "#1001"

# Written exactly as 14C.7M's introspection said 2026-07 declares it: flat arguments, `reason` and
# `restock` both required, and a payload that carries a `Job` and no `order`. It is offered to the
# client here to be refused — never to be sent.
CANCEL_PROBE = """
mutation Preflight14c7nCancelProbe($orderId: ID!) {
  orderCancel(orderId: $orderId, reason: CUSTOMER, restock: false) {
    job { id }
    orderCancelUserErrors { field message }
  }
}
"""

CREATE_PROBE = """
mutation Preflight14c7nCreateProbe($input: DraftOrderInput!) {
  draftOrderCreate(input: $input) { draftOrder { id } userErrors { field message } }
}
"""

SUBSCRIPTION_PROBE = """
subscription Preflight14c7nWatchProbe { reviewEvent { id } }
"""

MALFORMED_PROBES = {
    "empty": "",
    "not-a-document": "describe shopify orders",
    "unbalanced": "query Broken($x: Int!",
    "two-root-operations": "query A { shop { name } } mutation B { orderDelete(orderId: 1) }",
}


def _line(label: str, ok: bool | None, detail: str = "") -> bool:
    mark = {True: "SUCCESS", False: "FAILED", None: "SKIPPED"}[ok]
    print(f"{label}: {mark}" + (f" — {redact(detail)}" if detail else ""))
    return bool(ok)


class CountingTransport(httpx.AsyncBaseTransport):
    """
    A real transport with a memory.

    It forwards everything it is handed, and records the URL and the document of each call, so the
    script can distinguish "the client refused locally" from "the client asked and Shopify answered".
    """

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.calls: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        record = {"method": request.method, "url": str(request.url), "document": ""}
        if request.content:
            try:
                payload = json.loads(request.content.decode("utf-8"))
                record["document"] = str(payload.get("query") or "")
            except (ValueError, UnicodeDecodeError):
                record["document"] = "<unreadable body>"
        self.calls.append(record)
        return await self._inner.handle_async_request(request)

    @property
    def graphql_calls(self) -> list[dict]:
        return [call for call in self.calls if "/admin/api/" in call["url"]]

    @property
    def mutation_calls(self) -> list[dict]:
        return [call for call in self.graphql_calls if call["document"].lstrip().startswith("mutation")]

    def reset(self) -> None:
        self.calls.clear()


async def _refused(client: ShopifyReadOnlyClient, calls: CountingTransport, document: str, label: str) -> bool:
    """
    Offer one document to the real client and require a refusal that made no request.

    The counter is the evidence. `HTTP attempts added: 0` with the network available says the gate
    acted locally; anything else — a Shopify error, an accepted document, a call added — is reported
    as the failure it is.
    """
    before = len(calls.graphql_calls)
    try:
        await client.execute_graphql(document, {"orderId": "gid://shopify/Order/0"})
    except ShopifyReadOnlyViolation as exc:
        added = len(calls.graphql_calls) - before
        message = redact(str(exc))
        # The refusal must name what it refused without quoting it.
        leaked = any(fragment in message for fragment in ("$orderId", "restock:", "Preflight14c7n"))
        _line(
            f"local refusal: {label}",
            added == 0 and not leaked,
            f"HTTP attempts added: {added}" + (" — refusal quoted the document" if leaked else ""),
        )
        return added == 0 and not leaked
    except ShopifyError as exc:
        _line(f"local refusal: {label}", False, f"the document was sent: {exc.message}")
        return False
    _line(f"local refusal: {label}", False, "the client accepted a document it should have refused")
    return False


async def run_checks() -> int:
    settings = get_settings()
    try:
        get_shopify_client(settings).validate_configuration()
    except ShopifyConfigError as exc:
        _line("Shopify configuration", False, str(exc))
        print(
            "\nNo Shopify credentials on this machine. Populate SHOPIFY_CLIENT_ID / "
            "SHOPIFY_CLIENT_SECRET in backend/.env — never paste their values into chat, code or "
            "logs. The boundary checks below still need to be run against a configured store."
        )
        return 2

    calls = CountingTransport()
    # Same store, same credential, same client the app uses — only the transport is observed.
    client = ShopifyReadOnlyClient(
        store_domain=settings.shopify_store_domain,
        client_id=settings.shopify_client_id,
        client_secret=settings.shopify_client_secret,
        api_version=settings.shopify_api_version or "2026-07",
        transport=calls,
    )
    print(f"Store domain: {client.store_domain}")
    print(f"Admin API version: {client.api_version}")
    print(f"Declared write operations: {', '.join(SHOPIFY_DATA_USE.intended_write_mutations)}")

    ok = True

    # ---- 1. the reads still work, through the gate ------------------------------------
    try:
        catalog = await client.read_catalog(search_query="shampoo", first=10)
        _line("live read: catalog", True, f"{len(catalog.products)} product(s)")
        print(f"shop name: {catalog.shop_name}")
        print(f"store currency: {catalog.currency}")
        for product in catalog.products[:3]:
            print(f"product: {product.title} · {product.product_id}")
    except ShopifyError as exc:
        _line("live read: catalog", False, exc.message)
        catalog = None
        ok = False

    order_gid = None
    try:
        orders = await client.read_orders(first=25)
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
        for label, read in (
            ("order reference", client.read_order_reference),
            ("line identity", client.read_order_line_identity),
            ("return targets", client.read_return_targets),
        ):
            try:
                result = await read(order_gid)
                _line(f"live read: {label}", True, type(result).__name__)
            except ShopifyError as exc:
                _line(f"live read: {label}", False, exc.message)
                ok = False

    # ---- 2. every document that reached Shopify was a query --------------------------
    mutations_sent = calls.mutation_calls
    _line(
        "0 mutations sent to Shopify",
        not mutations_sent,
        f"{len(mutations_sent)} mutation body(ies) reached the transport" if mutations_sent else "",
    )
    documents = [call["document"] for call in calls.graphql_calls]
    all_queries = all(doc.lstrip().startswith("query ") for doc in documents)
    _line("every transmitted document is a query", all_queries)
    print(f"graphql calls transmitted: {len(documents)}")
    ok = ok and not mutations_sent and all_queries

    # ---- 3. refusals, measured with the network available ----------------------------
    for label, document in (
        ("orderCancel (the disposal operation 14C.7M measured)", CANCEL_PROBE),
        ("the merchant channel's own purchase mutation", CREATE_PROBE),
        ("subscription", SUBSCRIPTION_PROBE),
    ):
        ok = (await _refused(client, calls, document, label)) and ok

    for label, document in MALFORMED_PROBES.items():
        ok = (await _refused(client, calls, document, f"malformed document: {label}")) and ok

    # Whether a cancelled order stays readable is one of the six unknowns 14C.7M recorded, and
    # nothing here cancels an order to guess at it. The read below only re-states what Shopify
    # already says about the order that exists.
    if order_gid:
        try:
            targets = await client.read_return_targets(order_gid)
            print(f"order returnStatus as Shopify reports it: {targets.return_status}")
            print(f"shipped fulfillment lines readable: {len(targets.shipped_lines)}")
        except ShopifyError as exc:
            _line("live read: return targets (repeat)", False, exc.message)

    total = len(calls.calls)
    print(f"total HTTP attempts this preflight made: {total}")
    print("credential fields included in any printed line: none")
    _line("14C.7N read-only boundary preflight", ok)
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
