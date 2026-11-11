"use client";

import type { CheckoutSummary } from "@/lib/types";
import { Card } from "@/components/ui/Card";
import { formatMoney } from "@/lib/format";
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  CreditCard,
  Loader2,
  Lock,
  PackageSearch,
  Receipt,
  Store,
} from "lucide-react";

function formatProvider(provider: string): string {
  return provider
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

interface CheckoutReviewProps {
  /**
   * Server-derived checkout summary (Task 06). Every value in here was built by the
   * backend from the mission's persisted selected_product — the client never supplies
   * authoritative price/product data.
   */
  checkout: CheckoutSummary;
  /** True once the user has explicitly confirmed (mission is checkout_confirmed). */
  confirmed: boolean;
  confirmedAt?: string | null;
  isConfirming?: boolean;
  error?: string | null;
  onBack: () => void;
  onConfirm: () => void;
}

export function CheckoutReview({
  checkout,
  confirmed,
  confirmedAt = null,
  isConfirming = false,
  error = null,
  onBack,
  onConfirm,
}: CheckoutReviewProps) {
  const product = checkout.product;

  return (
    <Card className="p-5 shadow-soft border-accent-terracotta/25">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-text-charcoal/8 pb-4">
        <div className="flex items-center gap-2">
          <Receipt size={18} className="text-accent-terracotta" />
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/50">
              Checkout Review
            </p>
            <p className="font-display text-base text-text-charcoal">
              Confirm your order details
            </p>
          </div>
        </div>
        <span className="inline-flex items-center gap-1 rounded-full bg-neutral-sand/40 px-2.5 py-1 text-[10px] font-semibold text-text-charcoal/60">
          <Lock size={10} className="shrink-0" />
          No payment at this step
        </span>
      </div>

      {confirmed && (
        <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-status-success/25 bg-status-success-soft p-3.5 text-xs text-status-success">
          <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-status-success" />
          <div>
            <p className="font-semibold text-status-success">Checkout confirmed. Ready for payment.</p>
            <p className="mt-0.5 text-status-success/90">
              You explicitly confirmed these details
              {confirmedAt
                ? ` on ${new Date(confirmedAt).toLocaleString()}`
                : ""}
              . You can now proceed to payment in the Razorpay TEST MODE panel below — the
              amount and order are created by the server.
            </p>
          </div>
        </div>
      )}

      {/* Selected product */}
      <div className="mt-4 flex items-start gap-3.5">
        {product.image_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={product.image_url}
            alt={product.title}
            className="h-16 w-16 shrink-0 rounded-xl border border-text-charcoal/10 object-cover"
          />
        ) : (
          <span className="flex h-16 w-16 shrink-0 items-center justify-center rounded-xl bg-neutral-sand/30 text-text-charcoal/30">
            <PackageSearch size={22} />
          </span>
        )}

        <div className="min-w-0 flex-1">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-text-charcoal/50">
            Product
          </p>
          {product.product_url ? (
            <a
              href={product.product_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-0.5 block text-sm font-semibold leading-snug text-text-charcoal hover:text-accent-terracotta hover:underline"
            >
              {product.title}
            </a>
          ) : (
            <p className="mt-0.5 text-sm font-semibold leading-snug text-text-charcoal">
              {product.title}
            </p>
          )}
          {product.merchant && (
            <p className="mt-1 inline-flex items-center gap-1 text-xs font-medium text-text-charcoal/60">
              <Store size={11} className="shrink-0 text-text-charcoal/40" />
              {product.merchant}
            </p>
          )}
        </div>
      </div>

      {/* Order lines — all values come from the server-built summary */}
      <div className="mt-4 space-y-2 rounded-xl bg-neutral-sand/20 p-4 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-text-charcoal/60">Price</span>
          <span className="font-semibold text-text-charcoal">
            {product.price ??
              (product.extracted_price != null
                ? formatMoney(product.extracted_price, checkout.currency)
                : "Price on request")}
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-text-charcoal/60">Quantity</span>
          <span className="font-semibold text-text-charcoal">{checkout.quantity}</span>
        </div>
        <div className="flex items-center justify-between border-t border-text-charcoal/10 pt-2">
          <span className="font-semibold text-text-charcoal">Subtotal</span>
          <span className="font-bold text-accent-terracotta">
            {formatMoney(checkout.subtotal, checkout.currency)}
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="inline-flex items-center gap-1.5 text-text-charcoal/60">
            <CreditCard size={13} className="text-text-charcoal/40" />
            Payment
          </span>
          <span className="font-semibold text-text-charcoal">
            {formatProvider(checkout.payment_provider)}
          </span>
        </div>
        {checkout.payment_note && (
          <p className="pt-1 text-[11px] leading-snug text-text-charcoal/50">
            {checkout.payment_note}
          </p>
        )}
      </div>

      {error && (
        <div className="mt-3.5 flex items-start gap-2 rounded-xl border border-status-failure/20 bg-status-failure-soft p-3 text-xs font-medium text-status-failure">
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          {error}
        </div>
      )}

      <div className="mt-4 flex items-center justify-between gap-3 border-t border-text-charcoal/8 pt-4">
        <button
          onClick={onBack}
          disabled={isConfirming}
          className="inline-flex items-center gap-1.5 rounded-xl border border-text-charcoal/15 px-4 py-2.5 text-xs font-semibold text-text-charcoal/70 transition hover:bg-neutral-sand/20 hover:text-text-charcoal disabled:opacity-50"
        >
          <ArrowLeft size={13} />
          Back to Product
        </button>

        {confirmed ? (
          <span className="inline-flex items-center gap-1.5 rounded-xl bg-status-success-soft px-4 py-2.5 text-xs font-semibold text-status-success">
            <CheckCircle2 size={13} />
            Confirmed
          </span>
        ) : (
          <button
            onClick={onConfirm}
            disabled={isConfirming}
            className="inline-flex items-center gap-2 rounded-xl bg-accent-terracotta px-5 py-2.5 text-xs font-semibold text-surface shadow-soft transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isConfirming ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                Confirming...
              </>
            ) : (
              <>
                <Lock size={12} />
                Confirm &amp; Continue
              </>
            )}
          </button>
        )}
      </div>
    </Card>
  );
}
