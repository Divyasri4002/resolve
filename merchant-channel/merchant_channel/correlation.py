"""
Correlation identifiers, and the one reason they exist: a retry must be recognisable.

The channel cannot ask Shopify for idempotency. Measured live against the 2026-07 Admin schema,
``draftOrderCreate(input)``, ``draftOrderComplete(id, paymentGatewayId, sourceName)`` and
``orderCreate(order, options)`` expose no idempotency-key argument — of 523 mutation fields only
``appUsageRecordCreate`` and ``orderCreateMandatePayment`` carry one. So duplicate protection has to
come from the channel naming the same purchase the same way every time, and then looking that name
up before creating again.

A correlation id is therefore a pure function of the purchase, never a timestamp and never random:

    correlation_id = "RESOLVE-" + resolve_order_id + "-" + sha256(canonical payload)[:12]

The payload is the purchase and nothing else: which RESOLVE order, which variant, how many, and in
which currency. No amount, because the merchant's catalog owns the price the channel later verifies.
So:

    same RESOLVE order + same intended purchase    ->  same id, so a retry collides by construction;
    same RESOLVE order + a different purchase      ->  a different id, because it is a different
                                                       purchase and must not be silently deduped
                                                       into the first one.

The id is what gets stamped into the order as ``poNumber`` and what the read-back filters on
(``orders(query: "po_number:<id>")``), both measured live: ``po_number:`` is a real filter (an
impossible value answers with zero orders), while ``custom_attribute:`` is ignored and answers with
everything — which is why the correlation lives in ``poNumber`` and not in a custom attribute.
"""
from __future__ import annotations

import hashlib
import json
import re

CORRELATION_PREFIX = "RESOLVE-"
_HASH_LENGTH = 12
_CORRELATION_SHAPE = re.compile(r"^RESOLVE-[A-Za-z0-9_-]+-[0-9a-f]{12}$")


def canonical_payload(handoff) -> str:
    """
    The exact bytes the correlation hash is taken over.

    Key order is fixed and the fields are the purchase and nothing else: no customer, no timestamp,
    no free text. A payload that differs in any material way must produce a different correlation.
    """
    return json.dumps(
        {
            "resolve_order_id": handoff.resolve_order_id,
            "product_gid": handoff.shopify_product_gid,
            "variant_gid": handoff.shopify_variant_gid,
            "quantity": str(handoff.quantity),
            "currency": handoff.currency,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_hash(handoff) -> str:
    return hashlib.sha256(canonical_payload(handoff).encode("utf-8")).hexdigest()


def correlation_id(handoff) -> str:
    return f"{CORRELATION_PREFIX}{handoff.resolve_order_id}-{payload_hash(handoff)[:_HASH_LENGTH]}"


def is_correlation_id(value: object) -> bool:
    return isinstance(value, str) and bool(_CORRELATION_SHAPE.match(value))
