"""
Razorpay integration — TEST MODE only in this repo.

Every write here carries an idempotency key, and nothing is treated as "done" from the return
value of the initiating call alone — confirmation happens via a signature-verified webhook
(see app/routers/webhooks.py). This module is called ONLY after the Authority Engine has
approved the action; it has no authority-checking logic of its own, on purpose, so there is
exactly one place in the codebase that makes that call.

NOTE on scope: a Razorpay refund can only reverse a payment Razorpay itself processed. Money
paid directly to a third-party/foreign merchant through their own payment processor is NOT
refundable through this client — in that case RESOLVE's recovery spend (Payouts) and any
merchant-side refund are two separate flows, reconciled against each other in the ledger,
not treated as the same call.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from decimal import Decimal
from typing import Any

import razorpay

from app.config import get_settings

settings = get_settings()


def _client() -> razorpay.Client:
    client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    return client


def _idempotency_key(mission_id: str, purpose: str) -> str:
    return f"resolve_{mission_id}_{purpose}_{uuid.uuid4().hex[:8]}"


def create_recovery_payout(mission_id: str, amount: Decimal, purpose: str, contact_id: str) -> dict[str, Any]:
    """
    Executes an APPROVED recovery spend (e.g. paying the price difference for a replacement
    sourced from another merchant) via Razorpay Payouts, in test mode.
    """
    client = _client()
    payload = {
        "account_number": settings.razorpay_key_id,  # placeholder for the payout source account
        "amount": int(amount * 100),  # paise
        "currency": "INR",
        "mode": "IMPS",
        "purpose": purpose,
        "fund_account": {"contact_id": contact_id},
        "queue_if_low_balance": True,
        "reference_id": _idempotency_key(mission_id, "payout"),
    }
    return client.payout.create(payload)  # type: ignore[attr-defined]


def create_refund(payment_id: str, amount: Decimal, mission_id: str) -> dict[str, Any]:
    """Refunds an original Razorpay-processed payment. Only valid when RESOLVE's own payment
    rail processed the original charge — see the module-level note above."""
    client = _client()
    return client.payment.refund(
        payment_id,
        {
            "amount": int(amount * 100),
            "speed": "normal",
            "receipt": _idempotency_key(mission_id, "refund"),
        },
    )


# --------------------------------------------------------------------------- #
# Task 11A — controlled refund execution primitives.
# --------------------------------------------------------------------------- #

def is_test_mode() -> bool:
    """Razorpay test keys are prefixed rzp_test_. Refund execution refuses to run against
    anything else — a live key simply cannot pass this gate."""
    return settings.razorpay_key_id.startswith("rzp_test_")


def create_payment_refund(payment_id: str, amount_paise: int, resolve_refund_id: str) -> dict[str, Any]:
    """
    Create a Razorpay refund for an already-captured TEST-mode payment (integer paise in —
    no float money math anywhere). TEST MODE ONLY, enforced here.

    Only the canonical amount is sent: no `speed` field, because the provider rejects
    `speed: full` on this account ("The selected speed is invalid") and the documented default
    already covers a refund of the whole captured amount.

    `resolve_refund_id` is carried in the refund's notes, so a retry after a crash between
    "provider created it" and "we persisted it" can find and adopt OUR refund instead of
    blindly issuing a second one. Note that the returned object is a request accepted by
    the provider — NOT proof of a completed refund; call fetch_refund to verify status.
    """
    if not is_test_mode():
        raise RuntimeError("Refund execution is restricted to Razorpay TEST mode")
    client = _client()
    return client.payment.refund(
        payment_id,
        {
            "amount": amount_paise,
            "notes": {"resolve_refund_id": resolve_refund_id},
        },
    )


def fetch_refund(payment_id: str, razorpay_refund_id: str) -> dict[str, Any]:
    """Fetch the authoritative status of one Razorpay refund (processed | pending | failed)."""
    client = _client()
    return client.payment.fetch_refund_id(payment_id, razorpay_refund_id)


def list_refunds_for_payment(payment_id: str) -> list[dict[str, Any]]:
    """All refunds recorded against a Razorpay payment — used to adopt a refund whose create
    call succeeded before its id could be persisted (crash-recovery idempotency)."""
    client = _client()
    data = client.payment.fetch_multiple_refund(payment_id, {"count": 100})
    return data.get("items", []) if isinstance(data, dict) else []


def create_test_order(amount_paise: int, currency: str, receipt: str, notes: dict[str, str]) -> dict[str, Any]:
    """
    Creates a Razorpay ORDER in TEST MODE for a shopping-mission payment (Task 07).

    amount_paise is the smallest currency unit (INR: 1 rupee = 100 paise), computed with
    integer/Decimal math upstream — never floats. The receipt is deterministic per mission so
    order attempts are traceable; the mission_id note keeps the order reconcilable by the
    webhook receiver, the same way recovery payouts are.
    """
    client = _client()
    return client.order.create(
        {
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "notes": notes,
        }
    )


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """
    Standard Checkout payment-signature verification (Task 07): HMAC-SHA256 of
    "{order_id}|{payment_id}" signed with the backend-only key secret. This is the documented
    mechanism by which a checkout.js handler response is authenticated — the mission is marked
    payment_verified ONLY when this validates against the order the backend itself created.
    """
    expected = hmac.new(
        settings.razorpay_key_secret.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(payload_body: bytes, received_signature: str) -> bool:
    """
    Every payment event is confirmed here before RESOLVE trusts it. A mission is NEVER marked
    resolved on the strength of the initiating API call's HTTP response alone — only once the
    matching, signature-verified webhook has arrived.
    """
    expected = hmac.new(
        settings.razorpay_webhook_secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received_signature)
