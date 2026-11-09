"use client";

import { Card } from "@/components/ui/Card";

const STAGES = [
  {
    tag: "01 — Detect",
    title: "RESOLVE watches the real transaction",
    body: "Not a support inbox. Real carrier tracking, real merchant order status, real payment confirmations — checked continuously against what you were promised.",
  },
  {
    tag: "02 — Negotiate",
    title: "Two bounded agents settle it directly",
    body: "Your Buyer Agent and the merchant's own Merchant Agent exchange evidence and offers — each with its own hard spend ceiling — instead of a human on either end.",
  },
  {
    tag: "03 — Execute",
    title: "The fix runs on real payment rails",
    body: "A refund, a replacement, or a bounded top-up payment — executed through Razorpay, with every action checked against your authority first.",
  },
  {
    tag: "04 — Verify",
    title: "Nothing closes until it's actually confirmed",
    body: "A mission is marked resolved only once a webhook confirms the payment and the outcome is verified against real system state — never on the strength of an action having been sent.",
  },
];

export function HowItWorks() {
  return (
    <section id="how-it-works" className="px-6 py-24 lg:px-10">
      <div className="mx-auto max-w-6xl">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="font-display text-3xl tracking-tight sm:text-4xl">How it works</h2>
          <p className="mt-4 text-text-charcoal/65">
            Four stages, and a hard rule underneath all of them: the reasoning model can propose
            anything. It can execute nothing the Authority Engine hasn&apos;t approved.
          </p>
        </div>

        <div className="mt-14 space-y-6">
          {STAGES.map((stage, i) => (
            <Card
              key={stage.tag}
              className={`grid items-center gap-8 p-8 md:grid-cols-2 ${
                i % 2 === 1 ? "md:[&>div:first-child]:order-2" : ""
              }`}
            >
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-accent-terracotta">
                  {stage.tag}
                </p>
                <h3 className="mt-2 font-display text-2xl">{stage.title}</h3>
                <p className="mt-3 text-sm leading-relaxed text-text-charcoal/65">{stage.body}</p>
              </div>
              <div className="flex h-40 items-center justify-center rounded-xl2 bg-gradient-to-br from-neutral-sand/40 to-neutral-blush/30">
                <span className="font-display text-6xl text-primary-rose/25">{stage.tag.slice(0, 2)}</span>
              </div>
            </Card>
          ))}
        </div>
      </div>
    </section>
  );
}
