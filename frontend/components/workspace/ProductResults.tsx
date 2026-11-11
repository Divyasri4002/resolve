"use client";

import type { ProductResult, VariantOption } from "@/lib/types";
import { formatMoney } from "@/lib/format";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ExternalLink,
  Loader2,
  MessageSquareQuote,
  PackageSearch,
  Star,
  Store,
} from "lucide-react";

function formatPrice(p: ProductResult): string {
  // The provider's own price string is shown as it came back; only a numeric fallback is formatted,
  // and then with the currency code the provider named.
  if (p.price) return p.price;
  if (p.extracted_price != null) return formatMoney(p.extracted_price, p.currency);
  return "Price on request";
}

/**
 * Shopify's own option values, grouped by the dimension Shopify named them under.
 * Nothing is synthesised here: a product with no `variant_clarification` renders no choices.
 */
function groupVariantOptions(options: VariantOption[]): { name: string; values: string[] }[] {
  const groups: { name: string; values: string[] }[] = [];
  for (const option of options) {
    const existing = groups.find((group) => group.name === option.name);
    if (existing) {
      if (!existing.values.includes(option.value)) existing.values.push(option.value);
    } else {
      groups.push({ name: option.name, values: [option.value] });
    }
  }
  return groups;
}

interface ProductResultsProps {
  products: ProductResult[];
  selectedProductId?: string | null;
  selectingProductId?: string | null;
  clarifyingProductId?: string | null;
  error?: string | null;
  onSelectProduct?: (product: ProductResult) => void;
  onClarifyVariant?: (product: ProductResult, answer: string) => void;
}

/**
 * Real product results discovered through the configured provider — SerpApi Google Shopping, or the
 * connected Shopify store — with Task 05 selection and Task 14C.7E variant clarification.
 *
 * Every field shown here comes from the grounded ProductResult payload returned by the backend:
 * titles, prices, merchants, ratings, availability and extensions are real provider data, never
 * LLM-invented. Clicking the image/title opens the merchant's product page; the Select button asks
 * the backend to persist this exact discovered product as the mission's selection.
 *
 * A Shopify result whose variant Shopify has not pinned down is deliberately NOT selectable: it
 * shows the question and only the option values Shopify itself published, and answers by sending
 * that value as text. The variant identifier is resolved by the backend from Shopify's catalog, so
 * nothing here could name a variant even if this file wanted to.
 */
export function ProductResults({
  products,
  selectedProductId = null,
  selectingProductId = null,
  clarifyingProductId = null,
  error = null,
  onSelectProduct,
  onClarifyVariant,
}: ProductResultsProps) {
  if (!products || products.length === 0) return null;

  return (
    <section>
      <div className="flex items-center gap-2">
        <PackageSearch size={14} className="shrink-0 text-accent-terracotta" />
        <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/50">
          Product Discovery Results
        </p>
        <span className="rounded-full bg-accent-terracotta/10 px-2 py-0.5 text-[10px] font-semibold text-accent-terracotta">
          {products.length} {products.length === 1 ? "match" : "matches"}
        </span>
      </div>

      <div className="mt-2.5 space-y-2.5">
        {products.map((p, i) => {
          const isSelected = selectedProductId != null && p.product_id === selectedProductId;
          const isSelecting = selectingProductId != null && p.product_id === selectingProductId;
          const isClarifying = clarifyingProductId != null && p.product_id === clarifyingProductId;
          const selectable = onSelectProduct != null;
          const needsVariant = p.requires_variant_clarification === true;
          const optionGroups = groupVariantOptions(p.variant_clarification?.options ?? []);

          return (
            <div
              key={p.product_url ?? p.product_id ?? `${p.title}-${i}`}
              className={`group flex items-start gap-3 rounded-xl border bg-neutral-sand/10 p-3 transition ${
                isSelected
                  ? "border-status-success/60 bg-status-success-soft/50"
                  : "border-text-charcoal/10 hover:border-accent-terracotta/40 hover:bg-neutral-sand/20"
              }`}
            >
              {p.product_url ? (
                <a
                  href={p.product_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`View ${p.title} on the merchant site`}
                  className="shrink-0"
                >
                  {p.image_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={p.image_url}
                      alt={p.title}
                      className="h-14 w-14 rounded-lg border border-text-charcoal/10 object-cover"
                    />
                  ) : (
                    <span className="flex h-14 w-14 items-center justify-center rounded-lg bg-neutral-sand/30 text-text-charcoal/30">
                      <PackageSearch size={20} />
                    </span>
                  )}
                </a>
              ) : p.image_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={p.image_url}
                  alt={p.title}
                  className="h-14 w-14 shrink-0 rounded-lg border border-text-charcoal/10 object-cover"
                />
              ) : (
                <span className="flex h-14 w-14 shrink-0 items-center justify-center rounded-lg bg-neutral-sand/30 text-text-charcoal/30">
                  <PackageSearch size={20} />
                </span>
              )}

              <div className="min-w-0 flex-1">
                {p.product_url ? (
                  <a
                    href={p.product_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="line-clamp-2 text-xs font-medium leading-snug text-text-charcoal hover:text-accent-terracotta hover:underline"
                  >
                    {p.title}
                  </a>
                ) : (
                  <p className="line-clamp-2 text-xs font-medium leading-snug text-text-charcoal">
                    {p.title}
                  </p>
                )}

                <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-sm font-bold text-accent-terracotta">{formatPrice(p)}</span>

                  {p.merchant && (
                    <span className="inline-flex items-center gap-1 text-[10px] font-medium text-text-charcoal/55">
                      <Store size={10} className="shrink-0 text-text-charcoal/40" />
                      {p.merchant}
                    </span>
                  )}

                  {p.rating != null && (
                    <span className="inline-flex items-center gap-1 text-[10px] font-medium text-text-charcoal/60">
                      <Star size={10} className="shrink-0 fill-amber-400 text-amber-400" />
                      {p.rating}
                      {p.review_count != null && (
                        <span className="text-text-charcoal/40">({p.review_count.toLocaleString()})</span>
                      )}
                    </span>
                  )}
                </div>

                {p.availability && (
                  // Only a Shopify-sourced row's availability is the store's own stock answer;
                  // legacy provider labels persisted on this field render without stock styling.
                  <p
                    className={`mt-0.5 text-[10px] font-medium ${
                      p.source === "shopify"
                        ? "text-status-success"
                        : "text-text-charcoal/55"
                    }`}
                  >
                    {p.availability}
                  </p>
                )}

                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  {p.extensions?.map((ext) => (
                    <span
                      key={ext}
                      className="rounded-full bg-accent-terracotta/10 px-2 py-0.5 text-[9px] font-semibold text-accent-terracotta"
                    >
                      {ext}
                    </span>
                  ))}
                  {p.source && p.source !== "google_shopping" && (
                    <span className="rounded bg-neutral-sand/40 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-text-charcoal/40">
                      {p.source.replace(/_/g, " ")}
                    </span>
                  )}
                </div>

                {needsVariant && (
                  <div className="mt-2 rounded-lg border border-accent-terracotta/20 bg-accent-terracotta/5 p-2">
                    <p className="text-[11px] font-medium leading-snug text-text-charcoal/80">
                      {p.variant_clarification?.question ??
                        "Shopify has not identified one variant for this product yet."}
                    </p>

                    {optionGroups.length > 0 ? (
                      <div className="mt-1.5 space-y-1.5">
                        {optionGroups.map((group) => (
                          <div key={group.name} className="flex flex-wrap items-center gap-1.5">
                            <span className="text-[9px] font-semibold uppercase tracking-wide text-text-charcoal/45">
                              {group.name}
                            </span>
                            {group.values.map((value) => (
                              <button
                                key={`${group.name}-${value}`}
                                onClick={() => onClarifyVariant?.(p, value)}
                                disabled={!onClarifyVariant || isClarifying}
                                title={`Ask the store for the ${group.name.toLowerCase()} ${value}`}
                                className="inline-flex items-center gap-1 rounded-full border border-accent-terracotta/40 bg-surface px-2 py-0.5 text-[10px] font-semibold text-accent-terracotta transition hover:bg-accent-terracotta hover:text-surface disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-surface disabled:hover:text-accent-terracotta"
                              >
                                {isClarifying ? (
                                  <Loader2 size={10} className="shrink-0 animate-spin" />
                                ) : null}
                                {value}
                              </button>
                            ))}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="mt-1 text-[10px] text-text-charcoal/50">
                        Shopify published no available option for this product.
                      </p>
                    )}
                  </div>
                )}
              </div>

              <div className="flex shrink-0 flex-col items-end gap-1.5">
                {p.product_url && (
                  <a
                    href={p.product_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`Open ${p.title} on the merchant site`}
                    className="text-text-charcoal/30 transition hover:text-accent-terracotta"
                  >
                    <ExternalLink size={13} />
                  </a>
                )}

                {needsVariant ? (
                  <span
                    title="Shopify has not identified one variant yet, so nothing can be selected"
                    className="inline-flex items-center gap-1 rounded-full border border-text-charcoal/15 px-2.5 py-1 text-[10px] font-semibold text-text-charcoal/45"
                  >
                    <MessageSquareQuote size={11} className="shrink-0" />
                    Choose a variant
                  </span>
                ) : (
                  selectable &&
                  (isSelected ? (
                    <span className="inline-flex items-center gap-1 rounded-full bg-status-success-soft px-2.5 py-1 text-[10px] font-semibold text-status-success">
                      <CheckCircle2 size={11} className="shrink-0" />
                      Selected
                    </span>
                  ) : (
                    <button
                      onClick={() => onSelectProduct(p)}
                      disabled={isSelecting || !p.product_id}
                      title={p.product_id ? undefined : "This product cannot be selected"}
                      className="inline-flex items-center gap-1 rounded-full border border-accent-terracotta/40 px-2.5 py-1 text-[10px] font-semibold text-accent-terracotta transition hover:bg-accent-terracotta hover:text-surface disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-accent-terracotta"
                    >
                      {isSelecting ? (
                        <Loader2 size={11} className="shrink-0 animate-spin" />
                      ) : (
                        <Check size={11} className="shrink-0" />
                      )}
                      {isSelecting ? "Selecting..." : "Select"}
                    </button>
                  ))
                )}
              </div>
            </div>
          );
        })}
      </div>

      {error && (
        <div className="mt-2.5 flex items-center gap-2 rounded-xl border border-status-failure/20 bg-status-failure-soft p-2.5 text-[11px] font-medium text-status-failure">
          <AlertCircle size={13} className="shrink-0" />
          {error}
        </div>
      )}
    </section>
  );
}
