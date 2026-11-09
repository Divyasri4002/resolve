"""
Normalized product discovery models for RESOLVE.

These Pydantic models represent verified product results returned by real e-commerce data
providers (SerpApi Google Shopping, or the connected Shopify store). They normalize heterogeneous
provider outputs into a clean, structured schema used throughout the buyer reasoning flow.

The fields split into two kinds, and the difference is not cosmetic:

  DISPLAY / COMMERCIAL DATA — `title`, `price`, `extracted_price`, `currency`, `merchant`,
      `product_url`, `image_url`, `rating`, `review_count`, `availability`, `extensions`.
      These describe what a result looks like and what it costs where it was found. None of them
      identifies anything: two different stores can sell things with the same title at the same
      number, and this project has measured exactly that (a 410 Google-Shopping listing and a 410
      USD Shopify line for the same-branded product). Nothing may be matched on them. `availability`
      is only ever the provider's own stock answer, never a delivery or tag label — those live in
      `extensions` as provider metadata.

  AUTHORITATIVE IDENTITY — `product_id` (the id the SELECTION flow is keyed by, always
      provider-native and always resolved server-side from a persisted result), and for
      Shopify-sourced results the three explicit slots below. A Shopify GID is the only value in
      this model that names an object in a merchant's system, so it is carried in a field named
      for what it is rather than smuggled through `product_id` under a different meaning.
"""
from __future__ import annotations

from decimal import Decimal
from pydantic import BaseModel, Field

# The `source` value identifying a result that came from the connected Shopify catalog. Selection
# and ordering treat it as the switch that says "the three Shopify identity slots below are
# populated and authoritative"; any other value means they are absent, and must stay absent.
SHOPIFY_SOURCE = "shopify"

# The one switch the canonical product context needs: which `source` values carry an
# `availability` that is the provider's OWN stock answer, safe to show a reasoning model as
# availability. Written here — where the source vocabulary lives — so the agent-context layer can
# ask the question without naming any merchant vendor in its own code.
STOCK_REPORTING_SOURCES = frozenset({SHOPIFY_SOURCE})


def source_reports_stock(source: str | None) -> bool:
    """Whether this result's `availability` is the provider's own stock answer."""
    return source in STOCK_REPORTING_SOURCES


class VariantOption(BaseModel):
    """
    One Shopify `selectedOptions` pair, copied verbatim (Task 14C.7E).

    This is Shopify's own naming of one variant's dimension — `Size: 360ml`, `Color: Black`. It is
    the only thing a clarification may offer a buyer, because it is the only thing RESOLVE can put
    in front of them that Shopify itself used to distinguish the variants. Nothing here is derived
    from a title, a price, or a guess.
    """

    name: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)


class VariantClarification(BaseModel):
    """
    What RESOLVE has to ask, built only from Shopify's answer (Task 14C.7E).

    Carried on a result whose `requires_variant_clarification` is True. It names the option
    dimension(s) Shopify itself uses to separate the still-valid variants, the `(name, value)` pairs
    Shopify published for the variants that are actually available, and a deterministic question.
    Empty `options` means Shopify reported no available variant at all — there is nothing to ask
    and nothing to select.
    """

    question: str = Field(..., min_length=1)
    option_names: list[str] = Field(default_factory=list)
    options: list[VariantOption] = Field(default_factory=list)


class ProductResult(BaseModel):
    """
    Normalized product representation from real product search providers.
    All fields reflect real provider data; optional fields default to None if missing.
    """
    product_id: str | None = Field(
        default=None,
        description=(
            "Provider-native product/item identifier when the provider supplies one. For a "
            "SerpApi result without one this is the response's `position` — a per-search ordinal "
            "kept for selection compatibility, NOT a stable identity; it is valid only within "
            "the mission's own discovered list."
        ),
    )
    title: str = Field(..., description="Product title/name")
    price: str | None = Field(default=None, description="Raw formatted price string (e.g. '₹549', '₹1,999')")
    extracted_price: Decimal | None = Field(default=None, description="Numeric parsed price value in specified currency")
    currency: str = Field(default="INR", description="Currency code (e.g. 'INR')")
    merchant: str | None = Field(default=None, description="Merchant, seller, or store name (e.g. 'Amazon.in', 'Nykaa')")
    product_url: str | None = Field(default=None, description="Direct URL or purchase link to the product")
    image_url: str | None = Field(default=None, description="Product thumbnail or image URL")
    rating: float | None = Field(default=None, ge=0, le=5, description="Product rating out of 5 if available")
    review_count: int | None = Field(default=None, ge=0, description="Total number of reviews if available")
    availability: str | None = Field(
        default=None,
        description=(
            "Stock/availability status ONLY when the provider itself reports one (Shopify's "
            "availableForSale does; SerpApi Google Shopping does not). A delivery or tag label is "
            "provider metadata carried in `extensions`, never availability."
        ),
    )
    source: str = Field(default="google_shopping", description="Data source engine/provider name")
    extensions: list[str] = Field(default_factory=list, description="Additional product badges or highlights (e.g. 'Free shipping')")

    # ------------------------------------------------------------------ Shopify identity (Task 14C.7D)
    #
    # Populated ONLY by the Shopify catalog provider, from Shopify's own answer. They are never
    # derived from a title, a price, a merchant name or a timestamp, and never accepted from a
    # client: a SerpApi result leaves all three None forever, because nothing in a Google Shopping
    # listing entitles RESOLVE to claim it knows a Shopify object.
    shopify_product_gid: str | None = Field(default=None, description="Authoritative Shopify Product GID, Shopify-sourced results only")
    shopify_variant_gid: str | None = Field(default=None, description="Authoritative Shopify ProductVariant GID for the one variant this result represents")
    shopify_store_domain: str | None = Field(default=None, description="The connected Shopify store these GIDs belong to; identity is meaningless without it")
    # Set when Shopify itself does not answer which variant is meant. It travels with the result so
    # the ambiguity cannot be silently resolved downstream — a row that needs clarification is
    # discoverable but not selectable.
    requires_variant_clarification: bool = Field(default=False, description="True when no single authoritative variant could be determined; never a guess")
    # The Shopify-named options a buyer would have to choose between, present exactly when
    # `requires_variant_clarification` is True. Built from Shopify's own `selectedOptions`, so the
    # question RESOLVE asks can only ever offer values the merchant's catalog actually contains.
    variant_clarification: VariantClarification | None = Field(default=None, description="Shopify's own option values still in play; Shopify-sourced results only")


# The shapes Shopify itself issues. Written out here rather than imported from
# `app.tools.shopify` so the discovery model does not depend on the integration package, and
# checked instead of trusted everywhere identity is handed on.
SHOPIFY_PRODUCT_GID_PREFIX = "gid://shopify/Product/"
SHOPIFY_VARIANT_GID_PREFIX = "gid://shopify/ProductVariant/"


def _gid_tail_is_canonical(value: str | None, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix) and value[len(prefix) :].isdigit()


def shopify_identity_problem(result: ProductResult) -> str | None:
    """
    Why this result may NOT be carried forward as Shopify identity, or None if it may.

    Only Shopify-sourced results are judged. A Google Shopping result is expected to have no
    Shopify identity at all, and one that grew a GID it was never given is the confusion this
    function exists to catch.
    """
    if result.source != SHOPIFY_SOURCE:
        stray = [
            name
            for name in (
                "shopify_product_gid",
                "shopify_variant_gid",
                "shopify_store_domain",
                "variant_clarification",
            )
            if getattr(result, name) is not None
        ]
        if stray:
            return (
                f"a non-Shopify result carries Shopify identity fields ({', '.join(stray)}), "
                "which nothing has verified"
            )
        return None
    if not _gid_tail_is_canonical(result.shopify_product_gid, SHOPIFY_PRODUCT_GID_PREFIX):
        return "its Shopify Product GID is missing or is not a canonical Shopify Product global id"
    if not isinstance(result.shopify_store_domain, str) or not result.shopify_store_domain.strip():
        return "the Shopify store it belongs to is not recorded"
    # Checked before the Variant GID because "Shopify named no single variant" is the accurate
    # statement for an ambiguous result, where a plain "missing GID" would blame the reader for a
    # choice only the merchant can make.
    if result.requires_variant_clarification:
        return "no single Shopify variant has been identified for it"
    if not _gid_tail_is_canonical(result.shopify_variant_gid, SHOPIFY_VARIANT_GID_PREFIX):
        return "its Shopify Variant GID is missing or is not a canonical Shopify variant global id"
    return None



