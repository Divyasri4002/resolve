import clsx from "clsx";
import type { MissionStatus } from "@/lib/types";
import { missionStatusLabel } from "@/lib/format";

const STYLES: Record<MissionStatus, string> = {
  monitoring: "bg-neutral-sand/50 text-text-charcoal",
  deviation_detected: "bg-primary-pink/25 text-primary-rose",
  investigating: "bg-primary-pink/25 text-primary-rose",
  negotiating: "bg-accent-terracotta/20 text-accent-terracotta",
  awaiting_human_approval: "bg-primary-rose/20 text-primary-rose",
  executing: "bg-accent-terracotta/20 text-accent-terracotta",
  verifying: "bg-accent-terracotta/20 text-accent-terracotta",
  resolved: "bg-emerald-100 text-emerald-700",
  escalated: "bg-primary-rose/25 text-primary-rose",
  shopping_intent: "bg-blue-100 text-blue-700",
  needs_clarification: "bg-amber-100 text-amber-800",
  ready_for_discovery: "bg-blue-100 text-blue-800",
  discovery_completed: "bg-emerald-100 text-emerald-800",
  product_selected: "bg-emerald-100 text-emerald-700",
  checkout_review: "bg-amber-100 text-amber-800",
  checkout_confirmed: "bg-emerald-100 text-emerald-700",
  payment_pending: "bg-amber-100 text-amber-800",
  payment_verified: "bg-emerald-100 text-emerald-700",
  payment_failed: "bg-primary-rose/20 text-primary-rose",
  order_created: "bg-amber-100 text-amber-800",
  order_confirmed: "bg-emerald-100 text-emerald-700",
  awaiting_post_purchase_context: "bg-sky-100 text-sky-800",
};

export function StatusPill({ status }: { status: MissionStatus }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full px-3 py-1 text-xs font-medium",
        STYLES[status]
      )}
    >
      {missionStatusLabel(status)}
    </span>
  );
}
