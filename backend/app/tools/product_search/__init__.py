"""
Product search provider package.
"""
from __future__ import annotations

from app.config import get_settings
from app.tools.product_search.base import (
    MissingApiKeyError,
    ProductSearchError,
    ProductSearchProvider,
    ProviderRateLimitError,
)
from app.tools.product_search.mock_provider import MockProductSearchProvider
from app.tools.product_search.serpapi_provider import SerpApiProductSearchProvider


def get_product_search_provider(
    provider_name: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ProductSearchProvider:
    """
    Factory to retrieve the configured ProductSearchProvider instance.

    SerpApi stays the default. Shopify is opt-in via PRODUCT_SEARCH_PROVIDER=shopify, and because
    its client is built inside that branch a deployment selecting SerpApi never constructs or
    authenticates a Shopify client, and never needs Shopify credentials to exist.
    """
    settings = get_settings()
    provider = (provider_name or settings.product_search_provider or "serpapi").lower()
    key = api_key if api_key is not None else settings.serpapi_api_key
    url = base_url or settings.serpapi_base_url or "https://serpapi.com/search"

    if provider == "serpapi":
        return SerpApiProductSearchProvider(api_key=key, base_url=url)
    elif provider == "mock":
        return MockProductSearchProvider()
    elif provider == "shopify":
        from app.tools.shopify import get_shopify_client
        from app.tools.product_search.shopify_provider import ShopifyProductSearchProvider

        return ShopifyProductSearchProvider(client=get_shopify_client(settings))
    else:
        raise ValueError(
            f"Unknown product search provider: {provider!r}. Use 'serpapi', 'shopify' or 'mock'."
        )


__all__ = [
    "ProductSearchProvider",
    "SerpApiProductSearchProvider",
    "MockProductSearchProvider",
    "ProductSearchError",
    "MissingApiKeyError",
    "ProviderRateLimitError",
    "get_product_search_provider",
]

