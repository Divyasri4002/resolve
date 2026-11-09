"""
Test fixtures for the merchant-side channel.

Everything here is in-process and deterministic: the merchant store is the scripted fake, RESOLVE is
a fake HTTP client that checks the signature over the exact bytes it received, and the outbox lives
in a temporary file. No network, no credential, no real Shopify order.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from merchant_channel.channel import PurchaseChannel  # noqa: E402
from merchant_channel.config import ChannelConfig, Secret  # noqa: E402
from merchant_channel.fake_store import FakeAdminStore  # noqa: E402
from merchant_channel.linkage import SIGNATURE_HEADER  # noqa: E402
from merchant_channel.outbox import Outbox  # noqa: E402

SECRET = "disposable-merchant-channel-secret-not-a-credential"
# The channel's own Shopify credential. Fake, and only useful for proving it never surfaces.
TOKEN = "shpua_disposable-not-a-real-token"
RESOLVE_URL = "http://resolve.test"
ORDER_ID = "order_merchantchannel001"
PRODUCT_GID = "gid://shopify/Product/10622982422815"
VARIANT_GID = "gid://shopify/ProductVariant/52923834990879"


def make_config(**overrides) -> ChannelConfig:
    values = {
        "store_domain": "resolve-commerce-lab.myshopify.com",
        "api_version": "2026-07",
        "access_token": Secret(TOKEN),
        "linkage_secret": Secret(SECRET),
        "resolve_base_url": RESOLVE_URL,
        "expected_currency": "INR",
        "expected_shop_name": "RESOLVE Commerce Lab",
        "source": "merchant-purchase-channel",
    }
    values.update(overrides)
    return ChannelConfig(**values)


def handoff(**overrides):
    from merchant_channel.channel import PurchaseHandoff

    values = {
        "resolve_order_id": ORDER_ID,
        "shopify_product_gid": PRODUCT_GID,
        "shopify_variant_gid": VARIANT_GID,
        "quantity": 1,
        "currency": "INR",
    }
    values.update(overrides)
    return PurchaseHandoff(**values)


class FakeResolve:
    """
    RESOLVE's integration endpoint, as seen by the channel.

    It verifies the HMAC over the exact bytes (the same rule the real route applies), records the
    body it was given, and answers with a scripted status so a refusal or a lost answer is testable
    without a running server.
    """

    def __init__(self, *, status_code=200, payload=None, fail_with=None) -> None:
        self.status_code = status_code
        self.payload = payload if payload is not None else {"outcome": "linked"}
        self.fail_with = fail_with
        self.requests: list[dict] = []

    async def post(self, url, *, content=None, headers=None, **_kwargs):
        if self.fail_with is not None:
            raise self.fail_with(url)
        self.requests.append(
            {"url": url, "body": content, "headers": dict(headers or {})}
        )
        return self._Response(self.status_code, self.payload)

    @property
    def bodies(self) -> list[bytes]:
        return [request["body"] for request in self.requests]

    def signed_events(self) -> list[dict]:
        """The events RESOLVE accepted, recomputed from what it received — not from the channel."""
        events = []
        for request in self.requests:
            body = request["body"]
            signature = request["headers"].get(SIGNATURE_HEADER, "")
            if _hmac(SECRET, body) != signature:
                raise AssertionError("the channel sent a signature that does not match its body")
            events.append(json.loads(body.decode("utf-8")))
        return events

    class _Response:
        def __init__(self, status_code, payload) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload


def _hmac(secret: str, body: bytes) -> str:
    import hashlib
    import hmac

    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.fixture
def outbox(tmp_path) -> Outbox:
    return Outbox(tmp_path / "outbox.sqlite3")


@pytest.fixture
def store() -> FakeAdminStore:
    return FakeAdminStore()


@pytest.fixture
def resolve() -> FakeResolve:
    return FakeResolve()


@pytest.fixture
def channel(outbox, store, resolve) -> PurchaseChannel:
    return PurchaseChannel(
        make_config(),
        store,
        outbox,
        clock=lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
        http_client=resolve,
    )
