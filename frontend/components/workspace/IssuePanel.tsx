"use client";

import { useState } from "react";
import type { OrderIssue } from "@/lib/types";
import { Card } from "@/components/ui/Card";
import { formatMoney } from "@/lib/format";
import {
  AlertCircle,
  CheckCircle2,
  ClipboardList,
  Loader2,
  MessageSquareWarning,
} from "lucide-react";

const ISSUE_TYPES = [
  { value: "damaged", label: "Damaged" },
  { value: "defective", label: "Defective" },
  { value: "wrong_item", label: "Wrong item" },
  { value: "missing_item", label: "Missing item" },
  { value: "not_received", label: "Not received" },
  { value: "quality_issue", label: "Quality issue" },
  { value: "other", label: "Other" },
];

const RESOLUTIONS = [
  { value: "none", label: "No preference" },
  { value: "refund", label: "Refund" },
  { value: "replacement", label: "Replacement" },
];

const STATUS_LABELS: Record<OrderIssue["status"], string> = {
  reported: "Reported",
  under_review: "Under review",
  resolution_pending: "Resolution pending",
  replacement_requested: "Replacement requested",
  return_requested: "Return requested",
};

const ACTION_LABELS: Record<string, string> = {
  request_refund: "Refund",
  request_replacement: "Replacement",
  request_return: "Return",
  escalate_to_human: "Escalate to human review",
  request_more_information: "Request more information",
  no_action: "No action",
};

const DECISION_STYLES: Record<string, string> = {
  allow: "bg-emerald-100 text-emerald-800",
  deny: "bg-primary-rose/10 text-primary-rose",
  escalate: "bg-sky-100 text-sky-800",
};

const REPLACEMENT_STYLES: Record<string, string> = {
  pending: "bg-amber-100 text-amber-800",
  requested: "bg-sky-100 text-sky-800",
  confirmed: "bg-emerald-100 text-emerald-800",
  failed: "bg-primary-rose/10 text-primary-rose",
};

const RETURN_STYLES: Record<string, string> = {
  pending: "bg-amber-100 text-amber-800",
  requested: "bg-sky-100 text-sky-800",
  confirmed: "bg-emerald-100 text-emerald-800",
  failed: "bg-primary-rose/10 text-primary-rose",
};

const REFUND_STYLES: Record<string, string> = {
  pending: "bg-amber-100 text-amber-800",
  verified: "bg-emerald-100 text-emerald-800",
  failed: "bg-primary-rose/10 text-primary-rose",
};

const RESOLUTION_STYLES: Record<string, string> = {
  denied: "bg-primary-rose/10 text-primary-rose",
  pending: "bg-amber-100 text-amber-800",
  requested: "bg-sky-100 text-sky-800",
  verified: "bg-emerald-100 text-emerald-800",
  confirmed: "bg-emerald-100 text-emerald-800",
  failed: "bg-primary-rose/10 text-primary-rose",
  escalated: "bg-sky-100 text-sky-800",
  more_information_required: "bg-neutral-sand text-text-charcoal/70",
  no_action: "bg-neutral-sand text-text-charcoal/70",
};

// A resolution in one of these states is finished: /resolve returns the persisted outcome and
// starts nothing new, so the frontend hides the button for them.
const TERMINAL_RESOLUTION_STATES = new Set([
  "verified",
  "confirmed",
  "escalated",
  "more_information_required",
  "no_action",
]);

// Remedies whose absence from the mandate surfaces as an authority gap the owner can close
// by delegating and then reassessing (M22).
const REMEDY_ACTIONS = new Set(["request_refund", "request_replacement", "request_return"]);

function assessmentWarning(
  assessment: OrderIssue["assessment"] | undefined | null
): string {
  const generic =
    "Proposal only — this is not an executed action. No refund, replacement, return, or merchant contact has occurred.";
  if (!assessment) return generic;
  const remedy =
    assessment.proposed_action === "request_refund"
      ? "refund"
      : assessment.proposed_action === "request_replacement"
        ? "replacement"
        : assessment.proposed_action === "request_return"
          ? "return"
          : null;
  if (assessment.authority_decision === "allow") {
    if (assessment.proposed_action === "request_refund") {
      return "Refund authorized within the delegated limit — but nothing has been executed yet. Executing the TEST-mode refund is a separate, explicit action.";
    }
    if (assessment.proposed_action === "request_replacement") {
      return "Replacement authorized by the Authority Engine — but nothing has been executed yet, and a merchant replacement requires a connected fulfilment provider.";
    }
    return generic;
  }
  if (assessment.authority_decision === "escalate" && remedy) {
    return `The ${remedy} needs delegated authority this mission does not hold — delegate it in the mission panel, then reassess this issue; or route it to manual review. Nothing has been executed.`;
  }
  if (assessment.authority_decision === "deny" && remedy) {
    return `A ${remedy} cannot be authorized for this case right now — the reason is on the record above, and the case can be routed to manual review. Nothing has been executed.`;
  }
  return generic;
}

function issueTypeLabel(value: string): string {
  return ISSUE_TYPES.find((t) => t.value === value)?.label ?? value;
}

function resolutionLabel(value: string): string {
  return RESOLUTIONS.find((r) => r.value === value)?.label ?? value;
}

function actionLabel(value: string): string {
  return ACTION_LABELS[value] ?? value.replace(/_/g, " ");
}

interface IssuePanelProps {
  issues: OrderIssue[];
  isProcessing?: boolean;
  error?: string | null;
  /** M22: remedies already delegated on this mission — drives the reassess affordance. */
  delegatedActions?: string[];
  onReport: (issueType: string, description: string, requestedResolution: string) => Promise<boolean>;
  onAssess: (issueId: string) => Promise<boolean>;
  /** M22: re-run the assessment after a delegation — an authority gap is recoverable. */
  onReassess?: (issueId: string) => Promise<boolean>;
  onRefund: (issueId: string) => Promise<boolean>;
  onReplace: (issueId: string) => Promise<boolean>;
  onReturn: (issueId: string) => Promise<boolean>;
  /** Task 13: action-agnostic orchestration — the backend chooses what runs. */
  onResolve: (issueId: string) => Promise<boolean>;
  /** Task 14B.5A: customer opt-out from automated decisions → manual review. */
  onManualReview: (issueId: string) => Promise<boolean>;
}

export function IssuePanel({
  issues,
  isProcessing = false,
  error = null,
  delegatedActions = [],
  onReport,
  onAssess,
  onReassess,
  onRefund,
  onReplace,
  onReturn,
  onResolve,
  onManualReview,
}: IssuePanelProps) {
  const [formOpen, setFormOpen] = useState(false);
  const [issueType, setIssueType] = useState("damaged");
  const [description, setDescription] = useState("");
  const [resolution, setResolution] = useState("none");

  const trimmed = description.trim();
  const canSubmit = trimmed.length > 0 && trimmed.length <= 2000;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit || isProcessing) return;
    const ok = await onReport(issueType, trimmed, resolution);
    if (ok) {
      setFormOpen(false);
      setDescription("");
      setIssueType("damaged");
      setResolution("none");
    }
  };

  return (
    <Card className="p-5 shadow-soft border-accent-terracotta/25">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-text-charcoal/8 pb-4">
        <div className="flex items-center gap-2">
          <MessageSquareWarning size={18} className="text-accent-terracotta" />
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/50">
              Post-Purchase
            </p>
            <p className="font-display text-base text-text-charcoal">Issue intake</p>
          </div>
        </div>
        {!formOpen && (
          <button
            onClick={() => setFormOpen(true)}
            disabled={isProcessing}
            className="inline-flex items-center gap-2 rounded-xl border border-accent-terracotta/40 px-4 py-2 text-xs font-semibold text-accent-terracotta transition hover:bg-accent-terracotta hover:text-surface disabled:cursor-not-allowed disabled:opacity-60"
          >
            <ClipboardList size={13} />
            Report an issue
          </button>
        )}
      </div>

      <p className="mt-3.5 text-xs leading-relaxed text-text-charcoal/60">
        Report a problem with this order. RESOLVE records the issue for review — your requested
        resolution is a preference only and never starts a refund, replacement, or merchant action
        on its own. Assessing an issue produces a proposal and an Authority Engine decision only.
        When — and only when — the Authority Engine explicitly allows a refund proposal, the backend
        can execute that refund in Razorpay TEST MODE from canonical server-side data; &quot;verified&quot;
        means the provider itself reported the refund processed. A replacement proposal that the
        Authority Engine allows is executed only through a connected merchant fulfilment provider —
        with none connected, RESOLVE records the intent as pending and no replacement is claimed.
        No shipping, delivery, or merchant
        fulfillment information exists for this record.
      </p>

      {issues.length > 0 && (
        <div className="mt-3.5 space-y-2.5">
          {issues.map((issue) => (
            <div
              key={issue.issue_id}
              className="rounded-xl border border-text-charcoal/10 bg-neutral-sand/20 p-3.5 text-xs"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="inline-flex items-center gap-2 font-semibold text-text-charcoal">
                  {issueTypeLabel(issue.issue_type)}
                  <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-800">
                    <CheckCircle2 size={10} className="shrink-0" />
                    {STATUS_LABELS[issue.status] ?? issue.status}
                  </span>
                </span>
                {issue.created_at && (
                  <span className="text-text-charcoal/50">
                    {new Date(issue.created_at).toLocaleString()}
                  </span>
                )}
              </div>
              <p className="mt-1.5 text-text-charcoal/75">{issue.description}</p>
              <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-text-charcoal/55">
                <span>
                  Requested resolution:{" "}
                  <span className="font-semibold text-text-charcoal/75">
                    {resolutionLabel(issue.requested_resolution)}
                  </span>{" "}
                  <span className="text-text-charcoal/45">(preference only)</span>
                </span>
                <span className="font-mono">{issue.issue_id}</span>
              </div>

              {issue.assessment ? (
                <div className="mt-3 rounded-lg border border-text-charcoal/10 bg-surface p-3 text-[11px]">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-semibold uppercase tracking-wider text-text-charcoal/50">
                      Resolution assessment
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${
                        DECISION_STYLES[issue.assessment.authority_decision] ??
                        "bg-neutral-sand text-text-charcoal/70"
                      }`}
                    >
                      Authority decision: {issue.assessment.authority_decision}
                    </span>
                  </div>
                  <p className="mt-1.5 text-text-charcoal/80">
                    Proposed resolution:{" "}
                    <span className="font-semibold">
                      {actionLabel(issue.assessment.proposed_action)}
                    </span>
                    {issue.assessment.requested_amount != null && (
                      <>
                        {" "}
                        — amount{" "}
                        <span className="font-semibold">
                          {formatMoney(issue.assessment.requested_amount, issue.assessment.currency)}
                        </span>{" "}
                        <span className="text-text-charcoal/45">(canonical order amount)</span>
                      </>
                    )}
                    {issue.assessment.proposal_source === "safe_fallback" && (
                      <span className="ml-1 text-text-charcoal/45">
                        (deterministic fallback — the model's response could not be used)
                      </span>
                    )}
                  </p>
                  <p className="mt-1 text-text-charcoal/65">{issue.assessment.issue_assessment}</p>
                  <p className="mt-1 text-text-charcoal/65">{issue.assessment.reasoning_summary}</p>
                  <p className="mt-1 text-text-charcoal/55">
                    Decision reason: {issue.assessment.decision_reason}
                  </p>
                  <p className="mt-2 flex items-center gap-1.5 rounded-md bg-amber-50 px-2 py-1.5 font-semibold text-amber-800">
                    <AlertCircle size={12} className="shrink-0" />
                    {assessmentWarning(issue.assessment)}
                  </p>
                        <p className="mt-1 font-mono text-[10px] text-text-charcoal/40">
                    {issue.assessment.assessment_id}
                  </p>
                </div>
              ) : (
                issue.status === "reported" && (
                  <button
                    onClick={() => onAssess(issue.issue_id)}
                    disabled={isProcessing}
                    className="mt-2.5 inline-flex items-center gap-2 rounded-lg border border-sky-500/40 px-3 py-1.5 text-[11px] font-semibold text-sky-700 transition hover:bg-sky-500 hover:text-surface disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isProcessing ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <ClipboardList size={12} />
                    )}
                    {isProcessing ? "Assessing…" : "Assess resolution"}
                  </button>
                )
              )}

              {issue.assessment &&
                !issue.manual_review?.requested &&
                !TERMINAL_RESOLUTION_STATES.has(issue.resolution?.resolution_state ?? "") && (
                  <button
                    onClick={() => onResolve(issue.issue_id)}
                    disabled={isProcessing}
                    className="mt-2.5 inline-flex items-center gap-2 rounded-lg border border-violet-500/40 px-3 py-1.5 text-[11px] font-semibold text-violet-700 transition hover:bg-violet-500 hover:text-surface disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isProcessing ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <ClipboardList size={12} />
                    )}
                    {isProcessing
                      ? "Resolving…"
                      : "Resolve issue (the backend re-checks authority and runs the permitted action)"}
                  </button>
                )}

              {/* M22: an authority gap is recoverable — once the owner delegates the remedy
                  (mission panel), reassessment runs the same gate against the new mandate. */}
              {issue.assessment?.authority_decision === "escalate" &&
                REMEDY_ACTIONS.has(issue.assessment.proposed_action) &&
                !delegatedActions.includes(issue.assessment.proposed_action) &&
                !issue.manual_review?.requested &&
                !TERMINAL_RESOLUTION_STATES.has(issue.resolution?.resolution_state ?? "") &&
                onReassess && (
                  <button
                    onClick={() => onReassess(issue.issue_id)}
                    disabled={isProcessing}
                    className="mt-2.5 inline-flex items-center gap-2 rounded-lg border border-sky-500/40 px-3 py-1.5 text-[11px] font-semibold text-sky-700 transition hover:bg-sky-500 hover:text-surface disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isProcessing ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <ClipboardList size={12} />
                    )}
                    {isProcessing
                      ? "Reassessing…"
                      : "Reassess after delegating the remedy (mission panel)"}
                  </button>
                )}

              {!issue.manual_review?.requested && (
                <button
                  onClick={() => onManualReview(issue.issue_id)}
                  disabled={isProcessing}
                  className="mt-2.5 ml-2 inline-flex items-center gap-2 rounded-lg border border-sky-500/40 px-3 py-1.5 text-[11px] font-semibold text-sky-700 transition hover:bg-sky-500 hover:text-surface disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {isProcessing ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <AlertCircle size={12} />
                  )}
                  {"Don't decide automatically — request manual review"}
                </button>
              )}

              {issue.manual_review?.requested && (
                <p className="mt-2 flex items-start gap-1.5 rounded-md bg-sky-50 px-2 py-1.5 text-[11px] font-semibold text-sky-800">
                  <AlertCircle size={12} className="mt-0.5 shrink-0" />
                  Manual review requested
                  {issue.manual_review.requested_at
                    ? ` (${new Date(issue.manual_review.requested_at).toLocaleString()})`
                    : ""}
                  {" — autonomous resolution is paused for this issue. Your request is recorded; " +
                    "no external ticket was created and no human action is claimed."}
                </p>
              )}

              {issue.assessment?.authority_decision === "allow" &&
                issue.assessment.proposed_action === "request_refund" &&
                issue.refund?.status !== "verified" && (
                  <button
                    onClick={() => onRefund(issue.issue_id)}
                    disabled={isProcessing}
                    className="mt-2.5 inline-flex items-center gap-2 rounded-lg border border-emerald-600/40 px-3 py-1.5 text-[11px] font-semibold text-emerald-700 transition hover:bg-emerald-600 hover:text-surface disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isProcessing ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <CheckCircle2 size={12} />
                    )}
                    {isProcessing
                      ? "Executing refund…"
                      : "Execute TEST-mode refund (within delegated limit — amount is server-derived)"}
                  </button>
                )}

              {issue.assessment?.authority_decision === "allow" &&
                issue.assessment.proposed_action === "request_replacement" &&
                issue.refund?.status !== "verified" &&
                issue.replacement?.status !== "requested" &&
                issue.replacement?.status !== "confirmed" && (
                  <button
                    onClick={() => onReplace(issue.issue_id)}
                    disabled={isProcessing}
                    className="mt-2.5 inline-flex items-center gap-2 rounded-lg border border-emerald-600/40 px-3 py-1.5 text-[11px] font-semibold text-emerald-700 transition hover:bg-emerald-600 hover:text-surface disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isProcessing ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <CheckCircle2 size={12} />
                    )}
                    {isProcessing
                      ? "Requesting replacement…"
                      : "Execute replacement (product is server-resolved — no input needed)"}
                  </button>
                )}

              {issue.refund && (
                <div className="mt-3 rounded-lg border border-text-charcoal/10 bg-surface p-3 text-[11px]">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-semibold uppercase tracking-wider text-text-charcoal/50">
                      Refund execution
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${
                        REFUND_STYLES[issue.refund.status] ?? "bg-neutral-sand text-text-charcoal/70"
                      }`}
                    >
                      {issue.refund.status}
                    </span>
                  </div>
                  <p className="mt-1.5 text-text-charcoal/80">
                    Amount:{" "}
                    <span className="font-semibold">
                      {formatMoney(issue.refund.amount, issue.refund.currency)}
                    </span>{" "}
                    <span className="text-text-charcoal/45">
                      (canonical captured-payment amount, never client-supplied)
                    </span>
                  </p>
                  {issue.refund.razorpay_refund_id && (
                    <p className="mt-1 text-text-charcoal/65">
                      Razorpay refund:{" "}
                      <span className="font-mono">{issue.refund.razorpay_refund_id}</span>
                      {issue.refund.provider_status && (
                        <> — provider status: {issue.refund.provider_status}</>
                      )}
                    </p>
                  )}
                  {issue.refund.status === "verified" && issue.refund.verified_at && (
                    <p className="mt-1 text-text-charcoal/65">
                      Verified by the provider at {new Date(issue.refund.verified_at).toLocaleString()}
                    </p>
                  )}
                  {issue.refund.error_reason && (
                    <p className="mt-1 text-primary-rose/80">Reason: {issue.refund.error_reason}</p>
                  )}
                  <p
                    className={`mt-2 flex items-start gap-1.5 rounded-md px-2 py-1.5 font-semibold ${
                      issue.refund.status === "verified"
                        ? "bg-emerald-50 text-emerald-800"
                        : "bg-amber-50 text-amber-800"
                    }`}
                  >
                    <AlertCircle size={12} className="mt-0.5 shrink-0" />
                    {issue.refund.refund_note ??
                      "Razorpay TEST-MODE refund only — no merchant fulfillment or real-world money movement is claimed."}
                  </p>
                  <p className="mt-1 font-mono text-[10px] text-text-charcoal/40">
                    {issue.refund.refund_id}
                  </p>
                </div>
              )}

              {issue.assessment?.authority_decision === "allow" &&
                issue.assessment.proposed_action === "request_return" &&
                issue.refund?.status !== "verified" &&
                issue.replacement?.status !== "requested" &&
                issue.replacement?.status !== "confirmed" &&
                issue.return?.status !== "requested" &&
                issue.return?.status !== "confirmed" && (
                  <button
                    onClick={() => onReturn(issue.issue_id)}
                    disabled={isProcessing}
                    className="mt-2.5 inline-flex items-center gap-2 rounded-lg border border-emerald-600/40 px-3 py-1.5 text-[11px] font-semibold text-emerald-700 transition hover:bg-emerald-600 hover:text-surface disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isProcessing ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <CheckCircle2 size={12} />
                    )}
                    {isProcessing
                      ? "Requesting return…"
                      : "Execute return (order and product are server-resolved — no input needed)"}
                  </button>
                )}

              {issue.replacement && (
                <div className="mt-3 rounded-lg border border-text-charcoal/10 bg-surface p-3 text-[11px]">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-semibold uppercase tracking-wider text-text-charcoal/50">
                      Replacement execution
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${
                        REPLACEMENT_STYLES[issue.replacement.status] ??
                        "bg-neutral-sand text-text-charcoal/70"
                      }`}
                    >
                      {issue.replacement.status}
                    </span>
                  </div>
                  <p className="mt-1.5 text-text-charcoal/80">
                    Replacing:{" "}
                    <span className="font-semibold">{issue.replacement.original_title}</span>
                    {issue.replacement.original_merchant && (
                      <> from {issue.replacement.original_merchant}</>
                    )}{" "}
                    <span className="text-text-charcoal/45">
                      (canonical order product, resolved server-side)
                    </span>
                  </p>
                  <p className="mt-1 text-text-charcoal/65">
                    Quantity {issue.replacement.quantity} · value{" "}
                    {formatMoney(issue.replacement.value, issue.replacement.currency)}{" "}
                    {issue.replacement.currency} · provider {issue.replacement.provider}
                  </p>
                  {issue.replacement.provider_reference && (
                    <p className="mt-1 text-text-charcoal/65">
                      Provider reference:{" "}
                      <span className="font-mono">
                        {issue.replacement.provider_reference}
                      </span>
                      {issue.replacement.provider_outcome &&
                        ` — outcome: ${issue.replacement.provider_outcome}`}
                    </p>
                  )}
                  {issue.replacement.status === "confirmed" && issue.replacement.confirmed_at && (
                    <p className="mt-1 text-text-charcoal/65">
                      Confirmed by the provider at{" "}
                      {new Date(issue.replacement.confirmed_at).toLocaleString()}
                    </p>
                  )}
                  {issue.replacement.error_reason && (
                    <p className="mt-1 text-primary-rose/80">
                      Reason: {issue.replacement.error_reason}
                    </p>
                  )}
                  <p
                    className={`mt-2 flex items-start gap-1.5 rounded-md px-2 py-1.5 font-semibold ${
                      issue.replacement.provider === "none" &&
                      issue.replacement.status === "pending"
                        ? "bg-sky-50 text-sky-800"
                        : issue.replacement.status === "confirmed"
                          ? "bg-emerald-50 text-emerald-800"
                          : "bg-amber-50 text-amber-800"
                    }`}
                  >
                    <AlertCircle size={12} className="mt-0.5 shrink-0" />
                    {issue.replacement.provider === "none" &&
                    issue.replacement.status === "pending"
                      ? "Replacement capability is not currently connected. " +
                        (issue.replacement.replacement_note ?? "")
                      : issue.replacement.replacement_note ??
                        "No merchant replacement has been confirmed."}
                  </p>
                  <p className="mt-1 font-mono text-[10px] text-text-charcoal/40">
                    {issue.replacement.replacement_id}
                  </p>
                </div>
              )}

              {issue.return && (
                <div className="mt-3 rounded-lg border border-text-charcoal/10 bg-surface p-3 text-[11px]">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-semibold uppercase tracking-wider text-text-charcoal/50">
                      Return execution
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${
                        RETURN_STYLES[issue.return.status] ??
                        "bg-neutral-sand text-text-charcoal/70"
                      }`}
                    >
                      {issue.return.status}
                    </span>
                  </div>
                  <p className="mt-1.5 text-text-charcoal/80">
                    Returning:{" "}
                    <span className="font-semibold">{issue.return.original_title}</span>{" "}
                    <span className="text-text-charcoal/45">
                      (canonical order product, resolved server-side)
                    </span>
                  </p>
                  <p className="mt-1 text-text-charcoal/65">
                    Provider {issue.return.provider}
                    {issue.return.provider_reference && (
                      <>
                        {" · reference "}
                        <span className="font-mono">{issue.return.provider_reference}</span>
                      </>
                    )}
                    {issue.return.provider_outcome &&
                      ` · outcome: ${issue.return.provider_outcome}`}
                  </p>
                  {issue.return.status === "requested" && issue.return.requested_at && (
                    <p className="mt-1 text-text-charcoal/65">
                      Accepted by the provider at{" "}
                      {new Date(issue.return.requested_at).toLocaleString()} — acceptance only, not
                      a completed return.
                    </p>
                  )}
                  {issue.return.status === "confirmed" && issue.return.confirmed_at && (
                    <p className="mt-1 text-text-charcoal/65">
                      Confirmed by the provider at{" "}
                      {new Date(issue.return.confirmed_at).toLocaleString()}
                    </p>
                  )}
                  {issue.return.failure_reason && (
                    <p className="mt-1 text-primary-rose/80">
                      Reason: {issue.return.failure_reason}
                    </p>
                  )}
                  <p
                    className={`mt-2 flex items-start gap-1.5 rounded-md px-2 py-1.5 font-semibold ${
                      issue.return.provider === "none" && issue.return.status === "pending"
                        ? "bg-sky-50 text-sky-800"
                        : issue.return.status === "confirmed"
                          ? "bg-emerald-50 text-emerald-800"
                          : "bg-amber-50 text-amber-800"
                    }`}
                  >
                    <AlertCircle size={12} className="mt-0.5 shrink-0" />
                    {issue.return.provider === "none" && issue.return.status === "pending"
                      ? "Return provider not connected. " +
                        (issue.return.return_note ?? "")
                      : issue.return.return_note ??
                        "No merchant return has been confirmed."}
                  </p>
                  <p className="mt-1 font-mono text-[10px] text-text-charcoal/40">
                    {issue.return.return_id}
                  </p>
                </div>
              )}

              {issue.resolution && (
                <div className="mt-3 rounded-lg border border-violet-500/20 bg-surface p-3 text-[11px]">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-semibold uppercase tracking-wider text-text-charcoal/50">
                      Autonomous resolution
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${
                        RESOLUTION_STYLES[issue.resolution.resolution_state] ??
                        "bg-neutral-sand text-text-charcoal/70"
                      }`}
                    >
                      {issue.resolution.resolution_state.replace(/_/g, " ")}
                    </span>
                  </div>
                  <p className="mt-1.5 text-text-charcoal/80">
                    Action:{" "}
                    <span className="font-semibold">
                      {actionLabel(issue.resolution.action)}
                    </span>{" "}
                    · authority {issue.resolution.authority_decision} · executor{" "}
                    {issue.resolution.executor}
                  </p>
                  {issue.resolution.provider && (
                    <p className="mt-1 text-text-charcoal/65">
                      Provider {issue.resolution.provider}
                      {issue.resolution.provider_reference && (
                        <>
                          {" · reference "}
                          <span className="font-mono">
                            {issue.resolution.provider_reference}
                          </span>
                        </>
                      )}
                    </p>
                  )}
                  <p className="mt-2 flex items-start gap-1.5 rounded-md bg-amber-50 px-2 py-1.5 font-semibold text-amber-800">
                    <AlertCircle size={12} className="mt-0.5 shrink-0" />
                    {issue.resolution.resolution_note ??
                      "Being allowed is not being done — the summary follows the executor and the provider, never the request."}
                  </p>
                  <p className="mt-1 font-mono text-[10px] text-text-charcoal/40">
                    {issue.resolution.resolution_id}
                  </p>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {formOpen && (
        <form
          onSubmit={handleSubmit}
          className="mt-3.5 space-y-3 rounded-xl border border-text-charcoal/10 bg-neutral-sand/20 p-4"
        >
          <div>
            <label className="block text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/55">
              Issue type
            </label>
            <select
              value={issueType}
              onChange={(e) => setIssueType(e.target.value)}
              className="mt-1 w-full rounded-xl border border-text-charcoal/15 bg-surface px-3 py-2 text-sm text-text-charcoal focus:border-accent-terracotta focus:outline-none"
            >
              {ISSUE_TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/55">
              Description
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              maxLength={2000}
              placeholder="What went wrong? e.g. I received the wrong shampoo."
              className="mt-1 w-full rounded-xl border border-text-charcoal/15 bg-surface px-3 py-2 text-sm text-text-charcoal placeholder:text-text-charcoal/35 focus:border-accent-terracotta focus:outline-none"
            />
            <p className="mt-1 text-right text-[10px] text-text-charcoal/40">
              {trimmed.length}/2000
            </p>
          </div>

          <div>
            <label className="block text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/55">
              Requested resolution (your preference)
            </label>
            <select
              value={resolution}
              onChange={(e) => setResolution(e.target.value)}
              className="mt-1 w-full rounded-xl border border-text-charcoal/15 bg-surface px-3 py-2 text-sm text-text-charcoal focus:border-accent-terracotta focus:outline-none"
            >
              {RESOLUTIONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </div>

          {error && (
            <div className="flex items-start gap-2 rounded-xl border border-primary-rose/20 bg-primary-rose/10 p-3 text-xs font-medium text-primary-rose">
              <AlertCircle size={14} className="mt-0.5 shrink-0" />
              {error}
            </div>
          )}

          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setFormOpen(false);
                setDescription("");
                setIssueType("damaged");
                setResolution("none");
              }}
              disabled={isProcessing}
              className="rounded-xl px-4 py-2 text-xs font-semibold text-text-charcoal/60 transition hover:text-text-charcoal disabled:opacity-60"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSubmit || isProcessing}
              className="inline-flex items-center gap-2 rounded-xl bg-accent-terracotta px-5 py-2.5 text-xs font-semibold text-surface shadow-soft transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isProcessing ? (
                <>
                  <Loader2 size={13} className="animate-spin" />
                  Recording issue…
                </>
              ) : (
                "Report Issue"
              )}
            </button>
          </div>
        </form>
      )}
    </Card>
  );
}
