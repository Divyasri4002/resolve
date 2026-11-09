"""
Tests for Task 04A: SerpApi Google Shopping Provider Abstraction.

Covers:
- Provider configuration & factory resolution
- Query construction without redundant repetitions
- SerpApi request parameter building
- Normalization of real-world Google Shopping response payload into ProductResult
- Missing API key safety check
- HTTP error & rate limit handling
- Budget filtering
"""
import pytest
from decimal import Decimal
import httpx

from app.models.product import ProductResult
from app.tools.product_search.base import (
    MissingApiKeyError,
    ProductSearchError,
    ProviderRateLimitError,
)
from app.tools.product_search.serpapi_provider import SerpApiProductSearchProvider
from app.tools.product_search import get_product_search_provider


SAMPLE_SERPAPI_RESPONSE = {
    "search_metadata": {
        "id": "search_12345",
        "status": "Success",
    },
    "shopping_results": [
        {
            "position": 1,
            "title": "L'Oreal Paris Total Repair 5 Shampoo, 650ml",
            "link": "https://www.google.com/url?q=https://example.com/loreal-shampoo",
            "product_link": "https://example.com/loreal-shampoo",
            "product_id": "item_98765",
            "source": "Amazon.in",
            "price": "₹549.00",
            "extracted_price": 549.0,
            "rating": 4.3,
            "reviews": 1250,
            "thumbnail": "https://serpapi.com/thumbnail/loreal.jpg",
            "delivery": "Free delivery by tomorrow",
            "extensions": ["Top rated", "Free shipping"],
        },
        {
            "position": 2,
            "title": "Dove Hair Fall Rescue Shampoo",
            "link": "https://example.com/dove-shampoo",
            "source": "Nykaa",
            "price": "₹399.00",
            "extracted_price": 399.0,
            "rating": 4.5,
            "reviews": 3400,
            "thumbnail": "https://serpapi.com/thumbnail/dove.jpg",
            "delivery": "In stock",
            "extensions": ["Sale"],
        },
        {
            "position": 3,
            "title": "Premium Organic Salon Care Shampoo 1000ml",
            "link": "https://example.com/premium-shampoo",
            "source": "Flipkart",
            "price": "₹1,200.00",
            "extracted_price": 1200.0,
            "rating": 4.1,
            "reviews": 89,
            "thumbnail": "https://serpapi.com/thumbnail/premium.jpg",
            "delivery": "Delivery in 2 days",
        },
        {
            "position": 4,
            # Malformed item missing title
            "source": "Unknown",
            "price": "₹100",
        },
    ],
}


def test_build_search_query():
    provider = SerpApiProductSearchProvider(api_key="test_dummy_key")

    # Basic query with brand and preference
    query = provider.build_search_query(
        query="shampoo",
        brand="Dove",
        preferences=["anti-hairfall"],
        size_or_variant="250ml",
    )
    assert "Dove" in query
    assert "shampoo" in query
    assert "anti hairfall" in query or "anti-hairfall" in query
    assert "250ml" in query

    # Avoid duplicate tokens if already in query
    query2 = provider.build_search_query(
        query="Sony wireless noise-cancelling earbuds",
        brand="Sony",
        preferences=["wireless", "noise-cancelling"],
    )
    assert query2.lower().count("sony") == 1
    assert "earbuds" in query2


def test_build_request_params():
    provider = SerpApiProductSearchProvider(api_key="secret_test_key_123")
    params = provider.build_request_params("wireless earbuds under 2000")

    assert params["engine"] == "google_shopping"
    assert params["q"] == "wireless earbuds under 2000"
    assert params["hl"] == "en"
    assert params["gl"] == "in"
    assert params["api_key"] == "secret_test_key_123"


def test_normalize_item():
    provider = SerpApiProductSearchProvider(api_key="test_dummy_key")
    raw = SAMPLE_SERPAPI_RESPONSE["shopping_results"][0]
    normalized = provider.normalize_item(raw, default_currency="INR")

    assert normalized is not None
    assert isinstance(normalized, ProductResult)
    assert normalized.title == "L'Oreal Paris Total Repair 5 Shampoo, 650ml"
    assert normalized.extracted_price == Decimal("549.0")
    assert normalized.price == "₹549.00"
    assert normalized.currency == "INR"
    assert normalized.merchant == "Amazon.in"
    assert normalized.rating == 4.3
    assert normalized.review_count == 1250
    assert normalized.image_url == "https://serpapi.com/thumbnail/loreal.jpg"
    assert normalized.product_url == "https://www.google.com/url?q=https://example.com/loreal-shampoo"
    assert normalized.source == "google_shopping"
    assert "Top rated" in normalized.extensions
    # A delivery label is provider metadata, never a stock claim (CORE-5 Phase 2): availability
    # stays absent because Google Shopping reports none, and the label lands in extensions.
    assert normalized.availability is None
    assert "Free delivery by tomorrow" in normalized.extensions


def test_normalize_item_missing_title():
    provider = SerpApiProductSearchProvider(api_key="test_dummy_key")
    raw = SAMPLE_SERPAPI_RESPONSE["shopping_results"][3]
    normalized = provider.normalize_item(raw)
    assert normalized is None


@pytest.mark.asyncio
async def test_search_missing_api_key():
    provider = SerpApiProductSearchProvider(api_key="")
    with pytest.raises(MissingApiKeyError) as exc_info:
        await provider.search("shampoo")
    assert "SERPAPI_API_KEY is empty" in str(exc_info.value)


@pytest.mark.asyncio
async def test_search_normalization_with_budget_filter(monkeypatch):
    provider = SerpApiProductSearchProvider(api_key="valid_test_key")

    async def mock_get(self, url, params=None, **kwargs):
        class MockResponse:
            status_code = 200
            def json(self):
                return SAMPLE_SERPAPI_RESPONSE
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    # Search with budget ceiling of 600 INR
    results = await provider.search("shampoo", max_price=Decimal("600"), limit=5)

    assert len(results) == 2  # Items 1 (549) and 2 (399) should be included; item 3 (1200) excluded
    assert results[0].extracted_price == Decimal("549.0")
    assert results[1].extracted_price == Decimal("399.0")


@pytest.mark.asyncio
async def test_search_rate_limit_error(monkeypatch):
    provider = SerpApiProductSearchProvider(api_key="valid_test_key")

    async def mock_get(self, url, params=None, **kwargs):
        class MockResponse:
            status_code = 429
            def json(self):
                return {"error": "Rate limit exceeded"}
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    with pytest.raises(ProviderRateLimitError):
        await provider.search("laptop")


@pytest.mark.asyncio
async def test_search_timeout_error(monkeypatch):
    provider = SerpApiProductSearchProvider(api_key="valid_test_key")

    async def mock_get(self, url, params=None, **kwargs):
        raise httpx.ReadTimeout("Connection timed out")

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    with pytest.raises(ProductSearchError) as exc_info:
        await provider.search("shoes")
    assert "timed out" in str(exc_info.value).lower()


def test_factory_get_product_search_provider():
    serpapi_p = get_product_search_provider("serpapi", api_key="abc")
    assert isinstance(serpapi_p, SerpApiProductSearchProvider)

    mock_p = get_product_search_provider("mock")
    assert mock_p is not None

    with pytest.raises(ValueError):
        get_product_search_provider("invalid_provider_name")

