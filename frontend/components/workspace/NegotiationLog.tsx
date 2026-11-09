"use client";

import clsx from "clsx";
import type { NegotiationEntry } from "@/lib/types";

export function NegotiationLog({ entries }: { entries: NegotiationEntry[] }) {
  return (
    <div className="space-y-4">
      {entries.map((e, i) => {
        const isBuyer = e.sender === "buyer_agent";
        return (
          <div key={i} className={clsx("flex", isBuyer ? "justify-end" : "justify-start")}>
            <div
              className={clsx(
                "max-w-[75%] rounded-2xl px-4 py-3 text-sm shadow-card",
                isBuyer
                  ? "bg-accent-terracotta text-surface"
                  : "bg-neutral-sand/40 text-text-charcoal"
              )}
            >
              <p className="mb-1 text-[11px] font-medium uppercase tracking-wide opacity-70">
                {isBuyer ? "RESOLVE (your agent)" : "Merchant Agent"}
              </p>
              <p className="leading-relaxed">{e.text}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}
