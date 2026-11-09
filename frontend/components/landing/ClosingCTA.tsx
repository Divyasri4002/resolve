"use client";

import Link from "next/link";
import { Button } from "@/components/ui/Button";

export function ClosingCTA() {
  return (
    <section className="grain-overlay relative overflow-hidden bg-gradient-to-br from-accent-terracotta via-primary-pink to-neutral-blush px-6 py-24 text-surface lg:px-10">
      <div className="mx-auto max-w-3xl text-center">
        <h2 className="font-display text-3xl tracking-tight sm:text-4xl">
          Ready to stop chasing your own refunds?
        </h2>
        <p className="mx-auto mt-4 max-w-xl text-surface/85">
          Set an objective, set a budget, and let RESOLVE hold the line from there.
        </p>
        <div className="mt-8 flex items-center justify-center gap-4">
          <Link href="/workspace">
            <Button variant="secondary" className="bg-surface text-text-charcoal hover:bg-surface/90">
              Start a Mission
            </Button>
          </Link>
          <a href="#how-it-works">
            <Button
              variant="ghost"
              className="border-surface/50 text-surface hover:bg-surface/10"
            >
              Learn more
            </Button>
          </a>
        </div>
      </div>
    </section>
  );
}
