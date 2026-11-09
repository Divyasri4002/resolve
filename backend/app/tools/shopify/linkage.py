"""
Server-side linkage between a RESOLVE order and one Shopify order (Task 14C.5).

Why this exists: a merchant return can only be filed against a merchant order, and RESOLVE must
never have to guess which one. `ShopifyReturnProvider.resolve_return_target` therefore reads the
canonical reference off the persisted RESOLVE order and refuses — without submitting anything —
when none is there. This module is the only place that reference is ever written.

What makes a linkage trustworthy is not who asked for it but where the value came from, so the
rules here are deliberately narrow:

  * it is a server-side operation. There is NO endpoint, form, or parameter that carries a Shopify
    order id: no browser, and no request body of any kind, can reach this code with a reference;
  * the caller must already own the RESOLVE order, and the mission is loaded from the database,
    not from the caller;
  * the reference is verified AGAINST SHOPIFY before it is stored. Shopify must name that exact
    order at this store, so an invented, mistyped, or foreign id is refused instead of becoming a
    permanent record that later authorizes a mutation;
  * and — Task 14C.7F — when the RESOLVE order carries canonical Shopify identity from a purchase
    RESOLVE made out of that store's own catalog, Shopify's own order lines must sell the SAME
    Product GID and Variant GID, at the SAME store domain, before the linkage is stored. An
    integration can assert a pairing; only Shopify's line data can corroborate it, and a
    contradiction is refused rather than recorded.
  * one RESOLVE order holds at most one Shopify order, one Shopify order backs at most one RESOLVE
    order (enforced by a unique index, not just by this code), and once established the linkage
    cannot be moved;
  * repeating the same linkage is a no-op, so an integration retrying an event is safe.

What this module deliberately does NOT do is infer a linkage. A matching product title, an equal
amount, a nearby timestamp, the same merchant name, or any other similarity is not evidence that
two orders in two systems are the same order, and acting on such an inference is exactly how a
return gets filed against someone else's purchase. If Shopify has not said "this order", and no
integration has carried that statement in, RESOLVE does not know it and says so.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import ResolveOrderRecord
from app.tools.shopify.client import ShopifyReadOnlyClient
from app.tools.shopify.errors import ShopifyError
from app.tools.shopify.sanitizer import redact

logger = logging.getLogger(__name__)

ORDER_GID_PREFIX = "gid://shopify/Order/"
# A Shopify order global id is the prefix plus the store-internal numeric id and nothing else.
# Stricter than "some text after a slash": a linkage authorizes mutations later, so the shape has
# to be the shape Shopify actually issues.
_ORDER_GID_TAIL = re.compile(r"[0-9]+\Z")


class ShopifyLinkageError(Exception):
    """A linkage that RESOLVE will not store. `status_code` follows the project's convention."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(redact(message))
        self.message = redact(message)
        self.status_code = status_code


def canonical_shopify_order_reference(value: object) -> str:
    """
    Return the value if it is exactly one canonical Shopify order id, else refuse.

    Whitespace is not part of an id, and nothing here assembles or repairs one: what goes in is
    what comes out, or nothing does.
    """
    if not isinstance(value, str) or not value.strip():
        raise ShopifyLinkageError(
            "A Shopify order reference is required, and none was supplied. No linkage was made."
        )
    cleaned = value.strip()
    if not cleaned.startswith(ORDER_GID_PREFIX):
        raise ShopifyLinkageError(
            f"The Shopify order reference {redact(cleaned)!r} is not a canonical Shopify order "
            "global id of the form "
            f"{ORDER_GID_PREFIX}<numeric-id>. No linkage was made."
        )
    tail = cleaned[len(ORDER_GID_PREFIX) :]
    if not _ORDER_GID_TAIL.match(tail):
        raise ShopifyLinkageError(
            f"The Shopify order reference {redact(cleaned)!r} does not carry the numeric global id "
            "Shopify issues, so it cannot be trusted as a canonical order. No linkage was made."
        )
    return cleaned


async def link_order_to_shopify_order(
    db: AsyncSession,
    *,
    mission_id: str,
    actor_user_id: str,
    shopify_order_id: str,
    client: ShopifyReadOnlyClient,
) -> ResolveOrderRecord:
    """
    Record which Shopify order a RESOLVE order corresponds to, and return the persisted row.

    Raises ``ShopifyLinkageError`` — storing nothing — when the order is absent, belongs to someone
    else, is already linked to a different Shopify order, when that Shopify order is already
    claimed by another RESOLVE order, when the reference is malformed, when Shopify does not
    confirm the order exists at this store, or — Task 14C.7F — when the RESOLVE order's own
    canonical Shopify identity is not what Shopify says that order sells.
    """
    order = await db.scalar(select(ResolveOrderRecord).where(
        ResolveOrderRecord.mission_id == mission_id
    ))
    if order is None:
        raise ShopifyLinkageError(
            f"No RESOLVE order exists for mission {redact(str(mission_id))}, so there is nothing "
            "to link.",
            status_code=404,
        )
    if not isinstance(actor_user_id, str) or order.user_id != actor_user_id:
        # The same answer as "no such order": an outsider learns nothing about someone else's
        # mission, and a server-side caller that lost its subject learns that it cannot proceed.
        raise ShopifyLinkageError(
            "The order for that mission does not belong to the acting user, so no linkage was "
            "made.",
            status_code=404,
        )

    reference = canonical_shopify_order_reference(shopify_order_id)

    if order.provider_order_reference is not None:
        if order.provider_order_reference == reference:
            # Idempotent: an integration delivering the same event twice is not a change.
            logger.info(
                "Shopify order linkage already present for mission %s", redact(mission_id)
            )
            return order
        raise ShopifyLinkageError(
            f"Order {order.order_id} is already linked to a Shopify order, and an established "
            "linkage is not moved by a later write. Re-pointing it would silently re-aim every "
            "future return at a different purchase.",
            status_code=409,
        )

    already_claimed = await db.scalar(
        select(ResolveOrderRecord).where(
            ResolveOrderRecord.provider_order_reference == reference,
            ResolveOrderRecord.order_id != order.order_id,
        )
    )
    if already_claimed is not None:
        raise ShopifyLinkageError(
            "That Shopify order is already linked to a different RESOLVE order, so linking it "
            "again would let one merchant return answer for two purchases.",
            status_code=409,
        )

    # Verification, not inference: Shopify has to name this order at this store before RESOLVE
    # treats the reference as real. A store that cannot be reached is a refusal, not a licence to
    # record an unverified pointer.
    await _verify_against_shopify(client, reference)

    # And when RESOLVE bought the item out of a Shopify catalog it can name, Shopify's own order
    # lines have to say the same thing RESOLVE persisted (Task 14C.7F). A signed event proves that
    # an integration SAID so; this proves the merchant order is about the same purchase.
    await _verify_canonical_identity_against_shopify(order, client, reference)

    order.provider_order_reference = reference
    try:
        await db.commit()
    except IntegrityError as exc:
        # The unique index is the arbiter when two callers race past the read above.
        await db.rollback()
        raise ShopifyLinkageError(
            "That Shopify order is already linked to a RESOLVE order, so the linkage was refused "
            "by the storage-level invariant.",
            status_code=409,
        ) from exc
    await db.refresh(order)
    logger.info(
        "Linked RESOLVE order %s to Shopify order %s",
        redact(order.order_id),
        redact(reference),
    )
    return order


async def _verify_against_shopify(
    client: ShopifyReadOnlyClient, reference: str
) -> None:
    """Ask Shopify for that exact order. Anything other than a clear answer is a refusal."""
    try:
        confirmed = await client.read_order_reference(reference)
    except ShopifyError as exc:
        raise ShopifyLinkageError(
            "Shopify could not confirm that order, so no linkage was stored: " + exc.message,
            status_code=503,
        ) from exc
    except Exception as exc:  # a failure to verify is never a licence to link
        raise ShopifyLinkageError(
            f"Shopify could not be asked whether that order exists ({redact(str(exc))}), so no "
            "linkage was stored.",
            status_code=503,
        ) from exc
    if confirmed.order_id != reference:
        # Shopify answered about a different object than the one being recorded.
        raise ShopifyLinkageError(
            "Shopify confirmed a different order than the reference supplied, so the linkage was "
            "refused.",
            status_code=409,
        )


def _canonical_identity_gap(order: ResolveOrderRecord) -> str | None:
    """
    One sentence on why this row's own Shopify identity cannot be checked, or None.

    A row that carries a product mirror and nothing else is not "unverifiable, proceed anyway": the
    mirror columns are written together from one server-side selection, so a half-populated set is
    a contradiction inside RESOLVE's own record, and pairing anything against it would rest on data
    RESOLVE does not trust.
    """
    product_gid = order.shopify_product_gid
    variant_gid = order.shopify_variant_gid
    store_domain = order.shopify_store_domain
    if not (product_gid or variant_gid or store_domain):
        # Nothing claims this purchase came from a Shopify catalog, so there is no canonical
        # identity to hold the merchant order against. Silence, not a match.
        return None
    if not (product_gid and variant_gid and store_domain):
        return (
            "This RESOLVE order carries only part of a Shopify product identity, so the merchant "
            "order cannot be checked against what RESOLVE actually sold. Nothing was linked."
        )
    return None


async def _verify_canonical_identity_against_shopify(
    order: ResolveOrderRecord, client: ShopifyReadOnlyClient, reference: str
) -> None:
    """
    Require Shopify's own order lines to sell what RESOLVE's own row says it sold (Task 14C.7F).

    A signature proves that an integration ASSERTED a pairing; it cannot prove the assertion is
    about the same purchase. This is the corroborating half: the RESOLVE order's canonical Product
    GID and Variant GID — written from the merchant catalog's own answer at the moment of selection
    — must appear on the Shopify order's lines, at the store domain they were read from. Shopify
    answers with ids it issued, so a matching line means the merchant order demonstrably contains
    this purchase, and a non-matching one means it demonstrably does not.

    Everything is fail-closed. The check never falls back to a title, an amount, a currency, a SKU,
    a merchant name or a timestamp, and it never picks a "best" line: a variant that is not on the
    order, a variant that appears twice, a line whose product disagrees with its own variant, a
    merchant order at a different store, or a line list RESOLVE could not read to the end all
    refuse the linkage. Where the RESOLVE order carries no Shopify identity at all there is nothing
    to check, and nothing is claimed either way.
    """
    gap = _canonical_identity_gap(order)
    if gap is not None:
        raise ShopifyLinkageError(gap, status_code=409)
    if not order.shopify_product_gid:
        return  # no Shopify-native identity on this row: nothing to corroborate, nothing to deny

    store = getattr(client, "store_domain", "") or ""
    if order.shopify_store_domain != store:
        # The identity was read from one store and this order is being read from another. Two
        # merchants can sell the same product at the same price; their global ids cannot be the same
        # purchase, and neither can two stores' answers be merged into one linkage.
        raise ShopifyLinkageError(
            "The Shopify identity on this RESOLVE order was read from a different store than the "
            "order being linked, so they cannot be established as the same purchase. Nothing was "
            "linked.",
            status_code=409,
        )

    try:
        lines = await client.read_order_line_identity(reference)
    except ShopifyError as exc:
        raise ShopifyLinkageError(
            "Shopify could not say what that order sells, so the pairing could not be verified and "
            "nothing was linked: " + exc.message,
            status_code=503,
        ) from exc
    except Exception as exc:  # a failure to verify is never a licence to link
        raise ShopifyLinkageError(
            f"Shopify could not be asked what that order sells ({redact(str(exc))}), so nothing was "
            "linked.",
            status_code=503,
        ) from exc

    if lines.order_id != reference:
        raise ShopifyLinkageError(
            "Shopify answered about a different order than the one being verified, so nothing was "
            "linked.",
            status_code=409,
        )
    if not lines.all_lines_read:
        # "Not among the lines I read" is not "not on this order". An unread remainder cannot
        # support a match, and it cannot support a refusal either, so neither is claimed.
        raise ShopifyLinkageError(
            "Shopify reports that this order has more lines than RESOLVE read, so the order could "
            "not be verified completely and nothing was linked.",
            status_code=409,
        )

    matches = [line for line in lines.lines if line.variant_id == order.shopify_variant_gid]
    if len(matches) > 1:
        raise ShopifyLinkageError(
            f"Shopify names the variant RESOLVE sold on {len(matches)} lines of that order, so "
            "which line this purchase is cannot be determined from canonical data, and nothing was "
            "linked.",
            status_code=409,
        )
    if len(matches) == 1:
        if matches[0].product_id != order.shopify_product_gid:
            # Shopify's own pair disagrees with RESOLVE's own pair. The variant belongs to another
            # product, so the row and the order are not describing one purchase.
            raise ShopifyLinkageError(
                "Shopify's own line data puts that variant on a different product than the one "
                "RESOLVE sold, so the two are not the same purchase and nothing was linked.",
                status_code=409,
            )
        return

    if any(line.product_id == order.shopify_product_gid for line in lines.lines):
        raise ShopifyLinkageError(
            "That Shopify order sells this product but not the variant RESOLVE sold, so it is not "
            "the order for this purchase and nothing was linked.",
            status_code=409,
        )
    raise ShopifyLinkageError(
        "That Shopify order does not sell the product RESOLVE sold, so it is not the order for this "
        "purchase and nothing was linked.",
        status_code=409,
    )


__all__ = [
    "ORDER_GID_PREFIX",
    "ShopifyLinkageError",
    "canonical_shopify_order_reference",
    "link_order_to_shopify_order",
]
