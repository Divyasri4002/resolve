"use client";

import { Check, Loader2 } from "lucide-react";

export interface ActivityLine {
  label: string;
  done: boolean;
}

/**
 * Renders the live, backend-driven activity stream for one mission. Every line here should
 * correspond to a real WebSocket event received in the parent (see app/workspace/page.tsx) —
 * nothing in this component is a scripted/looping animation.
 */
export function ActivityStream({ lines }: { lines: ActivityLine[] }) {
  return (
    <div className="space-y-2.5">
      {lines.map((line, i) => (
        <div key={i} className="flex items-center gap-3 text-sm">
          <span
            className={
              line.done
                ? "flex h-5 w-5 items-center justify-center rounded-full bg-accent-terracotta/20 text-accent-terracotta"
                : "flex h-5 w-5 items-center justify-center rounded-full bg-text-charcoal/8 text-text-charcoal/40"
            }
          >
            {line.done ? <Check size={12} /> : <Loader2 size={12} className="animate-spin" />}
          </span>
          <span className={line.done ? "text-text-charcoal/80" : "text-text-charcoal/45"}>
            {line.label}
          </span>
        </div>
      ))}
    </div>
  );
}
