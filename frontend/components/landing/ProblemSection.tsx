"use client";

import { Card } from "@/components/ui/Card";

const MANUAL_STEPS = [
  "Notice something's wrong",
  "Dig up the order",
  "Find the return policy",
  "Email the merchant",
  "Wait, follow up, argue",
  "Pay extra, out of pocket",
  "Track the replacement",
  "Hope it actually arrives",
];

const RESOLVE_STEPS = [
  "Detects the deviation automatically",
  "Gathers real evidence",
  "Negotiates with the merchant's agent",
  "Confirms it's within your budget",
  "Executes the fix on Razorpay",
  "Verifies delivery before closing the case",
];

export function ProblemSection() {
  return (
    <section className="bg-neutral-sand/25 px-6 py-24 lg:px-10">
      <div className="mx-auto max-w-6xl">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="font-display text-3xl tracking-tight sm:text-4xl">
            A successful payment isn&apos;t a successful outcome.
          </h2>
          <p className="mt-4 text-text-charcoal/65">
            Every payment protocol stops the moment money moves. What happens after — a damaged
            item, a lost package, a merchant who won&apos;t budge — is still entirely on you.
          </p>
        </div>

        <div className="mt-14 grid gap-6 md:grid-cols-2">
          <Card className="p-8">
            <p className="text-xs font-medium uppercase tracking-wide text-text-charcoal/45">
              Today — you're the orchestrator
            </p>
            <ul className="mt-5 space-y-3">
              {MANUAL_STEPS.map((step, i) => (
                <li key={step} className="flex items-start gap-3 text-sm text-text-charcoal/70">
                  <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-text-charcoal/10 text-[11px]">
                    {i + 1}
                  </span>
                  {step}
                </li>
              ))}
            </ul>
          </Card>

          <Card className="border-accent-terracotta/25 bg-gradient-to-br from-neutral-blush/35 to-surface p-8">
            <p className="text-xs font-medium uppercase tracking-wide text-accent-terracotta">
              With RESOLVE
            </p>
            <ul className="mt-5 space-y-3">
              {RESOLVE_STEPS.map((step) => (
                <li key={step} className="flex items-start gap-3 text-sm text-text-charcoal/80">
                  <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent-terracotta/20 text-accent-terracotta">
                    ✓
                  </span>
                  {step}
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
    </section>
  );
}
