"""
Base interfaces and exceptions for merchant replacement (fulfilment) providers.

A replacement provider is the capability that actually asks a merchant to send another unit of
an ordered product. Razorpay is NOT one: it moves money, it does not fulfil orders, and no code
here may use it to imply that a merchant accepted anything.

The contract is deliberately narrow — ask for a replacement, then ask what happened to that
request. Nothing may report a shipment or delivery it was not explicitly told about.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


class ReplacementError(Exception):
    """Base exception for all replacement provider operations."""

    def __init__(self, message: str, status_code: int = 500, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class ReplacementProviderUnavailable(ReplacementError):
    """
    Raised when no merchant replacement provider is connected (or the connected one cannot be
    reached). This is an explicit absence of capability — never a silently successful request.
    """

    def __init__(
        self,
        message: str = (
            "No merchant replacement provider is connected, so no replacement order could be "
            "requested. RESOLVE recorded the intent only — nothing was ordered from any merchant."
        ),
    ) -> None:
        super().__init__(message, status_code=503)


class ReplacementProviderRejected(ReplacementError):
    """Raised when a connected provider refuses the replacement request."""

    def __init__(self, message: str = "The replacement provider refused this request.") -> None:
        super().__init__(message, status_code=502)


@dataclass(frozen=True)
class ReplacementRequest:
    """
    Everything a provider needs, assembled by RESOLVE from canonical persisted records.

    No field of this dataclass is client- or model-supplied: product identity comes from the
    persisted order/selected product, and the value is the canonical captured amount.
    """

    mission_id: str
    issue_id: str
    order_id: str
    replacement_id: str  # RESOLVE's own id, carried so a lost response can be adopted later
    product_id: str | None
    title: str
    merchant: str | None
    currency: str
    value: Decimal
    quantity: int = 1


@dataclass(frozen=True)
class ReplacementAck:
    """A provider's answer to a request. `accepted` means the provider created a replacement
    order — it does not mean shipped, delivered, or resolved."""

    provider_reference: str
    accepted: bool


@dataclass(frozen=True)
class ReplacementStatusInfo:
    """The provider's own current word on a previously requested replacement."""

    provider_reference: str
    outcome: str  # "accepted" | "declined" | "cancelled" | "completed" — normalized downstream
    detail: str | None = None


class ReplacementProvider(ABC):
    """Abstract merchant replacement provider."""

    name: str = "abstract"

    @abstractmethod
    async def request_replacement(self, request: ReplacementRequest) -> ReplacementAck:
        """Ask the merchant-side provider to create a replacement. Raises ReplacementError."""

    @abstractmethod
    async def get_replacement_status(self, provider_reference: str) -> ReplacementStatusInfo:
        """Read back what the provider now says about a previously created replacement."""


__all__ = [
    "ReplacementAck",
    "ReplacementError",
    "ReplacementProvider",
    "ReplacementProviderRejected",
    "ReplacementProviderUnavailable",
    "ReplacementRequest",
    "ReplacementStatusInfo",
]
