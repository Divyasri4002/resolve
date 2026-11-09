"""
Controlled Product Discovery Tool for RESOLVE.

Provides a safe, application-level interface between reasoning agents and product search providers.
The LLM never directly calls external APIs, never sees API credentials, and only receives normalized,
safe ProductResult objects.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.models.product import ProductResult
from app.tools.product_search.base import (
    MissingApiKeyError,
    ProductSearchError,
    ProductSearchProvider,
    ProviderRateLimitError,
)
from app.tools.product_search import get_product_search_provider

logger = logging.getLogger(__name__)


class ProductDiscoveryTool:
    """
    Controlled discovery tool.
    Accepts structured search criteria from reasoning agents, executes search via the configured
    ProductSearchProvider (e.g. SerpApi Google Shopping), applies deterministic budget filtering,
    and returns sanitized ProductResult models.
    """

    def __init__(self, provider: ProductSearchProvider | None = None) -> None:
        self.provider = provider or get_product_search_provider()

    async def search(
        self,
        query: str,
        *,
        budget: Decimal | None = None,
        currency: str = "INR",
        preferences: list[str] | None = None,
        brand: str | None = None,
        size_or_variant: str | None = None,
        limit: int = 5,
    ) -> list[ProductResult]:
        """
        Execute product discovery through the underlying provider with parameter validation.
        """
        clean_query = query.strip() if query else ""
        if not clean_query:
            logger.warning("ProductDiscoveryTool received empty query.")
            return []

        # Ensure budget is Decimal if provided
        max_price: Decimal | None = None
        if budget is not None:
            try:
                max_price = Decimal(str(budget))
            except Exception:
                max_price = None

        logger.info(
            "Executing ProductDiscoveryTool search: query=%r, max_price=%s %s, limit=%d",
            clean_query,
            max_price,
            currency,
            limit,
        )

        try:
            results = await self.provider.search(
                query=clean_query,
                max_price=max_price,
                currency=currency,
                preferences=preferences or [],
                brand=brand,
                size_or_variant=size_or_variant,
                limit=limit,
            )
            logger.info("ProductDiscoveryTool returned %d normalized results", len(results))
            return results
        except (MissingApiKeyError, ProviderRateLimitError, ProductSearchError) as exc:
            logger.error("ProductDiscoveryTool error: %s", exc)
            raise
        except Exception as exc:
            logger.exception("Unexpected error in ProductDiscoveryTool: %s", exc)
            raise ProductSearchError(f"Product discovery failed: {exc}", status_code=500) from exc


def get_product_discovery_tool(provider: ProductSearchProvider | None = None) -> ProductDiscoveryTool:
    return ProductDiscoveryTool(provider=provider)

