"""
Base interfaces and exceptions for merchant return providers.

A return provider is the capability that actually asks a merchant to accept goods back — the
label/RMA side of a return, not the money. Razorpay is not one (it moves money), and no code here
may imply that a merchant accepted a return when none was contacted.

The package is named ``returns`` rather than ``return`` because ``return`` is a reserved word in
Python and cannot be imported as a module name.

The contract mirrors the replacement provider on purpose: request a return, then ask what the
provider now says about it. Nothing may report an approved return, a shipped label, or a
delivered parcel that it was not explicitly told about.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ReturnError(Exception):
    """Base exception for all return provider operations."""

    def __init__(self, message: str, status_code: int = 500, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class ReturnProviderUnavailable(ReturnError):
    """
    Raised when no merchant return provider is connected (or the connected one is unreachable).
    An explicit absence of capability — never a silently successful return.
    """

    def __init__(
        self,
        message: str = (
            "No merchant return provider is connected, so no return could be requested. RESOLVE "
            "recorded the intent only — nothing was asked of any merchant."
        ),
    ) -> None:
        super().__init__(message, status_code=503)


class ReturnProviderRejected(ReturnError):
    """Raised when a connected provider refuses the return request."""

    def __init__(self, message: str = "The return provider refused this request.") -> None:
        super().__init__(message, status_code=502)


class ReturnOutcomeUnknown(ReturnError):
    """
    Raised when a provider MAY have accepted the submission but RESOLVE got no trustworthy answer.

    This is deliberately NOT ``ReturnProviderUnavailable`` and NOT ``ReturnProviderRejected``:
    both of those mean "nothing happened", which is a fact we do not have here. The mutation may
    already exist on the merchant side, so the only safe next step is a READ-ONLY reconciliation —
    never a second blind submission.
    """

    def __init__(
        self,
        message: str = (
            "The return provider's answer was lost or could not be understood, so RESOLVE cannot "
            "tell whether the return was submitted. The outcome is recorded as unknown and will be "
            "read back rather than re-sent."
        ),
    ) -> None:
        super().__init__(message, status_code=503)


@dataclass(frozen=True)
class ReturnRequest:
    """
    Everything a provider needs, assembled by RESOLVE from canonical persisted records.

    No field is client- or model-supplied: the order and product references come from the
    persisted order row and its selected-product snapshot. ``provider_order_reference`` is the
    one field that points OUTSIDE RESOLVE, and it comes from a persisted server-side linkage —
    there is no request body that could carry it.

    ``provider_product_reference`` and ``provider_variant_reference`` (Task 14C.7E) are the
    merchant-side identity of the thing bought, copied from the persisted order row's own identity
    columns. They are OPTIONAL and stay None for anything RESOLVE did not buy through a merchant
    catalog it can name: an absent reference narrows nothing, which is what keeps an order discovered
    outside a merchant's own catalog from ever being aimed at a merchant line it has no proven
    connection to.
    """

    mission_id: str
    issue_id: str
    order_id: str
    return_id: str  # RESOLVE's own id, carried so a lost response can be adopted later
    product_id: str | None
    title: str
    merchant: str | None
    reason: str | None = None
    issue_type: str | None = None  # RESOLVE's canonical issue taxonomy, never free text
    provider_order_reference: str | None = None  # merchant-side order id, persisted server-side
    provider_product_reference: str | None = None  # merchant-side product id, persisted server-side
    provider_variant_reference: str | None = None  # merchant-side variant id, persisted server-side


@dataclass(frozen=True)
class ReturnTarget:
    """
    The merchant-side coordinates of the thing being returned, plus the quantity to return.

    A provider builds this from canonical data it was handed and its OWN reads of the merchant
    system. It is what makes "resolve the target, then claim it, then submit" possible: the
    submission context persisted BEFORE the mutation is exactly this object, so a later reader
    knows what was sent even if the answer never arrived. It is never constructed from client
    input, and a provider that cannot resolve one unambiguously must refuse rather than guess.
    """

    provider_order_reference: str
    provider_line_reference: str
    provider_quantity: int  # units to return
    provider_reason: str  # provider's own reason token, mapped deterministically
    line_quantity: int | None = None  # units the merchant actually shipped (the local ceiling)
    detail: str | None = None


@dataclass(frozen=True)
class ReturnSubmission:
    """
    One previously attempted submission, read back out of RESOLVE's own persisted claim.

    This is the input to reconciliation: everything RESOLVE knows it sent (or may have sent),
    with nothing that only a live provider response could supply. It carries no credential and no
    customer detail — order/line references, a quantity, a reason token and a timestamp.
    """

    return_id: str
    issue_id: str
    provider_order_reference: str | None
    provider_line_reference: str | None
    provider_quantity: int | None
    provider_reason: str | None
    submitted_at: object | None = None  # datetime, or None when the claim predates this field


@dataclass(frozen=True)
class ReturnReconciliation:
    """
    What a read-only reconciliation concluded about a submission whose answer was lost.

    matched    : exactly one merchant-side return corresponds to the submission; its reference
                 and its own status are reported
    no_match   : nothing was found that can safely be identified as this submission — which is
                 NOT proof that no return exists
    ambiguous  : more than one candidate, so picking one would be a guess
    """

    state: str
    provider_reference: str | None = None
    outcome: str | None = None  # the provider's own word, same vocabulary as ReturnStatusInfo
    detail: str | None = None

    @classmethod
    def matched(cls, provider_reference: str, outcome: str, detail: str | None = None) -> "ReturnReconciliation":
        return cls(state="matched", provider_reference=provider_reference, outcome=outcome, detail=detail)

    @classmethod
    def no_match(cls, detail: str) -> "ReturnReconciliation":
        return cls(state="no_match", detail=detail)

    @classmethod
    def ambiguous(cls, detail: str) -> "ReturnReconciliation":
        return cls(state="ambiguous", detail=detail)


@dataclass(frozen=True)
class ReturnAck:
    """A provider's answer to a request. `accepted` means the provider opened a return — it does
    not mean approved, labelled, shipped, or completed."""

    provider_reference: str
    accepted: bool


@dataclass(frozen=True)
class ReturnStatusInfo:
    """The provider's own current word on a previously requested return."""

    provider_reference: str
    outcome: str  # "accepted" | "confirmed" | "declined" | "cancelled" | "completed"
    detail: str | None = None


class ReturnProvider(ABC):
    """Abstract merchant return provider."""

    name: str = "abstract"

    async def resolve_return_target(self, request: ReturnRequest) -> ReturnTarget | None:
        """
        Resolve the merchant-side target a return must reference, WITHOUT submitting anything.

        Returning None means this provider needs no separate target step. A provider that does
        need one raises ``ReturnProviderUnavailable`` when the target cannot be resolved
        unambiguously — an unavailable answer here is what keeps a guessed order id, line id,
        quantity or reason from ever reaching a mutation.
        """
        return None

    @abstractmethod
    async def request_return(
        self, request: ReturnRequest, target: ReturnTarget | None = None
    ) -> ReturnAck:
        """Ask the merchant-side provider to open a return. Raises ReturnError."""

    @abstractmethod
    async def get_return_status(self, provider_reference: str) -> ReturnStatusInfo:
        """Read back what the provider now says about a previously requested return."""

    async def reconcile_return(self, submission: ReturnSubmission) -> ReturnReconciliation:
        """
        Decide, READ-ONLY, whether a lost submission actually landed.

        The default says so plainly: this provider cannot correlate a submission, so the outcome
        stays unknown. Doing anything else — re-submitting on that silence — is exactly what this
        seam exists to prevent.
        """
        return ReturnReconciliation.no_match(
            f"The '{self.name}' return provider cannot correlate a lost submission, so RESOLVE "
            "cannot tell whether the return exists. Nothing was re-submitted."
        )


__all__ = [
    "ReturnAck",
    "ReturnError",
    "ReturnOutcomeUnknown",
    "ReturnProvider",
    "ReturnProviderRejected",
    "ReturnProviderUnavailable",
    "ReturnReconciliation",
    "ReturnRequest",
    "ReturnStatusInfo",
    "ReturnSubmission",
    "ReturnTarget",
]
