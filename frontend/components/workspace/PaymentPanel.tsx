"use client";

import type { CheckoutSummary, MissionStatus, ShoppingPayment } from "@/lib/types";
import { Card } from "@/components/ui/Card";
import { formatMoney } from "@/lib/format";
import {
  AlertCircle,
  CheckCircle2,
  CreditCard,
  Loader2,
  Lock,
  ShieldCheck,
  XCircle,
} from "lucide-react";

interface PaymentPanelProps {
  /** Persisted Razorpay TEST-MODE payment record, if an order has been created. */
  payment: ShoppingPayment | null;
  missionStatus: MissionStatus;
  /** Server-derived checkout summary used only for display. */
  checkout: CheckoutSummary | null;
  isProcessing?: boolean;
  error?: string | null;
  onProceed: () => void;
}

export function PaymentPanel({
  payment,
  missionStatus,
  checkout,
  isProcessing = false,
  error = null,
  onProceed,
}: PaymentPanelProps) {
  const status = payment?.status ?? null;
  // Both are server fields; no payment record means no currency is claimed either.
  const currency = payment?.currency ?? checkout?.currency ?? null;
  const amount = payment?.amount ?? checkout?.subtotal ?? null;

  return (
    <Card className="p-5 shadow-soft border-accent-terracotta/25">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-text-charcoal/8 pb-4">
        <div className="flex items-center gap-2">
          <CreditCard size={18} className="text-accent-terracotta" />
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/50">
              Payment
            </p>
            <p className="font-display text-base text-text-charcoal">Razorpay test checkout</p>
          </div>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-status-info-soft px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide text-status-info">
          <ShieldCheck size={11} className="shrink-0" />
          Razorpay TEST MODE
        </span>
      </div>

      <p className="mt-3.5 text-xs leading-relaxed text-text-charcoal/60">
        This checkout runs in Razorpay TEST MODE only. Use Razorpay&apos;s documented test
        credentials — no real money is charged and no real order is placed with any merchant.
      </p>

      {/* Amount + order lines (display-only; every value came from the server) */}
      <div className="mt-3.5 space-y-2 rounded-xl bg-neutral-sand/20 p-4 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-text-charcoal/60">Amount payable</span>
          <span className="font-bold text-accent-terracotta">{formatMoney(amount, currency)}</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-text-charcoal/60">Currency</span>
          <span className="font-semibold text-text-charcoal">{currency ?? "—"}</span>
        </div>
        {payment?.razorpay_order_id && (
          <div className="flex items-center justify-between">
            <span className="text-text-charcoal/60">Razorpay order id</span>
            <span className="font-mono text-xs font-semibold text-text-charcoal">
              {payment.razorpay_order_id}
            </span>
          </div>
        )}
        {payment?.razorpay_payment_id && (
          <div className="flex items-center justify-between">
            <span className="text-text-charcoal/60">Razorpay payment id</span>
            <span className="font-mono text-xs font-semibold text-text-charcoal">
              {payment.razorpay_payment_id}
            </span>
          </div>
        )}
      </div>

      {status === "verified" ? (
        <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-status-success/25 bg-status-success-soft p-3.5 text-xs text-status-success">
          <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-status-success" />
          <div>
            <p className="font-semibold text-status-success">Payment verified — TEST MODE</p>
            <p className="mt-0.5 text-status-success/90">
              Razorpay&apos;s payment signature was verified by the RESOLVE backend with
              HMAC-SHA256
              {payment?.verified_at
                ? ` on ${new Date(payment.verified_at).toLocaleString()}`
                : ""}
              . This was a test-mode transaction — no real money moved.
            </p>
            <p className="mt-1 text-status-success/90">
              {missionStatus === "payment_verified"
                ? "Next, server-side: RESOLVE creates its permanent order record for this purchase."
                : "RESOLVE's permanent order record for this purchase has been created server-side."}
            </p>
          </div>
        </div>
      ) : status === "failed" ? (
        <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-status-failure/25 bg-status-failure-soft p-3.5 text-xs text-status-failure">
          <XCircle size={16} className="shrink-0 mt-0.5" />
          <div>
            <p className="font-semibold">Test payment failed</p>
            <p className="mt-0.5 opacity-90">
              {payment?.failure_reason === "signature_verification_failed"
                ? "The payment response failed backend signature verification and was NOT marked as paid."
                : payment?.failure_reason ?? "The test payment did not complete."}
            </p>
          </div>
        </div>
      ) : status === "pending" ? (
        <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-status-pending/25 bg-status-pending-soft p-3.5 text-xs text-status-pending">
          <Loader2 size={16} className="shrink-0 animate-spin mt-0.5" />
          <div>
            <p className="font-semibold text-status-pending">Test payment in progress</p>
            <p className="mt-0.5 text-status-pending/90">
              A Razorpay TEST order has been created. Complete the payment in the Razorpay
              window — the mission is marked verified only after the backend checks Razorpay&apos;s
              signature.
            </p>
          </div>
        </div>
      ) : null}

      {error && (
        <div className="mt-3.5 flex items-start gap-2 rounded-xl border border-status-failure/20 bg-status-failure-soft p-3 text-xs font-medium text-status-failure">
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          {error}
        </div>
      )}

      {/* The payment record is the authority on whether payment actions make sense: a
          verified Razorpay payment never offers payment CTAs again, whatever the mission
          status — only null/pending/failed records may start or resume checkout. */}
      {payment?.status !== "verified" && (
        <div className="mt-4 flex items-center justify-between gap-3 border-t border-text-charcoal/8 pt-4">
          <span className="inline-flex items-center gap-1.5 text-[11px] text-text-charcoal/50">
            <Lock size={11} className="shrink-0" />
            Amount &amp; order id are created by the server
          </span>
          {status === "pending" ? (
            // The pending order is reused server-side (idempotent), so resuming just
            // reopens Razorpay Checkout for the same order — never a duplicate order.
            <button
              onClick={onProceed}
              disabled={isProcessing}
              className="inline-flex items-center gap-2 rounded-xl bg-status-pending px-5 py-2.5 text-xs font-semibold text-surface shadow-soft transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isProcessing ? (
                <>
                  <Loader2 size={13} className="animate-spin" />
                  Opening test checkout…
                </>
              ) : (
                <>
                  <CreditCard size={13} />
                  Resume Test Payment
                </>
              )}
            </button>
          ) : (
            <button
              onClick={onProceed}
              disabled={isProcessing}
              className="inline-flex items-center gap-2 rounded-xl bg-accent-terracotta px-5 py-2.5 text-xs font-semibold text-surface shadow-soft transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isProcessing ? (
                <>
                  <Loader2 size={13} className="animate-spin" />
                  Opening test checkout…
                </>
              ) : status === "failed" ? (
                <>
                  <CreditCard size={13} />
                  Retry Test Payment
                </>
              ) : (
                <>
                  <CreditCard size={13} />
                  Proceed to Payment
                </>
              )}
            </button>
          )}
        </div>
      )}
    </Card>
  );
}
