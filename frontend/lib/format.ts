import type { MissionStatus } from "./types";

const CURRENCY_SYMBOLS: Record<string, string> = {
  INR: "₹",
  USD: "$",
  EUR: "€",
  GBP: "£",
};

/**
 * A money value as the server named it. The currency code always comes from the record being shown
 * — nothing here assumes rupees, and an unrecognised code is printed as its own symbol rather than
 * silently relabelled.
 */
export function formatMoney(
  value: string | number | null | undefined,
  currency?: string | null
): string {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  const amount = Number.isNaN(numeric) ? String(value) : numeric.toLocaleString("en-IN");
  const symbol = currency ? CURRENCY_SYMBOLS[currency] ?? `${currency} ` : "";
  return `${symbol}${amount}`;
}

export const MISSION_STATUS_LABELS: Record<MissionStatus, string> = {
  monitoring: "Monitoring",
  deviation_detected: "Deviation detected",
  investigating: "Investigating",
  negotiating: "Recovery in progress",
  awaiting_human_approval: "Needs your approval",
  executing: "Executing fix",
  verifying: "Verifying",
  resolved: "Resolved",
  escalated: "Escalated to you",
  shopping_intent: "Shopping Intent",
  needs_clarification: "Needs Clarification",
  ready_for_discovery: "Ready for Discovery",
  discovery_completed: "Products Discovered",
  product_selected: "Product Selected",
  checkout_review: "Checkout Review",
  checkout_confirmed: "Checkout Confirmed",
  payment_pending: "Payment Pending",
  payment_verified: "Payment Verified",
  payment_failed: "Payment Failed",
  order_created: "Order Created",
  order_confirmed: "Order Confirmed",
  awaiting_post_purchase_context: "Post-Purchase Review",
};

/**
 * The one wording a mission's stage gets anywhere in the product.
 *
 * Every pane used to carry its own translation of the same status, so a new backend state silently
 * fell through to a generic label in some places and not others. The `??` is for a value the server
 * can send that this union has not caught up with yet — an unknown stage is shown as itself, never
 * as a nearby stage that would read better.
 */
export function missionStatusLabel(status: MissionStatus | null | undefined): string {
  if (!status) return "Unknown";
  return MISSION_STATUS_LABELS[status] ?? status.replace(/_/g, " ");
}

/**
 * A byte count the way a person reads it — used for upload limits and attached-file sizes.
 * Values under a kilobyte stay exact; larger values round to one decimal in the largest
 * unit that keeps them above 1.
 */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || Number.isNaN(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = -1;
  do {
    value /= 1024;
    unitIndex += 1;
  } while (value >= 1024 && unitIndex < units.length - 1);
  return `${value >= 10 || Number.isInteger(value) ? Math.round(value) : value.toFixed(1)} ${units[unitIndex]}`;
}
