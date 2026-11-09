"""
Machine-readable statement of RESOLVE's Shopify data use (Task 14B.5A; scopes updated by
Task 14C.6B after the live grant of `write_returns`).

This is the single source of truth the human-facing `docs/shopify-protected-data.md` mirrors.
It exists so the data-use claims — the scopes requested, the fields read, the one write mutation
intended, and that NO customer PII fields are requested — are declared in code and can be asserted
by tests, rather than only asserted in prose that nothing checks. It grants nothing and
authorifies nothing: the scopes live on the Shopify app installation, and this declares which ones
RESOLVE asks for and what it means to do with them.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ShopifyDataUse:
    # The scopes the installed app actually holds, re-read live from
    # `currentAppInstallation.accessScopes` (Task 14C.6A) and declared here so tests can hold the
    # code to it. Four reads plus exactly one write.
    #
    # `read_merchant_managed_fulfillment_orders` is the minimum read that exposes
    # Order.fulfillmentOrders -> lineItems (Task 14C.2); it is needed only to pair a fulfillment
    # order line with its order line and see its outstanding units, and grants no write
    # capability. It is NOT what a return references: `returnCreate` takes a FulfillmentLineItem
    # id, read under `read_orders` via Order.fulfillments.
    #
    # `write_returns` (Task 14C.6) is requested for ONE purpose: the controlled return workflow in
    # `app/tools/returns/`, whose only intended mutation is `returnCreate`. It is broader than
    # that on Shopify's side — the same scope also permits returnClose, returnReopen and editing
    # return staging — so the scope grant is not the limit: RESOLVE's own code must keep exposing
    # only the controlled path. No `write_orders`, no `write_products`, no
    # `write_merchant_managed_fulfillment_orders` is requested, and none is held.
    requested_scopes: tuple[str, ...] = (
        "read_products",
        "read_orders",
        "read_returns",
        "read_merchant_managed_fulfillment_orders",
        "write_returns",
    )
    # The ONLY merchant-side mutation this integration is built to send, and the only one
    # `write_returns` is requested for. Anything outside it — order edits, fulfillment creation,
    # return closing/reopening, refunds — is not implemented in RESOLVE by design.
    #
    # Task 14C.7N made this more than a declaration of intent, and 14C.7O made it precise about
    # where it applies. It is now the allowlist of exactly one object: `ShopifyReturnWriteClient`,
    # RESOLVE's isolated return-write capability, which transmits a document only if it is a
    # well-formed mutation with one root field named in this tuple, and refuses anything else by
    # raising `ShopifyWriteCapabilityViolation` locally — before the token exchange, before headers,
    # before the request body. `ShopifyReadOnlyClient`, which every read in the application goes
    # through, no longer consults this tuple at all: it has no write exception, so no document that
    # is not a read query can leave through it. A write that `write_returns` would permit but this
    # tuple does not name (`returnClose`, `returnApproveRequest`, `returnProcess`) is refused at the
    # capability gate, and so is anything RESOLVE holds no scope for (`orderCancel`,
    # `draftOrderCreate`, `refundCreate`).
    intended_write_mutations: tuple[str, ...] = ("returnCreate",)
    # The boundary the client enforces, declared here so tests can hold the code to it rather than
    # to prose. `enforces_read_only_transport` is the property 14C.7N made checkable and 14C.7O made
    # absolute: a document whose kind cannot be positively identified as exactly one read operation
    # never reaches the transport, there is no write allowance inside that rule, and every read
    # method of the client passes a query literal the gate accepts.
    enforces_read_only_transport: bool = True
    # Write scopes RESOLVE explicitly does NOT request. Declared so a test can prove the grant
    # never widened past the single controlled-return write capability.
    unrequested_write_scopes: tuple[str, ...] = (
        "write_orders",
        "write_products",
        "write_merchant_managed_fulfillment_orders",
        "write_fulfillments",
        "write_publications",
    )
    # Protected customer PII fields RESOLVE does NOT request. Tests assert none of these appear
    # in any Shopify GraphQL query string.
    not_requested_pii: tuple[str, ...] = (
        "customer { firstName }",
        "customer { lastName }",
        "customer { email }",
        "phone",
        "address",
    )
    purpose: str = (
        "Verify canonical order/fulfillment state and support an authorized post-purchase "
        "resolution, including one controlled return submission (`returnCreate`) for a return the "
        "user's own issue and the Authority Engine allowed. No advertising, analytics, marketing "
        "profiling, sale, or sharing."
    )
    # Whether Shopify-derived protected data is persisted anywhere today. It is not.
    persists_shopify_pii: bool = False
    # What the return-submission claim on a RESOLVE return row may hold, and nothing more: the
    # merchant order id, the fulfillment line id, an integer unit count, a reason token from a
    # fixed table, and an opaque attempt identifier with its timestamp. No token, no secret, no
    # request header, and no customer name, email, phone or address — the merchant-side data is
    # only ever read from Shopify, so there is no channel that could carry those into this row.
    persisted_shopify_reference_fields: tuple[str, ...] = (
        "provider_order_reference",
        "provider_line_reference",
        "provider_quantity",
        "provider_reason",
        "submission_claim",
        "submission_claimed_at",
    )

    @property
    def has_write_scope(self) -> bool:
        return any(not s.startswith("read_") for s in self.requested_scopes)


SHOPIFY_DATA_USE = ShopifyDataUse()


__all__ = ["ShopifyDataUse", "SHOPIFY_DATA_USE"]
