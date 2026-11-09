"""
Base interfaces and exceptions for product search providers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from app.models.product import ProductResult


class ProductSearchError(Exception):
    """Base exception for all product search operations."""
    def __init__(self, message: str, status_code: int = 500, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class MissingApiKeyError(ProductSearchError):
    """Raised when the provider API key is not configured."""
    def __init__(self, message: str = "Product search API key is not configured in backend environment.") -> None:
        super().__init__(message, status_code=503)


class ProviderRateLimitError(ProductSearchError):
    """Raised when the external provider returns a rate limit (HTTP 429)."""
    def __init__(self, message: str = "Product search rate limit reached. Please retry shortly.") -> None:
        super().__init__(message, status_code=429)


class ProductSearchProvider(ABC):
    """
    Abstract interface for e-commerce product search providers.
    All implementations return normalized ProductResult objects.
    """

    @abstractmethod
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
        Execute a search against a real product catalog and return normalized results.

        :param query: Base search query or product category (e.g. 'shampoo', 'wireless earbuds')
        :param max_price: Optional maximum price threshold in specified currency
        :param currency: Currency code, defaults to 'INR'
        :param preferences: Extracted feature preferences (e.g. ['anti-hairfall', 'noise-cancelling'])
        :param brand: Optional target brand filter
        :param size_or_variant: Optional size, volume, or variant specification
        :param limit: Maximum number of normalized items to return
        :return: List of normalized ProductResult instances
        """
        pass

