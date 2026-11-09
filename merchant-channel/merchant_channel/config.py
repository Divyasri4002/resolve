"""
The channel's own configuration, read from the channel's own environment values.

Why this file exists separately instead of reusing anything: the moment this module could read
RESOLVE's settings it would also be able to read RESOLVE's Shopify token, and the whole
"merchant-owned infrastructure outside RESOLVE's write boundary" claim would be fiction. So the
names it accepts are prefixed ``MERCHANT_``, and a RESOLVE-owned name appearing in its place is an
error rather than a convenience.

Secrets are wrapped, never bare: a ``Secret`` value renders as ``Secret(***)`` in any repr, log line
or exception text, and its content is only ever released through ``reveal()`` at the two places that
must sign or authorize a call.

Two Shopify credential paths are supported, decided by ``credential_mode()`` and never mixed: a
static ``MERCHANT_SHOPIFY_ACCESS_TOKEN`` (used exactly as supplied, no grant request), or the
current Dev Dashboard pair ``MERCHANT_SHOPIFY_CLIENT_ID`` / ``MERCHANT_SHOPIFY_CLIENT_SECRET``
 exchanged for a short-lived token by ``merchant_channel.auth``. The static token wins when both
are configured. Loading configuration performs no HTTP either way.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class Secret:
    """A string that refuses to be printed."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value or ""

    def reveal(self) -> str:
        return self._value

    @property
    def configured(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:  # never leak through logging or a pytest assertion header
        return "Secret(***)" if self._value else "Secret(<unset>)"

    __str__ = __repr__


# Environment values that belong to RESOLVE. Reading one of these would mean the channel had
# acquired RESOLVE's Shopify identity — a different app with different scopes — which is exactly the
# boundary this package exists to keep.
RESOLVE_OWNED_ENV = (
    "SHOPIFY_ACCESS_TOKEN",
    "SHOPIFY_CLIENT_ID",
    "SHOPIFY_CLIENT_SECRET",
    "SHOPIFY_STORE_DOMAIN",
    "SHOPIFY_API_VERSION",
    "LINKAGE_INTEGRATION_SECRET",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
)


class ChannelConfigError(Exception):
    """The channel is not configured. Nothing was sent anywhere."""


@dataclass(frozen=True)
class ChannelConfig:
    store_domain: str
    api_version: str
    access_token: Secret = field(repr=False)
    linkage_secret: Secret = field(repr=False)
    resolve_base_url: str
    expected_currency: str
    expected_shop_name: str
    source: str
    # The current Shopify Dev Dashboard credential pair. Either this pair or the static access
    # token above must be configured; the static token wins when both are present, and the
    # client-credentials pair is what a Dev Dashboard app can actually issue (24-hour tokens,
    # refreshed by the gateway's token provider, never pasted by hand per day).
    client_id: str | None = None
    client_secret: Secret | None = field(default=None, repr=False)

    @property
    def has_client_credentials(self) -> bool:
        return bool(
            self.client_id
            and self.client_secret is not None
            and self.client_secret.configured
        )

    def credential_mode(self) -> str:
        """
        Which credential path the channel will use: ``"static"`` or ``"client_credentials"``.

        The static token wins when both paths are configured, because a token supplied by hand is
        an explicit deployment decision. Raising here (rather than at first use) is what makes an
        unconfigured channel refuse instead of send.
        """
        if self.access_token.configured:
            return "static"
        if self.has_client_credentials:
            return "client_credentials"
        raise ChannelConfigError(
            "The merchant channel has no Shopify credential configured, so it cannot create an "
            "order. Set MERCHANT_SHOPIFY_ACCESS_TOKEN, or both MERCHANT_SHOPIFY_CLIENT_ID and "
            "MERCHANT_SHOPIFY_CLIENT_SECRET for a Dev Dashboard app. RESOLVE's token is not a "
            "substitute and was not used."
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.store_domain
            and self.linkage_secret.configured
            and self.resolve_base_url
            and (self.access_token.configured or self.has_client_credentials)
        )

    def __repr__(self) -> str:
        def state(secret: Secret | None) -> str:
            return "'set'" if secret is not None and secret.configured else "'unset'"

        return (
            f"ChannelConfig(store_domain={self.store_domain!r}, api_version={self.api_version!r}, "
            f"access_token={state(self.access_token)}, "
            f"linkage_secret={state(self.linkage_secret)}, "
            f"resolve_base_url={self.resolve_base_url!r}, "
            f"expected_currency={self.expected_currency!r}, "
            f"expected_shop_name={self.expected_shop_name!r}, source={self.source!r}, "
            f"client_id={self.client_id!r}, client_secret={state(self.client_secret)})"
        )


def _require_no_resolve_owned_names(environ) -> None:
    borrowed = [name for name in RESOLVE_OWNED_ENV if environ.get(name)]
    if borrowed:
        raise ChannelConfigError(
            "The merchant-side channel must not read RESOLVE's credentials. These RESOLVE-owned "
            f"environment values are set and were therefore not used: {sorted(borrowed)}. "
            "Configure the MERCHANT_* values instead."
        )


def load_config(environ=None) -> ChannelConfig:
    """
    Build the channel's configuration from ``MERCHANT_*`` values.

    Absent values are allowed and mean "not configured" — the channel then refuses to act. It never
    falls back to a default store, a default token, or RESOLVE's configuration. Where the outbox
    lives is not a credential question, so it is the caller's to decide and is passed to
    ``Outbox(...)`` directly, never read from here.
    """
    environ = os.environ if environ is None else environ
    _require_no_resolve_owned_names(environ)
    return ChannelConfig(
        store_domain=environ.get("MERCHANT_SHOPIFY_STORE_DOMAIN", ""),
        api_version=environ.get("MERCHANT_SHOPIFY_API_VERSION", ""),
        access_token=Secret(environ.get("MERCHANT_SHOPIFY_ACCESS_TOKEN", "")),
        linkage_secret=Secret(environ.get("MERCHANT_LINKAGE_SECRET", "")),
        resolve_base_url=environ.get("MERCHANT_RESOLVE_BASE_URL", ""),
        expected_currency=environ.get("MERCHANT_EXPECTED_CURRENCY", "INR"),
        expected_shop_name=environ.get("MERCHANT_EXPECTED_SHOP_NAME", ""),
        source=environ.get("MERCHANT_CHANNEL_SOURCE", "merchant-purchase-channel"),
        client_id=environ.get("MERCHANT_SHOPIFY_CLIENT_ID") or None,
        client_secret=(
            Secret(environ["MERCHANT_SHOPIFY_CLIENT_SECRET"])
            if environ.get("MERCHANT_SHOPIFY_CLIENT_SECRET")
            else None
        ),
    )
