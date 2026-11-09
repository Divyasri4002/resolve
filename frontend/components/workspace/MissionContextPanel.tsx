"use client";

import type {
  CheckoutSummary,
  IntentReceipt,
  OrderIssue,
  ProductResult,
  ResolveOrder,
} from "@/lib/types";
import { Card } from "@/components/ui/Card";
import { StatusPill } from "@/components/ui/StatusPill";
import { formatMoney } from "@/lib/format";
import { AlertCircle, Shield, ShieldCheck } from "lucide-react";

/**
 * The remedy a buyer may hand to the agent, by the name the backend's mandate uses.
 *
 * `confirm_payment` is deliberately absent: paying is a human step in this deployment, and a switch
 * that is off here refuses the remedy whatever the mandate says — which is why the reason the server
 * returns is shown as it came back rather than paraphrased.
 */
const DELEGABLE_REMEDIES = [
  { action: "request_refund", label: "Refund" },
  { action: "request_replacement", label: "Replacement" },
  { action: "request_return", label: "Return" },
];

function humanize(value?: string | null): string {
  return value ? value.replace(/_/g, " ") : "—";
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-text-charcoal/8 px-5 py-3.5 first:border-t-0">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-text-charcoal/45">
        {title}
      </p>
      <div className="mt-2 space-y-1.5">{children}</div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  if (value == null || value === "") return null;
  return (
    <div className="flex items-start justify-between gap-3 text-[11px]">
      <span className="shrink-0 text-text-charcoal/55">{label}</span>
      <span className="text-right font-semibold text-text-charcoal">{value}</span>
    </div>
  );
}

/** A product line: only the fields the backend actually returned for it. */
function productLine(product: { title: string; merchant?: string | null }, suffix?: string) {
  return (
    <span className="text-right font-semibold text-text-charcoal">
      {product.title}
      {product.merchant ? ` — ${product.merchant}` : ""}
      {suffix ? ` (${suffix})` : ""}
    </span>
  );
}

/**
 * A product's price as the provider named it.
 *
 * `price` is the provider's own display string, so it is shown verbatim; only the numeric
 * `extracted_price` is formatted, and then with the currency code that came with it.
 */
function productPrice(product: ProductResult): string {
  if (product.price) return product.price;
  if (product.extracted_price != null) return formatMoney(product.extracted_price, product.currency);
  return "—";
}

function IssueRows({ issue }: { issue: OrderIssue }) {
  const assessment = issue.assessment;
  const resolution = issue.resolution;
  return (
    <div className="mt-2 space-y-1.5 rounded-lg bg-neutral-sand/20 p-2.5">
      <Row label="Issue" value={`${humanize(issue.issue_type)} · ${humanize(issue.status)}`} />
      <Row
        label="Requested"
        value={`${humanize(issue.requested_resolution)} (preference only)`}
      />
      {assessment && (
        <>
          <Row label="Proposed action" value={humanize(assessment.proposed_action)} />
          <Row
            label="Amount"
            value={formatMoney(assessment.requested_amount, assessment.currency)}
          />
          {/* The gate's own word, never an outcome: `allow` here means the proposal passed the
              Authority Engine, not that money, a replacement or a return exists. */}
          <Row
            label="Authority Engine"
            value={`decision: ${assessment.authority_decision}`}
          />
          <Row label="Reason" value={assessment.decision_reason} />
          {assessment.proposal_source === "safe_fallback" && (
            <Row label="Source" value="deterministic fallback — the model's answer could not be used" />
          )}
        </>
      )}
      {issue.refund && (
        <Row
          label="Refund"
          value={`${humanize(issue.refund.status)} · ${formatMoney(issue.refund.amount, issue.refund.currency)}`}
        />
      )}
      {issue.replacement && (
        <Row
          label="Replacement"
          value={`${humanize(issue.replacement.status)} · provider ${issue.replacement.provider}`}
        />
      )}
      {issue.return && (
        <Row label="Return" value={`${humanize(issue.return.status)} · provider ${issue.return.provider}`} />
      )}
      {resolution && (
        <>
          <Row
            label="Resolution"
            value={`${humanize(resolution.resolution_state)} · ${humanize(resolution.action)}`}
          />
          <Row label="Executor" value={humanize(resolution.executor)} />
          {resolution.provider_reference && (
            <Row label="Provider ref" value={resolution.provider_reference} />
          )}
        </>
      )}
      {issue.manual_review?.requested && (
        <Row label="Manual review" value="requested — autonomous decisions paused; no human action claimed" />
      )}
    </div>
  );
}

interface Props {
  mission: IntentReceipt | null;
  checkout: CheckoutSummary | null;
  order: ResolveOrder | null;
  issues: OrderIssue[];
  delegationNotice?: string | null;
  delegationError?: string | null;
  isDelegating?: boolean;
  onDelegate?: (action: string) => void;
  onRevoke?: () => void;
}

/**
 * The live mission context, as RESOLVE last reported it.
 *
 * Nothing in here is assembled by the browser: every line is a field from the mission receipt, the
 * checkout review, the order record or the issue list the server returned. A section appears only
 * when the server has data for it, so the panel cannot describe a step that has not happened — and
 * it never restates a proposal as an achievement.
 */
export function MissionContextPanel({
  mission,
  checkout,
  order,
  issues,
  delegationNotice = null,
  delegationError = null,
  isDelegating = false,
  onDelegate,
  onRevoke,
}: Props) {
  if (!mission) {
    return (
      <Card className="p-5 text-xs leading-relaxed text-text-charcoal/55">
        Select a mission to see the state RESOLVE is holding for it.
      </Card>
    );
  }

  const intent = mission.shopping_intent ?? null;
  const isShopping = mission.mission_type === "shopping";
  const product = intent?.selected_product ?? null;
  const payment = intent?.payment ?? null;
  const confirmation = intent?.checkout ?? null;

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between gap-2 bg-neutral-sand/20 px-5 py-3">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/55">
          Mission context
        </p>
        <StatusPill status={mission.status} />
      </div>

      <Section title="Identity">
        <Row label="Type" value={humanize(mission.mission_type)} />
        <Row label="Objective" value={mission.objective} />
        <Row label="Mission id" value={<span className="font-mono">{mission.mission_id}</span>} />
        {mission.created_at && (
          <Row label="Created" value={new Date(mission.created_at).toLocaleString()} />
        )}
        {mission.deadline && <Row label="Deadline" value={new Date(mission.deadline).toLocaleString()} />}
      </Section>

      {isShopping && intent && (
        <Section title="Purchase intent">
          <Row label="Category" value={intent.category} />
          <Row label="Budget" value={formatMoney(intent.budget, intent.currency)} />
          <Row label="Brand" value={intent.brand} />
          <Row label="Size / variant" value={intent.size_or_variant} />
          {intent.color && <Row label="Color" value={intent.color} />}
          {intent.material && <Row label="Material" value={intent.material} />}
          {intent.gender && <Row label="Gender" value={intent.gender} />}
          {intent.intended_use && <Row label="Intended use" value={intent.intended_use} />}
          {intent.quantity != null && <Row label="Quantity" value={String(intent.quantity)} />}
          {intent.exclusions && intent.exclusions.length > 0 && (
            <Row label="Exclusions" value={intent.exclusions.join(", ")} />
          )}
          {intent.preferences.length > 0 && (
            <Row label="Preferences" value={intent.preferences.join(", ")} />
          )}
          {intent.missing_info.length > 0 && (
            <Row label="Still needed" value={intent.missing_info.join(", ")} />
          )}
          {intent.clarification_prompt && <Row label="Question" value={intent.clarification_prompt} />}
        </Section>
      )}

      {product && (
        <Section title="Selected product">
          <Row
            label="Item"
            value={productLine(product, productPrice(product))}
          />
          {product.merchant && <Row label="Merchant" value={product.merchant} />}
          {product.source && <Row label="Source" value={humanize(product.source)} />}
        </Section>
      )}

      {checkout && (
        <Section title="Checkout">
          <Row label="Quantity" value={String(checkout.quantity)} />
          <Row label="Subtotal" value={formatMoney(checkout.subtotal, checkout.currency)} />
          <Row label="Currency" value={checkout.currency} />
          <Row label="Provider" value={humanize(checkout.payment_provider)} />
          {checkout.payment_note && <Row label="Note" value={checkout.payment_note} />}
          {confirmation?.confirmed_at && (
            <Row
              label="You confirmed"
              value={new Date(confirmation.confirmed_at).toLocaleString()}
            />
          )}
        </Section>
      )}

      {payment && (
        <Section title="Payment">
          <Row label="Status" value={humanize(payment.status)} />
          <Row label="Amount" value={formatMoney(payment.amount, payment.currency)} />
          {payment.razorpay_order_id && (
            <Row label="Razorpay order" value={<span className="font-mono">{payment.razorpay_order_id}</span>} />
          )}
          {payment.razorpay_payment_id && (
            <Row
              label="Razorpay payment"
              value={<span className="font-mono">{payment.razorpay_payment_id}</span>}
            />
          )}
          {payment.failure_reason && <Row label="Failure" value={humanize(payment.failure_reason)} />}
          <Row
            label="Meaning"
            value={
              payment.status === "verified"
                ? "signature verified by the backend (TEST MODE)"
                : payment.status === "failed"
                  ? "not paid"
                  : "awaiting Razorpay + backend verification"
            }
          />
        </Section>
      )}

      {order && (
        <Section title="RESOLVE order">
          <Row label="Order id" value={<span className="font-mono">{order.order_id}</span>} />
          <Row label="Status" value={humanize(order.status)} />
          <Row label="Item" value={productLine(order.product)} />
          <Row label="Amount" value={formatMoney(order.amount, order.currency)} />
          <Row label="Merchant order" value={humanize(order.merchant_order_linkage)} />
          {order.fulfillment_note && <Row label="Note" value={order.fulfillment_note} />}
        </Section>
      )}

      {issues.length > 0 && (
        <Section title="Post-purchase issues">
          {issues.map((issue) => (
            <IssueRows key={issue.issue_id} issue={issue} />
          ))}
        </Section>
      )}

      {!isShopping && (
        <Section title="Recovery">
          <Row label="Budget authorized" value={formatMoney(mission.recovery_budget, "INR")} />
          <Row label="Spend so far" value={formatMoney(mission.total_recovery_spend, "INR")} />
          {mission.resolution_summary && <Row label="Outcome" value={mission.resolution_summary} />}
        </Section>
      )}

      {isShopping && (
        <Section title="Delegated to this agent">
          <p className="text-[11px] leading-relaxed text-text-charcoal/70">
            {mission.allowed_actions?.length
              ? humanize(mission.allowed_actions.join(", "))
              : "nothing yet — the agent may describe and prepare, but not seek a remedy"}
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {DELEGABLE_REMEDIES.map((remedy) => (
              <button
                key={remedy.action}
                onClick={() => onDelegate?.(remedy.action)}
                disabled={isDelegating || !onDelegate}
                className="inline-flex items-center gap-1 rounded-lg border border-text-charcoal/12 px-2.5 py-1 text-[11px] font-medium text-text-charcoal/75 transition hover:bg-neutral-sand/25 disabled:opacity-50"
              >
                <Shield size={11} className="shrink-0" />
                Allow {remedy.label.toLowerCase()}
              </button>
            ))}
            {mission.allowed_actions?.length > 0 && onRevoke && (
              <button
                onClick={onRevoke}
                disabled={isDelegating}
                className="rounded-lg border border-primary-rose/30 px-2.5 py-1 text-[11px] font-medium text-primary-rose transition hover:bg-primary-rose/10 disabled:opacity-50"
              >
                Withdraw all
              </button>
            )}
          </div>
          {delegationNotice && (
            <p className="mt-2 flex items-start gap-1.5 rounded-lg bg-neutral-sand/25 p-2 text-[11px] text-text-charcoal/75">
              <ShieldCheck size={12} className="mt-0.5 shrink-0" />
              {delegationNotice}
            </p>
          )}
          {delegationError && (
            <p className="mt-2 flex items-start gap-1.5 rounded-lg bg-primary-rose/10 p-2 text-[11px] text-primary-rose">
              <AlertCircle size={12} className="mt-0.5 shrink-0" />
              {delegationError}
            </p>
          )}
        </Section>
      )}
    </Card>
  );
}
