"""
Tests for Task 14C.6C: registering the Shopify return provider by configuration.

`RETURN_PROVIDER` now resolves to something other than "none". That is a capability being named,
not a return being made, and the line between the two is what this file holds:

  * "none" stays the default, and stays what a fresh deployment gets;
  * "shopify" resolves to the SAME provider class written in Task 14C.4 — registration added no
    second implementation, no second credential path, and no second client;
  * selecting the provider performs no network exchange at all, not even a token request;
  * unconfigured or invalid Shopify settings produce a refusal that names no secret, and never a
    partial success;
  * the furthest a configuration change can take anything on its own — resolving a target through
    the registered provider — still sends only queries, and the transport under test asserts on
    any mutation, so a slip fails loudly.

Hermetic by construction: the only Shopify transport in this file is httpx.MockTransport against
example.myshopify.com. Nothing here contacts the real store or creates anything anywhere.
"""
from __future__ import annotations

import httpx
import pytest

from app.tools.returns import get_return_provider
from app.tools.returns import unconnected as unconnected_pkg
from app.tools.returns.base import ReturnError
from app.tools.shopify.client import ShopifyReadOnlyClient
from app.tools.shopify.return_provider import ShopifyReturnProvider
from app.tools.shopify.return_write_client import ShopifyReturnWriteClient
from app.tools.shopify.sanitizer import reset_registered_secrets
from tests.test_shopify_return_provider import (  # noqa: F401 - existing helpers, reused not cloned
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    FULFILLMENT_LINE_GID,
    ORDER_GID,
    Recorded,
    make_clients,
    make_request,
    shipped,
    targets_data,
)


@pytest.fixture(autouse=True)
def _clean_secrets():
    reset_registered_secrets()
    yield
    reset_registered_secrets()


@pytest.fixture
def recorded() -> Recorded:
    return Recorded()


@pytest.fixture
def provider_on_recorded_transport(monkeypatch, recorded):
    """
    Stand in for `get_shopify_clients` ONLY to bolt the existing test transport onto the two
    capabilities the factory hands the provider — so every document the registered provider could
    send is recorded, and a mutation would raise instead of quietly succeeding.

    The credential path itself is unchanged: the same client classes, the same constructor, the same
    single client-credentials exchange they share. Patching the PAIR factory is what 14C.7O requires
    — patching only `get_shopify_client` would leave the return-write capability built for real, and
    a test would quietly talk to the merchant's actual store.
    """
    import app.tools.shopify as shopify_pkg

    def build(settings=None):  # noqa: ARG001 - the factory passes settings; this spy ignores them
        return make_clients(recorded, data=targets_data(shipped()), forbid_mutations=True)

    monkeypatch.setattr(shopify_pkg, "get_shopify_clients", build)
    return build


# --------------------------------------------------------------------------- #
# (a) the default stays 'none'
# --------------------------------------------------------------------------- #
def test_none_is_the_configured_default_and_selects_the_unconnected_provider():
    from app.config import get_settings

    assert get_settings().return_provider == "none"
    provider = get_return_provider("none")
    assert provider.name == "none"
    assert isinstance(provider, unconnected_pkg.UnconnectedReturnProvider)
    # Called bare, exactly as the executor calls it.
    assert get_return_provider().name == "none"


def test_any_other_name_is_still_refused_by_name():
    for name in ("razorpay", "Shopify-Live", "mock", "shopifyy", "gemini"):
        with pytest.raises(ValueError) as excinfo:
            get_return_provider(name)
        # The factory normalises case before matching, so it reports the normalised name.
        assert name.lower() in str(excinfo.value)


def test_an_empty_name_falls_back_to_the_default_instead_of_guessing():
    """
    '' means unset, and unset means 'none'. The factory must not treat a blank as a reason to
    reach for the only other thing it knows.
    """
    assert get_return_provider("").name == "none"
    assert get_return_provider(None).name == "none"


# --------------------------------------------------------------------------- #
# (b) + (e) 'shopify' resolves to the existing provider over the existing client
# --------------------------------------------------------------------------- #
def test_shopify_selection_returns_the_existing_provider_and_sends_nothing(
    provider_on_recorded_transport, recorded
):
    provider = get_return_provider("shopify")
    assert isinstance(provider, ShopifyReturnProvider)
    assert provider.name == "shopify"
    assert isinstance(provider._client, ShopifyReadOnlyClient)
    # 14C.7O: the write goes on its own capability, and that capability is the one this factory built
    # over the same transport — one credential, two capabilities, no second client implementation.
    assert isinstance(provider._write, ShopifyReturnWriteClient)
    assert provider._write._admin is provider._client._admin
    # No second implementation was introduced for registration.
    import app.tools.shopify.return_provider as return_provider_module

    assert type(provider) is return_provider_module.ShopifyReturnProvider
    # Selecting it performed no exchange — not even a token request.
    assert recorded.attempted is False


def test_the_factory_reads_the_configured_name_without_an_argument(
    provider_on_recorded_transport, monkeypatch, recorded
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "return_provider", "shopify")
    assert get_return_provider().name == "shopify"
    assert recorded.attempted is False
    monkeypatch.setattr(get_settings(), "return_provider", "none")
    assert get_return_provider().name == "none"
    assert recorded.attempted is False


def test_the_client_is_built_through_the_one_existing_factory_with_these_settings(monkeypatch):
    """
    (e) Proof there is no parallel OAuth path: the factory delegates, once, with Settings.

    14C.7O widened the spy to the pair factory, because that is now the one call the return provider
    is built from — and the spy returns a client and a capability, so 'once' still means once.
    """
    import app.tools.shopify as shopify_pkg
    from app.config import get_settings

    seen: list[object] = []

    def spy(settings=None):
        seen.append(settings)
        return make_clients(
            Recorded(),
            data={},
            http_status=599,
            raise_exc=httpx.ConnectError("no exchange is expected"),
        )

    monkeypatch.setattr(shopify_pkg, "get_shopify_clients", spy)
    provider = get_return_provider("shopify")
    assert isinstance(provider, ShopifyReturnProvider)
    assert seen == [get_settings()]


def test_shopify_package_does_not_re_export_the_return_provider():
    """Registration reaches the provider through the return factory, not the Shopify package."""
    import app.tools.shopify as shopify_pkg

    assert "ShopifyReturnProvider" not in shopify_pkg.__all__


# --------------------------------------------------------------------------- #
# (c) + (d) unconfigured and invalid configuration
# --------------------------------------------------------------------------- #
async def test_unconfigured_shopify_settings_refuse_without_a_byte_sent(monkeypatch):
    """(c) Missing configuration is a refusal, never a credential leak or a partial success."""
    import httpx as httpx_module

    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "shopify_store_domain", "")
    monkeypatch.setattr(settings, "shopify_client_id", "")
    monkeypatch.setattr(settings, "shopify_client_secret", "")

    def explode(*_args, **_kwargs):
        raise AssertionError("an unconfigured return provider attempted a network call")

    monkeypatch.setattr(httpx_module, "AsyncClient", explode)

    provider = get_return_provider("shopify")  # constructing is safe by design
    assert isinstance(provider, ShopifyReturnProvider)
    with pytest.raises(ReturnError) as excinfo:
        await provider.resolve_return_target(make_request(provider_order_reference=ORDER_GID))
    assert excinfo.value.status_code == 503
    message = excinfo.value.message
    # Naming which environment variable is missing is the point of this error; carrying a value is
    # not. So the check is on credential shapes and on every secret this process could know.
    for leaked in (CLIENT_SECRET, ACCESS_TOKEN, "shpat_", "shppa_", "Authorization", "Bearer "):
        assert leaked not in message, leaked
    assert "shpat_" not in message.lower() and "shppa_" not in message.lower()


async def test_invalid_shopify_settings_are_refused_and_stay_credential_free(monkeypatch):
    """(d) A nonsense host reads as unavailable. Nothing is created, nothing is disclosed."""
    from app.config import get_settings
    from app.tools.shopify.errors import ShopifyError

    settings = get_settings()
    monkeypatch.setattr(settings, "shopify_store_domain", "not a host")
    monkeypatch.setattr(settings, "shopify_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "shopify_client_secret", CLIENT_SECRET)

    provider = get_return_provider("shopify")
    with pytest.raises((ReturnError, ShopifyError)) as excinfo:
        await provider.resolve_return_target(make_request(provider_order_reference=ORDER_GID))
    assert CLIENT_SECRET not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# (g) the factory exposes no credential
# --------------------------------------------------------------------------- #
def test_a_registered_provider_leaks_no_credential_through_any_string_form():
    from app.config import get_settings

    secret = get_settings().shopify_client_secret
    provider = get_return_provider("shopify")
    forms = [
        repr(provider),
        str(provider),
        str(provider.__dict__),
        repr(provider._client),
        repr(provider._write),
        str(provider._write.__dict__),
    ]
    for text in forms:
        assert "shpat_" not in text
        assert "shppa_" not in text
        if secret:
            assert secret not in text  # never echo the form: it is the thing under test


# --------------------------------------------------------------------------- #
# (f) + STEP 16 tripwire: selecting the provider cannot cause a mutation
# --------------------------------------------------------------------------- #
async def test_merely_selecting_shopify_never_reaches_return_create(
    provider_on_recorded_transport, recorded
):
    """
    The furthest a configuration change gets anything on its own: the provider is registered, it is
    pointed at a linked order, and it resolves its target. Every document sent is a query,
    `returnCreate` appears nowhere, and the mock transport would have raised on any mutation.
    """
    provider = get_return_provider("shopify")
    assert recorded.attempted is False  # registration itself is inert

    target = await provider.resolve_return_target(
        make_request(provider_order_reference=ORDER_GID)
    )
    assert target.provider_line_reference == FULFILLMENT_LINE_GID
    assert recorded.attempted is True
    assert recorded.mutations == []
    for entry in recorded.graphql:
        assert not entry["query"].lstrip().startswith("mutation")
        assert "returnCreate" not in entry["query"]


def test_the_provider_still_names_one_mutation_document_and_no_other_write_operation():
    """
    Registration did not widen the write surface. The provider module still holds exactly one
    `mutation …` literal, and none of the other operations `write_returns` would permit.
    """
    import ast

    import app.tools.shopify.return_provider as return_provider_module

    with open(return_provider_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    literals = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert sum(1 for text in literals if text.lstrip().startswith("mutation ")) == 1
    for forbidden in (
        "fulfillmentCreate",
        "orderEdit",
        "refundCreate",
        "returnClose",
        "returnReopen",
        "returnProcess",
    ):
        assert not any(forbidden in text for text in literals), forbidden
