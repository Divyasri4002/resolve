"use client";

import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";

export function ForMerchants() {
  return (
    <section id="merchants" className="px-6 py-24 lg:px-10">
      <div className="mx-auto grid max-w-6xl items-center gap-12 md:grid-cols-2">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-primary-rose">
            For merchants
          </p>
          <h2 className="mt-3 font-display text-3xl tracking-tight sm:text-4xl">
            Run your own Merchant Agent
          </h2>
          <p className="mt-4 text-text-charcoal/65">
            A small, self-hostable service that holds your return policy and your own bounded
            spend ceiling, and settles disputes directly with RESOLVE — often in under a minute,
            instead of days of email back-and-forth. No Merchant Agent yet? RESOLVE falls back to
            structured, policy-aware correspondence automatically.
          </p>
          <div className="mt-6">
            <Button variant="secondary">Read the integration docs</Button>
          </div>
        </div>

        <Card className="grain-overlay bg-gradient-to-br from-neutral-sand/40 to-surface p-8">
          <p className="text-xs uppercase tracking-wide text-text-charcoal/45">
            Merchant Agent · Authority
          </p>
          <div className="mt-4 space-y-3 text-sm">
            <div className="flex items-center justify-between rounded-lg bg-surface/70 px-4 py-3">
              <span className="text-text-charcoal/70">Replacement stock</span>
              <span className="font-medium">Available</span>
            </div>
            <div className="flex items-center justify-between rounded-lg bg-surface/70 px-4 py-3">
              <span className="text-text-charcoal/70">Goodwill spend ceiling</span>
              <span className="font-medium">₹8,000 / case</span>
            </div>
            <div className="flex items-center justify-between rounded-lg bg-surface/70 px-4 py-3">
              <span className="text-text-charcoal/70">Return window</span>
              <span className="font-medium">30 days</span>
            </div>
          </div>
        </Card>
      </div>
    </section>
  );
}
