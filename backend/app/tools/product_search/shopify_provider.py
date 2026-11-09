"""
Shopify-native product discovery (Task 14C.7D).

The connected store's own catalog, read through the one existing read-only Shopify client,
normalised into the same `ProductResult` shape every other provider produces. Selected with
`PRODUCT_SEARCH_PROVIDER=shopify`; nothing about SerpApi's behaviour changes.

Why this exists is an identity question, not a catalog question. A Google Shopping listing tells
RESOLVE what something costs somewhere; it does not identify an object in a merchant's system, and
a merchant-side return can only ever be filed against such an object. Shopify answers with its own
global ids, so a result from this provider carries — from the first read — the two identifiers the
rest of the flow will need:

    product_id             = the Shopify Product GID, and the key selection is matched on
    shopify_product_gid    = the same Product GID, named for what it is
    shopify_variant_gid    = exactly one Shopify ProductVariant GID
    shopify_store_domain   = the store that issued both

The first is the selection handle; the other three are the authoritative identity that must never
be lost or re-derived. They are populated from Shopify's response and nowhere else: no title,
price, merchant name or timestamp is used to decide which object a result is, and a SerpApi result
never grows a Shopify identifier it was not given.

What this module deliberately does NOT do: it creates nothing. It reads the catalog and stops. It
holds no order, no draft order, no return, and no merchant-side identifier of any kind beyond what
Shopify itself published as its catalog.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from app.models.product import (
    SHOPIFY_SOURCE,
    ProductResult,
    VariantClarification,
    VariantOption,
    shopify_identity_problem,
)
from app.tools.product_search.base import ProductSearchError, ProductSearchProvider
from app.tools.shopify.errors import ShopifyError
from app.tools.shopify.models import ShopifyCatalog, ShopifyCatalogProduct, ShopifyCatalogVariant

logger = logging.getLogger(__name__)

# A result whose price cannot be compared to the buyer's stated budget says so, rather than being
# quietly kept in (or quietly dropped from) the list on the strength of two incomparable numbers.
CURRENCY_NOTE = "budget not applied: the store prices in {store} and the budget was stated in {want}"
# Same honesty for the case where the store published no currency at all: the budget is not applied,
# and the result says why instead of quietly deciding the item is inside or outside it.
NO_STORE_CURRENCY_NOTE = "budget not applied: the store published no currency to compare it against"


class ShopifyProductSearchProvider(ProductSearchProvider):
    """
    Reads one Shopify store's catalog. READ ONLY — this provider sends queries and nothing else.

    The client is injected: this is the same credential path the return provider and the linkage
    verification use, so a deployment configures Shopify once, not per feature.
    """

    name = SHOPIFY_SOURCE

    def __init__(
        self,
        client,
        *,
        max_products: int = 25,
        max_variants_per_product: int = 25,
    ) -> None:
        self._client = client
        self.max_products = max_products
        self.max_variants_per_product = max_variants_per_product

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
        Search the store's catalog for one intent, and return at most one result per product.

        A product whose variants Shopify has not let RESOLVE pin down still appears — flagged as
        needing clarification — because hiding it would leave the buyer choosing blind, while
        picking a variant for them would be the invention this whole task exists to avoid.
        """
        terms = _search_terms(
            query, brand=brand, preferences=preferences, size_or_variant=size_or_variant
        )
        catalog = await self._read_catalog(terms)

        results: list[ProductResult] = []
        for product in catalog.products:
            result = _to_product_result(
                product,
                catalog,
                max_price=max_price,
                requested_variant=size_or_variant,
                preferences=preferences or [],
                wanted_currency=currency,
            )
            if result is None:
                continue
            results.append(result)
            if len(results) >= max(1, limit):
                break
        logger.info(
            "Shopify catalog discovery returned %d result(s) for %r (needs clarification: %d)",
            len(results),
            terms,
            sum(1 for r in results if r.requires_variant_clarification),
        )
        return results

    async def _read_catalog(self, terms: str) -> ShopifyCatalog:
        """
        One catalog read, with Shopify's errors normalised onto this layer's error model.

        An unreachable or unconfigured store is a failure RESOLVE reports; it is never dressed up
        as an empty catalog, because "the merchant has nothing" and "the merchant could not be
        asked" lead to different decisions downstream.
        """
        try:
            return await self._client.read_catalog(
                search_query=terms or None,
                first=self.max_products,
                variants_per_product=self.max_variants_per_product,
            )
        except ShopifyError as exc:
            raise ProductSearchError(exc.message, status_code=exc.status_code) from exc
        except Exception as exc:  # an unexpected failure is reported, never mistaken for no results
            raise ProductSearchError(
                f"Shopify product discovery failed: {type(exc).__name__}", status_code=502
            ) from exc


def _search_terms(
    query: str,
    *,
    brand: str | None,
    preferences: list[str] | None,
    size_or_variant: str | None,
) -> str:
    """
    Build Shopify's free-text `query:` argument from the structured intent.

    Order-stable and duplicate-free so the same intent always asks the same question. Nothing here
    is product-category specific: it is the buyer's own words and constraints, joined.
    """
    tokens: list[str] = []
    for value in (brand, query, size_or_variant, *(preferences or [])):
        if not isinstance(value, str) or not value.strip():
            continue
        tokens.append(value.strip())
    joined = " ".join(tokens)
    seen: list[str] = []
    for token in joined.split():
        if token.lower() not in {already.lower() for already in seen}:
            seen.append(token)
    return " ".join(seen)


def _matching_tokens(variant: ShopifyCatalogVariant, wanted: list[str]) -> set[str]:
    """
    Which requested terms this variant's own Shopify-supplied attributes name.

    Compared against `selectedOptions` values and the SKU the merchant published — i.e. against
    fields that identify the variant — and never against the product title or the price, which
    describe the product and would turn matching into guessing.
    """
    haystack = [value for _, value in variant.selected_options]
    haystack += [name for name, _ in variant.selected_options]
    if variant.sku:
        haystack.append(variant.sku)
    lowered = {entry.strip().lower() for entry in haystack if entry and entry.strip()}
    return {
        term.strip().lower()
        for term in wanted
        if isinstance(term, str) and term.strip() and term.strip().lower() in lowered
    }


@dataclass(frozen=True)
class VariantSelection:
    """
    The outcome of Shopify's own variant data on one product.

    `variant` is set only when exactly one available variant is defensible. When it is not,
    `candidates` still holds every variant Shopify reported as available and
    `undecided_option_names` holds the Shopify option dimensions those candidates disagree on —
    which is what makes the ambiguity askable instead of guessable (Task 14C.7E).
    """

    variant: ShopifyCatalogVariant | None = None
    problem: str | None = None
    candidates: tuple[ShopifyCatalogVariant, ...] = ()
    undecided_option_names: tuple[str, ...] = ()


def _undecided_option_names(variants: tuple[ShopifyCatalogVariant, ...]) -> tuple[str, ...]:
    """
    Which Shopify option dimensions still distinguish the candidates.

    Computed from `selectedOptions` alone — the dimension names and values Shopify itself published
    — never from titles or prices. A dimension every candidate shares answers nothing, so it is not
    asked about; if none differs (Shopify offering two variants that read identically) every
    dimension is named rather than RESOLVE silently preferring one.
    """
    if len(variants) < 2:
        return ()
    ordered: list[str] = []
    for variant in variants:
        for name, _value in variant.selected_options:
            if name not in ordered:
                ordered.append(name)
    differing = tuple(
        name
        for name in ordered
        if len({value for v in variants for n, value in v.selected_options if n == name}) > 1
    )
    return differing or tuple(ordered)


def _clarification(selection: VariantSelection) -> VariantClarification | None:
    """
    The question RESOLVE may ask, built only from what Shopify named.

    Returning None means there is nothing to clarify: a variant WAS determined. Every value here is
    a `selectedOptions` pair copied from Shopify's answer for a variant Shopify says is available,
    so a buyer can never be offered an option the catalog does not contain.
    """
    if selection.variant is not None:
        return None
    options: list[VariantOption] = []
    seen: set[tuple[str, str]] = set()
    for variant in selection.candidates:
        for name, value in variant.selected_options:
            if (name, value) not in seen:
                seen.add((name, value))
                options.append(VariantOption(name=name, value=value))
    if not selection.candidates:
        question = (
            "Shopify reports no available variant of this product, so there is nothing to "
            "choose from it yet."
        )
    elif selection.undecided_option_names:
        question = (
            f"Which {' or '.join(selection.undecided_option_names)} do you want? "
            "Only the options Shopify publishes for this product can be chosen."
        )
    else:
        question = (
            "Which of this product's Shopify variants do you want? "
            "Only the options Shopify publishes for this product can be chosen."
        )
    return VariantClarification(
        question=question,
        option_names=list(selection.undecided_option_names),
        options=options,
    )


def _select_variant(
    product: ShopifyCatalogProduct, *, requested_variant: str | None, preferences: list[str]
) -> VariantSelection:
    """
    The one variant this intent means, or none — with the reason it could not be determined.

    Shopify's `availableForSale` decides what is even a candidate; an unavailable variant is never
    chosen, however well it matches. When the buyer asked for something specific and no available
    variant answers to it, that is reported as unresolved rather than traded for the closest
    thing: a silent substitution is how an order ends up describing an item nobody selected.

    Refusal wording is load-bearing — it is what the buyer reads and what the tests pin — so the
    three sentences below are the whole vocabulary of "Shopify did not let me decide this".
    """
    available = tuple(variant for variant in product.variants if variant.available_for_sale)
    if not available:
        return VariantSelection(
            problem="no Shopify variant of this product is currently available"
        )

    wanted = [
        term
        for term in ([requested_variant] if requested_variant else []) + list(preferences)
        if isinstance(term, str) and term.strip()
    ]
    if wanted:
        scored = [(len(_matching_tokens(variant, wanted)), variant) for variant in available]
        best = max(score for score, _ in scored)
        if best > 0:
            winners = tuple(variant for score, variant in scored if score == best)
            if len(winners) == 1:
                return VariantSelection(variant=winners[0])
            return VariantSelection(
                problem=(
                    f"{len(winners)} Shopify variants match the requested options equally, so "
                    "which one is meant is not determinable"
                ),
                candidates=available,
                undecided_option_names=_undecided_option_names(winners),
            )
        return VariantSelection(
            problem=(
                "no available Shopify variant matches the requested variant, and nothing is "
                "substituted for it"
            ),
            # The available variants are still shown as options: choosing one of them would be the
            # buyer's decision, made knowingly, not RESOLVE trading it for something else.
            candidates=available,
            undecided_option_names=_undecided_option_names(available),
        )

    if len(available) == 1:
        return VariantSelection(variant=available[0])
    return VariantSelection(
        problem=(
            f"{len(available)} Shopify variants are available and nothing in the request selects "
            "one, so no variant is chosen arbitrarily"
        ),
        candidates=available,
        undecided_option_names=_undecided_option_names(available),
    )


def _money(value: Decimal | None, currency: str | None) -> str | None:
    if value is None:
        return None
    amount = f"{value:.2f}"
    return f"{currency} {amount}" if currency else amount


def _to_product_result(
    product: ShopifyCatalogProduct,
    catalog: ShopifyCatalog,
    *,
    max_price: Decimal | None,
    requested_variant: str | None,
    preferences: list[str],
    wanted_currency: str | None,
) -> ProductResult | None:
    """
    Normalise one Shopify product into a `ProductResult`.

    Returns None only when Shopify gave RESOLVE nothing to represent (no title at all). Every other
    shortcoming — no available variant, several plausible variants, a price the buyer's budget
    cannot be compared against — produces a result that says so, because an absent answer and an
    unusable answer are different facts and the flow must be able to tell them apart.
    """
    if not product.title.strip():
        return None

    selection = _select_variant(
        product, requested_variant=requested_variant, preferences=preferences
    )
    chosen = selection.variant
    problem = selection.problem
    store_currency = catalog.currency
    price = chosen.price if chosen is not None else None

    if max_price is not None and price is not None and store_currency == wanted_currency:
        if price > max_price:
            return None

    extensions: list[str] = []
    if chosen is not None and chosen.selected_options:
        extensions.extend(f"{name}: {value}" for name, value in chosen.selected_options)
    if problem is not None:
        extensions.append(f"variant not resolved by Shopify: {problem}")
    if (
        max_price is not None
        and store_currency
        and wanted_currency
        and store_currency != wanted_currency
    ):
        extensions.append(
            CURRENCY_NOTE.format(store=store_currency, want=wanted_currency)
        )
    elif max_price is not None and not store_currency:
        extensions.append(NO_STORE_CURRENCY_NOTE)

    result = ProductResult(
        # For a Shopify result the selection handle IS the Shopify Product GID — Shopify's own
        # string, copied, never assembled. The dedicated slots below carry the same authority
        # explicitly, so nothing downstream has to infer a source from a string's shape.
        product_id=product.product_id,
        title=product.title,
        price=_money(price, store_currency),
        extracted_price=price,
        currency=store_currency or wanted_currency or "INR",
        # Display context only. The store's identity is the domain, and that is what is authoritative.
        merchant=catalog.shop_name or catalog.store_domain,
        product_url=None,
        image_url=product.variants[0].image_url if product.variants else None,
        rating=None,
        review_count=None,
        # Availability is Shopify's answer about the product, not a consequence of RESOLVE failing
        # to pick a variant: an ambiguous product is still in stock.
        availability=(
            "In stock"
            if any(variant.available_for_sale for variant in product.variants)
            else "Out of stock"
        ),
        source=SHOPIFY_SOURCE,
        extensions=extensions,
        shopify_product_gid=product.product_id,
        shopify_variant_gid=chosen.variant_id if chosen is not None else None,
        shopify_store_domain=catalog.store_domain,
        requires_variant_clarification=chosen is None,
        variant_clarification=_clarification(selection),
    )
    # Never hand on an identity this provider could not vouch for: if the normalisation above ever
    # drifts, the result is dropped rather than passed downstream with a half-trusted GID.
    problem_with_identity = shopify_identity_problem(result)
    if problem_with_identity is not None and not result.requires_variant_clarification:
        logger.warning(
            "Dropping a Shopify catalog result whose identity is incomplete: %s",
            problem_with_identity,
        )
        return None
    return result


__all__ = ["ShopifyProductSearchProvider"]
