"""
Mock product search provider for tests and development without external credentials.
"""
from __future__ import annotations

from decimal import Decimal
from app.models.product import ProductResult
from app.tools.product_search.base import ProductSearchProvider


class MockProductSearchProvider(ProductSearchProvider):
    """
    Mock provider for isolated unit tests and offline testing.
    """

    def __init__(self, sample_results: list[ProductResult] | None = None) -> None:
        self.sample_results = sample_results if sample_results is not None else []
        self.last_query: str | None = None
        self.last_call_params: dict | None = None
        self.search_calls: list[dict] = []

    async def search(
        self,
        query: str,
        *,
        max_price: Decimal | None = None,
        currency: str = "INR",
        preferences: list[str] | None = None,
        brand: str | None = None,
        size_or_variant: str | None = None,
        limit: int = 10,
    ) -> list[ProductResult]:
        self.last_query = query
        self.last_call_params = {
            "query": query,
            "max_price": max_price,
            "currency": currency,
            "preferences": preferences,
            "brand": brand,
            "size_or_variant": size_or_variant,
            "limit": limit,
        }
        self.search_calls.append(dict(self.last_call_params))

        results = self.sample_results
        if max_price is not None:
            results = [
                r for r in results
                if r.extracted_price is None or r.extracted_price <= max_price
            ]

        return results[:limit]

