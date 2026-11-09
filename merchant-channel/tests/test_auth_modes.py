"""
The two credential paths the gateway can hold, measured against injected transports.

Nothing here reaches a network: both the token grant and the Admin GraphQL endpoint are answered
by an httpx.MockTransport, and the assertions are about what the channel sends and when — which
URL, which form fields, which header, how many grants, and what a refusal means. The static path
is measured by the same standard: zero grant requests, the supplied token in the header.

The token the grant returns in these tests is opaque on purpose (``shpua_``-shaped): that is the
shape the Dev Dashboard client-credentials grant actually issues, and the shape that must travel
as ``X-Shopify-Access-Token`` — never ``Authorization: Bearer``.
"""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import TOKEN, make_config

from merchant_channel.auth import MerchantTokenProvider
from merchant_channel.config import Secret, load_config
from merchant_channel.gateway import GatewayDefiniteFailure, ShopifyAdminGateway

CLIENT_ID = "client_id_test_value"
CLIENT_SECRET = "shpss_test-secret-not-a-credential"
GRANTED_TOKEN = "shpua_granted-not-a-real-token"

TOKEN_URL = "https://resolve-commerce-lab.myshopify.com/admin/oauth/access_token"
GRAPHQL_URL = "https://resolve-commerce-lab.myshopify.com/admin/api/2026-07/graphql.json"

EMPTY_READ_BACK = {"data": {"orders": {"edges": []}}}


def client_config():
    return make_config(
        access_token=Secret(""),
        client_id=CLIENT_ID,
        client_secret=Secret(CLIENT_SECRET),
    )


def recording_client(answer):
    """
    A client whose transport records every request and answers with `answer(entry, seen)`.

    `entry` is the request just sent; `seen` is the ordered list so far, so a scripted answer can
    depend on how many requests have already happened.
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        entry = {
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "content": request.content,
        }
        seen.append(entry)
        return answer(entry, seen)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _grant(entry, seen, expires_in=86400):
    return httpx.Response(200, json={"access_token": GRANTED_TOKEN, "expires_in": expires_in})


def grant_ok(entry, seen):
    return _grant(entry, seen)


def graphql_ok(entry, seen):
    return httpx.Response(200, json=EMPTY_READ_BACK)


def grant_then_graphql(entry, seen):
    if entry["url"] == TOKEN_URL:
        return grant_ok(entry, seen)
    return graphql_ok(entry, seen)


def grants_in(seen) -> list[dict]:
    return [s for s in seen if s["url"] == TOKEN_URL]


def graphqls_in(seen) -> list[dict]:
    return [s for s in seen if s["url"] == GRAPHQL_URL]


# ----------------------------------------------------------------------------- static path
async def test_static_token_mode_sends_the_supplied_token_and_no_grant():
    client, seen = recording_client(grant_then_graphql)
    gateway = ShopifyAdminGateway(make_config(), client=client)

    orders = await gateway.read_back("RESOLVE-order_t-000000000000")

    assert orders == []
    assert [s["url"] for s in seen] == [GRAPHQL_URL]
    assert seen[0]["headers"]["x-shopify-access-token"] == TOKEN


# ------------------------------------------------------------------- client-credentials path
async def test_the_grant_runs_first_and_its_token_is_what_is_presented():
    client, seen = recording_client(grant_then_graphql)
    gateway = ShopifyAdminGateway(client_config(), client=client)

    orders = await gateway.read_back("RESOLVE-order_t-000000000000")

    assert orders == []
    grant, graphql = seen[0], seen[1]
    assert grant["url"] == TOKEN_URL and grant["method"] == "POST"
    form = parse_qs(grant["content"].decode("utf-8"))
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == [CLIENT_SECRET]
    assert graphql["headers"]["x-shopify-access-token"] == GRANTED_TOKEN


async def test_an_opaque_granted_token_never_travels_as_a_bearer_header():
    client, seen = recording_client(grant_then_graphql)
    gateway = ShopifyAdminGateway(client_config(), client=client)

    await gateway.read_back("RESOLVE-order_t-000000000000")

    for entry in seen:
        assert "authorization" not in entry["headers"]
    graphql = graphqls_in(seen)[0]
    assert graphql["headers"]["x-shopify-access-token"] == GRANTED_TOKEN


async def test_load_config_reads_the_client_credential_names():
    config = load_config(
        {
            "MERCHANT_SHOPIFY_STORE_DOMAIN": "somewhere.myshopify.com",
            "MERCHANT_SHOPIFY_API_VERSION": "2026-07",
            "MERCHANT_SHOPIFY_CLIENT_ID": CLIENT_ID,
            "MERCHANT_SHOPIFY_CLIENT_SECRET": CLIENT_SECRET,
            "MERCHANT_LINKAGE_SECRET": "shared",
            "MERCHANT_RESOLVE_BASE_URL": "http://resolve.test",
        }
    )
    assert config.client_id == CLIENT_ID
    assert config.client_secret is not None and config.client_secret.configured
    assert config.credential_mode() == "client_credentials"


def test_neither_credential_path_configured_is_a_clear_refusal_naming_both():
    from merchant_channel.config import ChannelConfigError

    with pytest.raises(ChannelConfigError) as refused:
        ShopifyAdminGateway(make_config(access_token=Secret("")))
    message = str(refused.value)
    assert "MERCHANT_SHOPIFY_ACCESS_TOKEN" in message
    assert "MERCHANT_SHOPIFY_CLIENT_ID" in message
    assert "MERCHANT_SHOPIFY_CLIENT_SECRET" in message


# ------------------------------------------------------------------------- cache and refresh
async def test_requests_within_expiry_perform_exactly_one_grant():
    client, seen = recording_client(grant_then_graphql)
    gateway = ShopifyAdminGateway(client_config(), client=client)

    await gateway.read_back("RESOLVE-order_t-000000000000")
    await gateway.read_back("RESOLVE-order_t-000000000000")

    assert len(grants_in(seen)) == 1
    assert len(graphqls_in(seen)) == 2


async def test_a_token_is_refreshed_once_it_is_inside_the_safety_margin():
    now = {"t": 1000.0}
    client, seen = recording_client(
        lambda entry, seen: (
            _grant(entry, seen, expires_in=90)
            if entry["url"] == TOKEN_URL
            else graphql_ok(entry, seen)
        )
    )
    provider = MerchantTokenProvider(client_config(), client=client, clock=lambda: now["t"])

    assert await provider.token() == GRANTED_TOKEN
    now["t"] += 29.0  # a 90-second token with a 60-second margin is trusted for 30
    assert await provider.token() == GRANTED_TOKEN
    assert len(grants_in(seen)) == 1
    now["t"] += 2.0  # 31 seconds in: inside the margin, so the next call refreshes
    assert await provider.token() == GRANTED_TOKEN
    assert len(grants_in(seen)) == 2


async def test_concurrent_callers_trigger_exactly_one_grant():
    client, seen = recording_client(grant_then_graphql)
    provider = MerchantTokenProvider(client_config(), client=client)

    tokens = await asyncio.gather(provider.token(), provider.token(), provider.token())

    assert tokens == [GRANTED_TOKEN, GRANTED_TOKEN, GRANTED_TOKEN]
    assert len(grants_in(seen)) == 1


# --------------------------------------------------------------------------- grant failures
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
async def test_a_refused_grant_is_a_definite_failure_that_names_no_secret(status):
    client, _seen = recording_client(
        lambda entry, seen: httpx.Response(status, json={"error": "invalid_client"})
    )
    gateway = ShopifyAdminGateway(client_config(), client=client)

    with pytest.raises(GatewayDefiniteFailure) as refused:
        await gateway.read_back("RESOLVE-order_t-000000000000")

    message = str(refused.value)
    assert CLIENT_SECRET not in message
    assert CLIENT_ID not in message
    assert f"HTTP {status}" in message
    assert "Nothing was created" in message


async def test_a_grant_timeout_is_definite_and_never_an_unknown_outcome():
    client, _seen = recording_client(
        lambda entry, seen: (_ for _ in ()).throw(httpx.ConnectTimeout("no answer"))
    )
    gateway = ShopifyAdminGateway(client_config(), client=client)

    with pytest.raises(GatewayDefiniteFailure) as refused:
        await gateway.read_back("RESOLVE-order_t-000000000000")

    message = str(refused.value)
    assert "ConnectTimeout" in message
    assert CLIENT_SECRET not in message


async def test_a_grant_answer_without_a_token_is_a_definite_failure():
    client, _seen = recording_client(
        lambda entry, seen: httpx.Response(200, json={"unexpected": "shape"})
    )
    gateway = ShopifyAdminGateway(client_config(), client=client)

    with pytest.raises(GatewayDefiniteFailure):
        await gateway.read_back("RESOLVE-order_t-000000000000")


# ------------------------------------------------------------------------ 401 refresh/retry
def _four_oh_one_then_ok(entry, seen):
    if entry["url"] == TOKEN_URL:
        return grant_ok(entry, seen)
    if len(graphqls_in(seen)) == 1:
        return httpx.Response(401, json={})
    return graphql_ok(entry, seen)


def _always_401(entry, seen):
    if entry["url"] == TOKEN_URL:
        return grant_ok(entry, seen)
    return httpx.Response(401, json={})


@pytest.mark.parametrize(
    "make_cfg, expected_grants",
    [
        pytest.param(lambda: make_config(), 0, id="static"),
        pytest.param(lambda: client_config(), 2, id="client-credentials"),
    ],
)
async def test_a_single_401_refreshes_and_retries_the_same_request_exactly_once(
    make_cfg, expected_grants
):
    client, seen = recording_client(_four_oh_one_then_ok)
    gateway = ShopifyAdminGateway(make_cfg(), client=client)

    orders = await gateway.read_back("RESOLVE-order_t-000000000000")

    assert orders == []
    assert len(graphqls_in(seen)) == 2
    assert len(grants_in(seen)) == expected_grants


@pytest.mark.parametrize(
    "make_cfg, expected_grants",
    [
        pytest.param(lambda: make_config(), 0, id="static"),
        pytest.param(lambda: client_config(), 2, id="client-credentials"),
    ],
)
async def test_a_persistent_401_is_a_definite_failure_after_exactly_one_retry(
    make_cfg, expected_grants
):
    client, seen = recording_client(_always_401)
    gateway = ShopifyAdminGateway(make_cfg(), client=client)

    with pytest.raises(GatewayDefiniteFailure) as refused:
        await gateway.read_back("RESOLVE-order_t-000000000000")

    assert "401 twice" in str(refused.value)
    assert len(graphqls_in(seen)) == 2  # original + one retry, never a third
    assert len(grants_in(seen)) == expected_grants


# ------------------------------------------------------------------------------ secret safety
def test_the_client_secret_never_renders_in_any_repr():
    config = client_config()
    provider = MerchantTokenProvider(config)
    assert CLIENT_SECRET not in repr(provider)
    assert CLIENT_SECRET not in repr(config)
    assert CLIENT_SECRET not in str(config)
    assert "client_secret='set'" in repr(config)
    assert repr(provider).startswith("MerchantTokenProvider(mode=")


async def test_a_granted_token_is_masked_out_of_graphql_error_text():
    client, _seen = recording_client(
        lambda entry, seen: (
            grant_ok(entry, seen)
            if entry["url"] == TOKEN_URL
            else httpx.Response(200, json={"errors": [{"message": f"bad token {GRANTED_TOKEN}"}]})
        )
    )
    gateway = ShopifyAdminGateway(client_config(), client=client)

    with pytest.raises(GatewayDefiniteFailure) as refused:
        await gateway.read_back("RESOLVE-order_t-000000000000")

    assert GRANTED_TOKEN not in str(refused.value)
