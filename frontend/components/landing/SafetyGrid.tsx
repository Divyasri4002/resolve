"use client";

import { Card } from "@/components/ui/Card";
import { ShieldCheck, ScrollText, Ban, Landmark } from "lucide-react";

const ITEMS = [
  {
    icon: ShieldCheck,
    title: "Bounded by design",
    body: "RESOLVE is never given open-ended permission to act. Every mission carries a hard, machine-checked spend ceiling.",
  },
  {
    icon: ScrollText,
    title: "Every action explainable",
    body: "Every approval or block is written to an insert-only audit log — a full, replayable reason for every decision.",
  },
  {
    icon: Ban,
    title: "Nothing executes unchecked",
    body: "The reasoning model proposes. A separate, plain, deterministic Authority Engine is the only thing that can approve.",
  },
  {
    icon: Landmark,
    title: "Real payment rails",
    body: "Every refund, payout, and recovery payment runs through Razorpay, confirmed by a signature-verified webhook — never assumed.",
  },
];

export function SafetyGrid() {
  return (
    <section id="safety" className="bg-neutral-blush/20 px-6 py-24 lg:px-10">
      <div className="mx-auto max-w-6xl">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="font-display text-3xl tracking-tight sm:text-4xl">
            Why it&apos;s safe to hand this money
          </h2>
          <p className="mt-4 text-text-charcoal/65">
            The model has no path to unrestricted control over money. That holds regardless of
            how it&apos;s prompted, or what it &quot;believes&quot; is the right call.
          </p>
        </div>

        <div className="mt-14 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {ITEMS.map(({ icon: Icon, title, body }) => (
            <Card key={title} className="p-6">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-accent-terracotta/15 text-accent-terracotta">
                <Icon size={18} />
              </div>
              <h3 className="mt-4 font-display text-lg">{title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-text-charcoal/60">{body}</p>
            </Card>
          ))}
        </div>
      </div>
    </section>
  );
}
