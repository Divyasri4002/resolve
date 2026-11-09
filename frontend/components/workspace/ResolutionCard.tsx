"use client";

import { Card } from "@/components/ui/Card";
import type { IntentReceipt } from "@/lib/types";
import { CheckCircle2 } from "lucide-react";

export function ResolutionCard({ mission }: { mission: IntentReceipt }) {
  return (
    <Card className="mx-auto max-w-lg bg-gradient-to-br from-neutral-blush/30 to-surface p-8 text-center">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-accent-terracotta/15 text-accent-terracotta">
        <CheckCircle2 size={24} />
      </div>
      <h3 className="mt-4 font-display text-xl">Mission Restored</h3>
      <p className="mt-2 text-sm text-text-charcoal/65">{mission.objective}</p>

      <div className="mt-6 grid grid-cols-2 gap-4 text-left text-sm">
        <div className="rounded-lg bg-surface/70 p-3">
          <p className="text-text-charcoal/45">Additional spend</p>
          <p className="mt-1 font-medium">₹{mission.total_recovery_spend}</p>
        </div>
        <div className="rounded-lg bg-surface/70 p-3">
          <p className="text-text-charcoal/45">Authority limit</p>
          <p className="mt-1 font-medium">₹{mission.recovery_budget}</p>
        </div>
      </div>

      {mission.resolution_summary && (
        <p className="mt-5 text-sm text-text-charcoal/70">{mission.resolution_summary}</p>
      )}
    </Card>
  );
}
