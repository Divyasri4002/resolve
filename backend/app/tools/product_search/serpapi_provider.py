"""
SerpApi Google Shopping provider adapter for RESOLVE.

Queries the SerpApi Google Shopping engine (https://serpapi.com/search?engine=google_shopping)
and normalizes live search results into ProductResult models.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any
import httpx

from app.models.product import ProductResult
from app.tools.product_search.base import (
    MissingApiKeyError,
    ProductSearchError,
    ProductSearchProvider,
    ProviderRateLimitError,
)

logger = logging.getLogger(__name__)


class SerpApiProductSearchProvider(ProductSearchProvider):
    """
    Adapter for querying SerpApi's Google Shopping engine.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://serpapi.com/search",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.api_key = api_key.strip() if api_key else ""
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def build_search_query(
        self,
        query: str,
        *,
        brand: str | None = None,
        preferences: list[str] | None = None,
        size_or_variant: str | None = None,
    ) -> str:
        """
        Dynamically constructs an effective search string from shopping parameters.
        Avoids redundant repetitions.
        """
        tokens: list[str] = []
        lower_query = query.lower()

        # Add brand if specified and not already in query
        if brand and brand.lower() not in lower_query:
            tokens.append(brand)

        tokens.append(query)

        # Add specific variant or size if not already in query
        if size_or_variant and size_or_variant.lower() not in lower_query:
            tokens.append(size_or_variant)

        # Add relevant preferences (e.g. 'anti-hairfall', 'noise-cancelling')
        if preferences:
            for pref in preferences:
                pref_clean = pref.replace("-", " ")
                if pref.lower() not in lower_query and pref_clean.lower() not in lower_query:
                    tokens.append(pref)

        return " ".join(tokens).strip()

    def build_request_params(
        self,
        search_query: str,
        *,
        currency: str = "INR",
        gl: str = "in",
        hl: str = "en",
    ) -> dict[str, str]:
        """
        Builds query parameters for the SerpApi Google Shopping engine.
        """
        return {
            "engine": "google_shopping",
            "q": search_query,
            "hl": hl,
            "gl": gl,
            "api_key": self.api_key,
        }

    def normalize_item(self, item: dict[str, Any], default_currency: str = "INR") -> ProductResult | None:
        """
        Normalizes a single SerpApi shopping result dictionary into a ProductResult.
        Returns None if title is missing.
        """
        title = item.get("title")
        if not title or not isinstance(title, str):
            return None

        # Extract numeric price
        extracted_price: Decimal | None = None
        raw_price_val = item.get("extracted_price")
        if raw_price_val is not None:
            try:
                extracted_price = Decimal(str(raw_price_val))
            except Exception:
                extracted_price = None

        # Fallback price regex parse if extracted_price is absent
        raw_price_str = item.get("price")
        if extracted_price is None and raw_price_str and isinstance(raw_price_str, str):
            match = re.search(r"(\d+(?:,\d+)*(?:\.\d+)?)", raw_price_str.replace(",", ""))
            if match:
                try:
                    extracted_price = Decimal(match.group(1))
                except Exception:
                    extracted_price = None

        # Extract rating and review count
        rating: float | None = None
        if item.get("rating") is not None:
            try:
                rating = float(item["rating"])
            except (ValueError, TypeError):
                rating = None

        review_count: int | None = None
        if item.get("reviews") is not None:
            try:
                review_count = int(item["reviews"])
            except (ValueError, TypeError):
                review_count = None

        extensions_raw = item.get("extensions", [])
        extensions: list[str] = [str(e) for e in extensions_raw if isinstance(e, (str, int, float))]
        # `delivery` and `tag` are SerpApi's own labels ("Free delivery by tomorrow", "Used") —
        # provider metadata, not stock statements. They are preserved here and never mapped onto
        # `availability`, which stays None because this engine reports no stock status at all.
        for label in (item.get("delivery"), item.get("tag")):
            if isinstance(label, (str, int, float)):
                text = str(label).strip()
                if text and text not in extensions:
                    extensions.append(text)

        # When SerpApi sends no product_id, the response's `position` is kept for selection
        # compatibility. It is a per-search ordinal, NOT a stable identity: it is only meaningful
        # inside the mission's own discovered list, never across missions or searches.
        product_id = str(item.get("product_id") or item.get("position") or "") or None

        return ProductResult(
            product_id=product_id,
            title=title.strip(),
            price=str(raw_price_str) if raw_price_str else (f"₹{extracted_price}" if extracted_price else None),
            extracted_price=extracted_price,
            currency=default_currency,
            merchant=item.get("source"),
            product_url=item.get("link") or item.get("product_link"),
            image_url=item.get("thumbnail"),
            rating=rating,
            review_count=review_count,
            # Google Shopping supplies no stock field; absence is preserved as absence rather than
            # being manufactured out of a delivery label.
            availability=None,
            source="google_shopping",
            extensions=extensions,
        )

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
        """
        Executes Google Shopping search via SerpApi and normalizes the results.
        """
        if not self.api_key:
            raise MissingApiKeyError(
                "SERPAPI_API_KEY is empty. Please set SERPAPI_API_KEY in backend/.env to enable live product search."
            )

        if not query or not query.strip():
            return []

        search_query = self.build_search_query(
            query=query.strip(),
            brand=brand,
            preferences=preferences,
            size_or_variant=size_or_variant,
        )

        params = self.build_request_params(search_query=search_query, currency=currency)

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(self.base_url, params=params)

                if resp.status_code == 401 or resp.status_code == 403:
                    logger.error("SerpApi returned authentication error (HTTP %s)", resp.status_code)
                    raise ProductSearchError(
                        "Authentication with SerpApi product search failed. Please verify SERPAPI_API_KEY.",
                        status_code=resp.status_code,
                    )
                elif resp.status_code == 429:
                    logger.warning("SerpApi rate limit reached (HTTP 429)")
                    raise ProviderRateLimitError()
                elif resp.status_code >= 400:
                    logger.error("SerpApi returned error status HTTP %s", resp.status_code)
                    raise ProductSearchError(
                        f"Product search provider returned error status HTTP {resp.status_code}",
                        status_code=resp.status_code,
                    )

                data = resp.json()

        except httpx.TimeoutException as exc:
            logger.warning("SerpApi product search request timed out: %s", exc)
            raise ProductSearchError("Product search request timed out.", status_code=504) from exc
        except httpx.RequestError as exc:
            logger.error("Network error while connecting to SerpApi: %s", exc)
            raise ProductSearchError("Network error while connecting to product search provider.", status_code=502) from exc
        except (ValueError, TypeError) as exc:
            logger.error("Failed to decode SerpApi JSON response: %s", exc)
            raise ProductSearchError("Received malformed response from product search provider.", status_code=502) from exc

        # Check for provider-level error message in JSON
        if "error" in data:
            error_msg = str(data.get("error", "Unknown SerpApi error"))
            logger.warning("SerpApi payload contained error: %s", error_msg)
            raise ProductSearchError(f"Product provider error: {error_msg}", status_code=502)

        raw_results = data.get("shopping_results", [])
        if not isinstance(raw_results, list):
            return []

        normalized_results: list[ProductResult] = []
        for raw_item in raw_results:
            if not isinstance(raw_item, dict):
                continue
            normalized = self.normalize_item(raw_item, default_currency=currency)
            if normalized:
                # Apply budget filtering if max_price was requested
                if max_price is not None and normalized.extracted_price is not None:
                    if normalized.extracted_price > max_price:
                        continue
                normalized_results.append(normalized)

            if len(normalized_results) >= limit:
                break

        return normalized_results

