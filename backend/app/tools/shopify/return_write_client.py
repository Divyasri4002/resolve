"""
RESOLVE's one declared write, isolated in its own capability object (Task 14C.7O).

`ShopifyReadOnlyClient` is now absolutely query-only: no document that is not a read can leave
through it, and it holds no allowance for any write. That is only honest if the write RESOLVE
declares somewhere has its own, smaller home — otherwise the read-only object is one refactor away
from being the general transport again, which is exactly the shape 14C.7N found it had.

So this class exists for one operation. It is a separate object, constructed separately, over the
SAME configured credential: one Shopify app, one client id, one client secret, one short-lived
access token, held by one `ShopifyAdminTransport` instance that the read capability and this one
share. Separating a capability must never duplicate a secret, and a second installed app would be a
second grant to audit — neither is created here, and no scope is requested by it.

The gate is the mirror image of the read client's. A document may be transmitted only if it is:

  * a `mutation` — so a query is refused here too, and belongs to the read-only client;
  * well-formed, with exactly ONE root field — a document that bundles a declared write next to
    anything else is refused as a whole, rather than partly sent;
  * that one field is named in `SHOPIFY_DATA_USE.intended_write_mutations`.

Anything else is refused locally, before the token exchange, and raises
``ShopifyWriteCapabilityViolation`` — which the return provider already maps to a definite
"nothing was sent" outcome, never to an unknown one. Shopify's `write_returns` scope would permit
several more return operations; the scope grant is not the limit, this declaration is.

`submit_return_create` is deliberately not named `execute_graphql` and there is no generic send on
this class: the only thing it will carry is the operation its name states. Who may build it is
pinned by a test to the return-provider seam and nothing else in the application.
"""
from __future__ import annotations

import logging
from typing import Any

from app.tools.shopify.admin_transport import (
    DEFAULT_API_VERSION,
    DEFAULT_TIMEOUT_SECONDS,
    ShopifyAdminTransport,
)
from app.tools.shopify.data_use import SHOPIFY_DATA_USE
from app.tools.shopify.document_gate import MUTATION, classify
from app.tools.shopify.errors import ShopifyWriteCapabilityViolation

logger = logging.getLogger(__name__)


def declared_write_operations() -> tuple[str, ...]:
    """
    The operations this capability may transmit, read from the machine-readable data-use
    declaration rather than restated here.

    It is a lookup, not a wildcard: an operation the held `write_returns` scope would permit but the
    declaration does not name is refused, and so is every operation RESOLVE holds no scope for —
    order edits, fulfillment creation, refunds, cancellations, deletes and voids.
    """
    return SHOPIFY_DATA_USE.intended_write_mutations


def _declared_write_refusal(document: object) -> str | None:
    """
    Return None when this capability may transmit `document`, or the refusal message when not.

    The message names the document's kind and, when the classifier read one, its single root
    operation — never the document's text, which is caller-supplied content that could carry an
    object id, a customer field or a credential inside it.
    """
    verdict = classify(document)
    operations = declared_write_operations()
    prefix = (
        "RESOLVE's Shopify return-write capability transmits exactly one declared return operation "
        "and nothing else, so this document was refused locally before any request was built"
    )
    not_sent = "No token exchange, no header, no request body and no HTTP call took place."

    if verdict.kind != MUTATION:
        why = (
            "it is a read query, which belongs to the read-only client"
            if verdict.is_read
            else f"its kind is {verdict.kind}"
        )
        return f"{prefix}: {why}. {not_sent}"

    attempted = verdict.surface_name
    if not verdict.well_formed:
        return (
            f"{prefix}: its kind is mutation but it cannot be read as one operation"
            + (f" ({verdict.reason})" if verdict.reason else "")
            + f". {not_sent}"
        )
    if len(verdict.root_fields) != 1:
        return (
            f"{prefix}: a mutation must carry exactly one root operation and this one carries "
            f"{len(verdict.root_fields)}. {not_sent}"
        )
    if attempted not in operations:
        return (
            f"{prefix}: it asks for mutation {attempted}, which RESOLVE does not declare. "
            f"Declared: {', '.join(operations) or 'none'}. {not_sent}"
        )
    return None


class ShopifyReturnWriteClient:
    """
    The single declared write operation, on an object that cannot read and cannot write anything else.
    """

    name = "shopify_return_write"

    def __init__(
        self,
        *,
        store_domain: str = "",
        client_id: str = "",
        client_secret: str = "",
        api_version: str = DEFAULT_API_VERSION,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Any = None,
        admin: ShopifyAdminTransport | None = None,
    ) -> None:
        # Same rule as the read client: absent credentials are not an error at construction time.
        #
        # `admin` is how one credential stays one credential. `get_shopify_clients()` builds a single
        # transport and hands it to the read client and to this one, so the client-credentials
        # exchange happens once and is shared, and no second secret value exists anywhere in the
        # process. Constructing this class from bare arguments gives it its own transport — used by
        # tests that want an isolated capability — but never a second credential.
        self._admin = admin or ShopifyAdminTransport(
            store_domain=store_domain,
            client_id=client_id,
            client_secret=client_secret,
            api_version=api_version,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    # ------------------------------------------------------------------ identity
    @property
    def store_domain(self) -> str:
        return self._admin.store_domain

    @property
    def api_version(self) -> str:
        return self._admin.api_version

    @property
    def graphql_url(self) -> str:
        return self._admin.graphql_url

    @property
    def permitted_write_operations(self) -> tuple[str, ...]:
        """What this capability can send, as declared — a refusal is only useful if it is checkable."""
        return declared_write_operations()

    def validate_configuration(self) -> None:
        """Public, network-free check that all three SHOPIFY_* settings are present."""
        self._admin.validate_configuration()

    # ------------------------------------------------------------------ the one write
    async def submit_return_create(
        self, document: str, variables: dict[str, Any] | None = None
    ) -> dict:
        """
        Transmit the declared return write and return Shopify's `data` dict.

        The document's KIND and ROOT OPERATION are decided locally first, so a caller that hands this
        method something else — an order edit, a cancellation, a refund, a second return operation, a
        query, a malformed document — gets ``ShopifyWriteCapabilityViolation`` and Shopify is never
        contacted. The document text itself is never logged or quoted back.

        What this method does NOT decide is whether the return should happen. Eligibility, authority
        and the target line are the return provider's and the Authority Engine's, and this object
        knows nothing about them; it only bounds which operation may leave the process.
        """
        refusal = _declared_write_refusal(document)
        if refusal is not None:
            logger.error("Shopify return-write boundary: %s", refusal)
            raise ShopifyWriteCapabilityViolation(refusal)

        return await self._admin.execute(document, variables)


__all__ = ["ShopifyReturnWriteClient", "declared_write_operations"]
