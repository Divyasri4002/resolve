import type { MissionStatus } from "@/lib/types";

/**
 * Semantic status tones — the single vocabulary for every state chip in the app
 * (mission pill, authority decisions, refund/replacement/return/resolution states).
 * Visual classes live in TONE_CLASSES so color styling has exactly one source.
 */
export type StatusTone = "neutral" | "brand" | "info" | "success" | "pending" | "failure";

export const TONE_CLASSES: Record<StatusTone, string> = {
  neutral: "bg-text-charcoal/8 text-text-charcoal/70",
  brand: "bg-accent-terracotta/15 text-accent-terracotta",
  info: "bg-status-info-soft text-status-info",
  success: "bg-status-success-soft text-status-success",
  pending: "bg-status-pending-soft text-status-pending",
  failure: "bg-status-failure-soft text-status-failure",
};

export const MISSION_STATUS_TONES: Record<MissionStatus, StatusTone> = {
  monitoring: "neutral",
  deviation_detected: "pending",
  investigating: "pending",
  negotiating: "brand",
  awaiting_human_approval: "pending",
  executing: "brand",
  verifying: "brand",
  resolved: "success",
  escalated: "info",
  shopping_intent: "info",
  needs_clarification: "pending",
  ready_for_discovery: "info",
  discovery_completed: "success",
  product_selected: "success",
  checkout_review: "pending",
  checkout_confirmed: "success",
  payment_pending: "pending",
  payment_verified: "success",
  payment_failed: "failure",
  order_created: "pending",
  order_confirmed: "success",
  awaiting_post_purchase_context: "info",
};

export const AUTHORITY_DECISION_TONES: Record<string, StatusTone> = {
  allow: "success",
  deny: "failure",
  escalate: "info",
};

// Refund: pending | verified | failed. Replacement/return: pending | requested | confirmed |
// failed | escalated (M4 manual-review routing).
export const EXECUTION_STATE_TONES: Record<string, StatusTone> = {
  pending: "pending",
  requested: "info",
  verified: "success",
  confirmed: "success",
  failed: "failure",
  escalated: "info",
};

export const RESOLUTION_STATE_TONES: Record<string, StatusTone> = {
  denied: "failure",
  pending: "pending",
  requested: "info",
  verified: "success",
  confirmed: "success",
  failed: "failure",
  escalated: "info",
  more_information_required: "neutral",
  no_action: "neutral",
};

export const ISSUE_STATUS_TONES: Record<string, StatusTone> = {
  reported: "pending",
  under_review: "pending",
  resolution_pending: "pending",
  replacement_requested: "info",
  return_requested: "info",
};

export function statusTone(
  map: Record<string, StatusTone>,
  key: string | null | undefined,
  fallback: StatusTone = "neutral"
): StatusTone {
  if (!key) return fallback;
  return map[key] ?? fallback;
}
