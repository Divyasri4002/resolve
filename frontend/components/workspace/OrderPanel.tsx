"use client";

import type { ResolveOrder } from "@/lib/types";
import { Card } from "@/components/ui/Card";
import { formatMoney } from "@/lib/format";
import {
  AlertCircle,
  CheckCircle2,
  ClipboardCheck,
  FileText,
  Loader2,
  Package,
  ShieldCheck,
} from "lucide-react";

// Copy for the three states the server can report. "awaiting" says only that RESOLVE bought this
// from a catalog it can name and no pairing has arrived — never that a merchant order exists, and
// never when one will.
const MERCHANT_ORDER_COPY: Record<string, string> = {
  linked: "Paired with a merchant order this store confirmed",
  awaiting_linkage: "Payment verified — awaiting merchant order linkage",
  unlinked: "No merchant order pairing on record",
};

// Sprint M2: the server advances this ladder on order fetches (delivery_mode=demo_simulated).
// The stage lives on the order row; "delivered" can also arrive directly when the whole ladder
// collapses into one fetch.
const DELIVERY_STAGES = ["packed", "shipped", "out_for_delivery", "delivered"] as const;

function DeliveryStageStrip({ order }: { order: ResolveOrder }) {
  const current =
    order.status === "delivered" ? "delivered" : (order.delivery_stage ?? null);
  // Unknown stage strings land at -1 and render as "not being simulated" — never as progress.
  const currentIndex = current
    ? (DELIVERY_STAGES as readonly string[]).indexOf(current)
    : -1;

  return (
    <div className="mt-3.5 rounded-xl bg-neutral-sand/20 p-4">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/50">
          Delivery (demo simulated)
        </p>
        {order.status === "delivered" && order.delivered_at && (
          <span className="text-[10px] text-text-charcoal/45">
            {new Date(order.delivered_at).toLocaleString()}
          </span>
        )}
      </div>
      {currentIndex >= 0 ? (
        <div className="mt-2 flex items-center gap-1">
          {DELIVERY_STAGES.map((stage, i) => {
            const reached = i <= currentIndex;
            const isCurrent = i === currentIndex;
            return (
              <div key={stage} className="flex min-w-0 flex-1 items-center gap-1">
                <span
                  className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] font-semibold capitalize ${
                    isCurrent
                      ? "bg-status-info-soft text-status-info"
                      : reached
                        ? "bg-status-success-soft text-status-success"
                        : "border border-text-charcoal/10 bg-surface text-text-charcoal/40"
                  }`}
                >
                  {isCurrent && i < DELIVERY_STAGES.length - 1 && (
                    <Loader2 size={10} className="shrink-0 animate-spin" />
                  )}
                  {stage.replace(/_/g, " ")}
                </span>
                {i < DELIVERY_STAGES.length - 1 && (
                  <span
                    className={`h-px w-3 shrink-0 ${
                      i < currentIndex ? "bg-status-success/40" : "bg-text-charcoal/10"
                    }`}
                  />
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <p className="mt-1.5 text-[11px] text-text-charcoal/55">
          {order.status === "order_created"
            ? "Delivery simulation begins once the order record is confirmed."
            : "This order's delivery is not being simulated right now — no delivery state is claimed."}
        </p>
      )}
      <p className="mt-1.5 text-[10px] text-text-charcoal/45">
        Stages advance server-side on order fetches. No carrier or merchant shipment is claimed.
      </p>
    </div>
  );
}

interface OrderPanelProps {
  /** Persisted RESOLVE order record, if one has been created. */
  order: ResolveOrder | null;
  /** True while a verified payment exists but no order record has been created yet. */
  canCreate: boolean;
  isProcessing?: boolean;
  error?: string | null;
  onCreate: () => void;
  onConfirm: () => void;
}

export function OrderPanel({
  order,
  canCreate,
  isProcessing = false,
  error = null,
  onCreate,
  onConfirm,
}: OrderPanelProps) {
  const currency = order?.currency ?? null;
  const confirmed = order?.status === "order_confirmed";
  // "delivered" is the persisted demo transition (delivery_mode=demo_simulated); the delivered_at
  // fact stays on the row even after an issue moves the status to post-purchase.
  const delivered = order?.status === "delivered";
  const wasDelivered = !!order?.delivered_at;
  const postPurchase = order?.status === "awaiting_post_purchase_context";

  return (
    <Card className="p-5 shadow-soft border-accent-terracotta/25">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-text-charcoal/8 pb-4">
        <div className="flex items-center gap-2">
          <Package size={18} className="text-accent-terracotta" />
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/50">
              RESOLVE Order
            </p>
            <p className="font-display text-base text-text-charcoal">Order lifecycle record</p>
          </div>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-status-info-soft px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide text-status-info">
          <ShieldCheck size={11} className="shrink-0" />
          Razorpay TEST MODE
        </span>
      </div>

      <p className="mt-3.5 text-xs leading-relaxed text-text-charcoal/60">
        {order?.fulfillment_note ??
          "This is RESOLVE's own order record, created after the verified test payment. Payment verification is not merchant fulfillment."}
      </p>

      {order ? (
        <>
          {/* Lifecycle state */}
          <div
            className={`mt-3.5 flex items-start gap-2.5 rounded-xl border p-3.5 text-xs ${
              delivered || confirmed
                ? "border-status-success/25 bg-status-success-soft text-status-success"
                : postPurchase
                  ? "border-status-info/25 bg-status-info-soft text-status-info"
                  : "border-status-pending/25 bg-status-pending-soft text-status-pending"
            }`}
          >
            {delivered ? (
              <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
            ) : postPurchase ? (
              <ClipboardCheck size={16} className="mt-0.5 shrink-0" />
            ) : confirmed ? (
              <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
            ) : (
              <FileText size={16} className="mt-0.5 shrink-0" />
            )}
            <div>
              <p className="font-semibold">
                {delivered
                  ? "Delivered (demo simulated)"
                  : postPurchase
                    ? "Awaiting post-purchase context"
                    : confirmed
                      ? "Order confirmed"
                      : "Order created"}
              </p>
              <p className="mt-0.5 opacity-90">
                {delivered
                  ? "RESOLVE persisted this delivery in the demo environment on your reload. It is simulated — not a carrier or merchant confirmation — and you can report a post-purchase issue below."
                  : postPurchase
                    ? `A post-purchase issue has been recorded against this RESOLVE order.${wasDelivered ? " The order was demo-delivered before the issue was reported." : ""} It will be reviewed through deterministic resolution steps — no refund, replacement, or merchant action has been initiated.`
                    : confirmed
                      ? "You confirmed this RESOLVE order record. You can report a post-purchase issue below — no merchant fulfillment is implied."
                      : "The persistent RESOLVE order record was created from the verified test payment. Review it and confirm below."}
              </p>
            </div>
          </div>

          {/* Sprint M2: server-advanced demo delivery stage, rendered only from the row */}
          <DeliveryStageStrip order={order} />

          {/* Canonical order detail — every value came from the server */}
          <div className="mt-3.5 space-y-2 rounded-xl bg-neutral-sand/20 p-4 text-sm">
            <div className="flex items-center justify-between gap-4">
              <span className="shrink-0 text-text-charcoal/60">RESOLVE order id</span>
              <span className="font-mono text-xs font-semibold text-text-charcoal">
                {order.order_id}
              </span>
            </div>
            {/* Server-derived only. The merchant-side identifier is never sent to the browser, so
                there is nothing here a client could copy back into a request. */}
            <div className="flex items-center justify-between gap-4">
              <span className="shrink-0 text-text-charcoal/60">Merchant order</span>
              <span className="text-right text-xs font-semibold text-text-charcoal">
                {MERCHANT_ORDER_COPY[order.merchant_order_linkage]}
              </span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="shrink-0 text-text-charcoal/60">Product</span>
              <span className="text-right font-semibold text-text-charcoal">
                {order.product.title}
              </span>
            </div>
            {order.product.merchant && (
              <div className="flex items-center justify-between gap-4">
                <span className="shrink-0 text-text-charcoal/60">Merchant</span>
                <span className="font-semibold text-text-charcoal">{order.product.merchant}</span>
              </div>
            )}
            <div className="flex items-center justify-between gap-4">
              <span className="shrink-0 text-text-charcoal/60">Amount paid (test)</span>
              <span className="font-bold text-accent-terracotta">
                {formatMoney(order.amount, currency)}
              </span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="shrink-0 text-text-charcoal/60">Currency</span>
              <span className="font-semibold text-text-charcoal">{currency}</span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="shrink-0 text-text-charcoal/60">Razorpay order id</span>
              <span className="font-mono text-xs font-semibold text-text-charcoal">
                {order.razorpay_order_id}
              </span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="shrink-0 text-text-charcoal/60">Razorpay payment id</span>
              <span className="font-mono text-xs font-semibold text-text-charcoal">
                {order.razorpay_payment_id}
              </span>
            </div>
            {order.created_at && (
              <div className="flex items-center justify-between gap-4">
                <span className="shrink-0 text-text-charcoal/60">Order record created</span>
                <span className="text-xs font-semibold text-text-charcoal">
                  {new Date(order.created_at).toLocaleString()}
                </span>
              </div>
            )}
            {order.delivered_at && (
              <div className="flex items-center justify-between gap-4">
                <span className="shrink-0 text-text-charcoal/60">Delivered (demo simulated)</span>
                <span className="text-xs font-semibold text-text-charcoal">
                  {new Date(order.delivered_at).toLocaleString()} · {order.delivery_mode}
                </span>
              </div>
            )}
          </div>
        </>
      ) : (
        canCreate && (
          <div className="mt-3.5 flex items-start gap-2.5 rounded-xl border border-status-info/25 bg-status-info-soft p-3.5 text-xs text-status-info">
            <FileText size={16} className="mt-0.5 shrink-0" />
            <div>
              <p className="font-semibold">Payment verified — create the order record</p>
              <p className="mt-0.5 opacity-90">
                The next step records RESOLVE&apos;s persistent order context for this verified test
                payment. It is created server-side from the persisted checkout state.
              </p>
            </div>
          </div>
        )
      )}

      {error && (
        <div className="mt-3.5 flex items-start gap-2 rounded-xl border border-status-failure/20 bg-status-failure-soft p-3 text-xs font-medium text-status-failure">
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          {error}
        </div>
      )}

      <div className="mt-4 flex items-center justify-between gap-3 border-t border-text-charcoal/8 pt-4">
        <span className="text-[11px] text-text-charcoal/50">
          {order
            ? "Record created & confirmed by the server — client supplies nothing"
            : "Order values are derived from the verified payment"}
        </span>
        {!order && canCreate && (
          <button
            onClick={onCreate}
            disabled={isProcessing}
            className="inline-flex items-center gap-2 rounded-xl bg-accent-terracotta px-5 py-2.5 text-xs font-semibold text-surface shadow-soft transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isProcessing ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                Creating order record…
              </>
            ) : (
              <>
                <FileText size={13} />
                Create Order Record
              </>
            )}
          </button>
        )}
        {order && order.status === "order_created" && (
          <button
            onClick={onConfirm}
            disabled={isProcessing}
            className="inline-flex items-center gap-2 rounded-xl bg-accent-terracotta px-5 py-2.5 text-xs font-semibold text-surface shadow-soft transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isProcessing ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                Confirming order…
              </>
            ) : (
              <>
                <ClipboardCheck size={13} />
                Confirm Order
              </>
            )}
          </button>
        )}
        {(confirmed || delivered) && (
          <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-status-success">
            <CheckCircle2 size={13} />
            {delivered ? "Demo journey complete" : "Lifecycle complete"}
          </span>
        )}
      </div>
    </Card>
  );
}
