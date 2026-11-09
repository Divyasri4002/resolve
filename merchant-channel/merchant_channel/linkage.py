"""
Building, signing and delivering `resolve.order_linkage.v1` — the channel's only voice in RESOLVE.

The contract is RESOLVE's (Task 14C.7B) and is not modified here: the model allows exactly seven
fields and forbids the rest, and this channel sends six of them — never `mission_id`, because it
knows the order itself. `shopify_order_id` is a canonical Shopify order GID, the signature is
HMAC-SHA256 over the **raw bytes** that are sent, in `x-resolve-signature`. Nothing about money,
nothing about the customer: RESOLVE reads those from its own rows, and the event's job is only to
carry a pairing that the sender is entitled to assert.

Attempt 1 of a pairing uses the derivation this channel shipped with, and it is frozen:

    "evt-" + sha256(correlation_id|shopify_order_id)[:24]

so a resend of the same pairing is byte-identical to the first one. That is what makes retry safe —
RESOLVE's replay guard answers a settled event from storage instead of re-verifying it, and a *changed*
body under the same id is a conflict it refuses. A channel that generated a fresh id per attempt would
turn its own retry into a brand-new claim, and that is how two claims for one purchase appear.

A reclaim (Task 14C.7L) is the one case where a new claim is the point, and it is not allowed to reach
for this function directly. An attempt number arrives only from the outbox, only after a settled
refusal, and only through ``PurchaseChannel.reclaim_linkage`` — which reads the merchant store again
and cannot create anything:

    attempt 1 -> sha256(correlation|order)          (the frozen preimage)
    attempt N -> sha256(correlation|order|N)         with N > 1

Attempt 1's preimage deliberately does not carry its own number. Changing it would re-name pairings
that were already settled under the frozen derivation, and a re-named settled claim is exactly the
second claim the paragraph above is about.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from urllib.parse import urlparse

EVENT_TYPE = "resolve.order_linkage.v1"
SIGNATURE_HEADER = "x-resolve-signature"
LINKAGE_ROUTE = "/integrations/shopify/order-linkage"
ORDER_GID_PREFIX = "gid://shopify/Order/"
_ORDER_GID_TAIL = re.compile(r"[0-9]+\Z")
_EVENT_ID_SHAPE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class LinkageContractError(Exception):
    """The channel is about to say something the contract does not allow. Nothing was sent."""


def event_id_for(
    *, correlation_id: str, shopify_order_id: str, attempt: int = 1
) -> str:
    """
    The identity of one *attempt* at one pairing.

    `attempt` comes from the outbox and nowhere else — never from a caller, a clock, or a random
    draw — so the only way to reach a second identity is to have a row that was refused and to
    rotate it deliberately.
    """
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise LinkageContractError(
            f"a linkage attempt number must be a whole number of at least 1, not {attempt!r}"
        )
    preimage = f"{correlation_id}|{shopify_order_id}"
    if attempt > 1:
        preimage = f"{preimage}|{attempt}"
    digest = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    return f"evt-{digest[:24]}"


def build_event(
    *,
    correlation_id: str,
    shopify_order_id: str,
    resolve_order_id: str,
    issued_at: str,
    source: str,
    attempt: int = 1,
) -> dict:
    """The seven permitted fields and nothing else."""
    if not shopify_order_id.startswith(ORDER_GID_PREFIX):
        raise LinkageContractError(
            "a linkage event must name a canonical Shopify order GID, and the value supplied does "
            "not start with "
            f"{ORDER_GID_PREFIX!r}. Nothing was sent."
        )
    tail = shopify_order_id[len(ORDER_GID_PREFIX) :]
    if not _ORDER_GID_TAIL.match(tail):
        raise LinkageContractError(
            "a linkage event must carry the numeric global id Shopify issued, so a product or "
            "variant GID can never be passed off as an order. Nothing was sent."
        )
    if len(resolve_order_id) > 64 or not resolve_order_id.strip():
        raise LinkageContractError("the RESOLVE order id this event names is not usable")
    if len(source) > 64 or not source.strip():
        raise LinkageContractError("the event must say which integration sent it")
    event = {
        "event_id": event_id_for(
            correlation_id=correlation_id,
            shopify_order_id=shopify_order_id,
            attempt=attempt,
        ),
        "event_type": EVENT_TYPE,
        "shopify_order_id": shopify_order_id,
        "issued_at": issued_at,
        "resolve_order_id": resolve_order_id,
        "source": source,
    }
    # Exactly one of `resolve_order_id` / `mission_id` names the order. The channel always knows the
    # order itself, so `mission_id` is never sent, and the dict above is the whole contract.
    return event


def serialize_event(event: dict) -> bytes:
    """
    The exact bytes that will be sent, and therefore the exact bytes that are signed.

    Signing a re-serialisation of a parsed body is the classic way to build a verifier that agrees
    with itself and nothing else.
    """
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_event(secret: str, body: bytes) -> str:
    if not secret:
        raise LinkageContractError(
            "no linkage secret is configured, so nothing can be signed and nothing was sent"
        )
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def deliver_event(
    client, *, base_url: str, body: bytes, signature: str
) -> tuple[int, dict]:
    """
    POST the event to RESOLVE and hand back the answer as-is.

    A transport failure is not a refusal: it raises, and the caller records the pairing as
    delivery-unknown so it re-sends the same bytes under the same id rather than re-creating an order.
    """
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise LinkageContractError("the RESOLVE base url is not a usable absolute URL")
    url = f"{base_url.rstrip('/')}{LINKAGE_ROUTE}"
    response = await client.post(
        url, content=body, headers={SIGNATURE_HEADER: signature, "content-type": "application/json"}
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {"detail": str(payload)}
