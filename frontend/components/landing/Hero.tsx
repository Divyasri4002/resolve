"use client";

import { motion } from "framer-motion";
import Link from "next/link";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";

export function Hero() {
  return (
    <section className="relative overflow-hidden px-6 pb-24 pt-20 lg:px-10 lg:pt-28">
      <div className="mx-auto max-w-4xl text-center">
        <motion.span
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
          className="mb-6 inline-block rounded-full bg-neutral-blush/60 px-4 py-1.5 text-xs font-medium text-primary-rose"
        >
          Built on Razorpay · bounded by a deterministic Authority Engine
        </motion.span>

        <motion.h1
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.1 }}
          className="font-display text-4xl leading-[1.1] tracking-tight text-text-charcoal sm:text-5xl lg:text-6xl"
        >
          From payment completed
          <br />
          to problem solved.
        </motion.h1>

        <motion.p
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.2 }}
          className="mx-auto mt-6 max-w-xl text-base text-text-charcoal/65 sm:text-lg"
        >
          RESOLVE watches what happens after your money moves, negotiates a fix with the
          merchant&apos;s own agent, and never spends a rupee beyond what you explicitly allowed.
        </motion.p>

        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.3 }}
          className="mt-9 flex items-center justify-center gap-4"
        >
          <Link href="/workspace">
            <Button variant="primary">Start a Mission</Button>
          </Link>
          <a href="#how-it-works">
            <Button variant="ghost">See how it works</Button>
          </a>
        </motion.div>
      </div>

      {/* Floating live-mission visual, mirroring a real fintech hero rather than a static screenshot */}
      <motion.div
        initial={{ opacity: 0, y: 30 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.7, delay: 0.4 }}
        className="relative mx-auto mt-16 max-w-3xl"
      >
        <Card className="grain-overlay relative overflow-visible bg-gradient-to-br from-neutral-blush/50 to-neutral-sand/40 p-8">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs uppercase tracking-wide text-text-charcoal/50">
                Mission · Camera purchase, Japan
              </p>
              <p className="mt-1 font-display text-xl">Recovery in progress</p>
            </div>
            <span className="rounded-full bg-accent-terracotta/20 px-3 py-1 text-xs font-medium text-accent-terracotta">
              Negotiating
            </span>
          </div>

          <div className="mt-6 space-y-2 text-sm text-text-charcoal/75">
            <p>✓ Damage confirmed from delivery photo</p>
            <p>✓ Merchant policy checked</p>
            <p>→ Merchant Agent offered replacement — ₹4,500 additional cost</p>
            <p className="text-text-charcoal/40">→ Authority check pending…</p>
          </div>
        </Card>

        <Card className="absolute -left-6 -top-6 hidden w-48 rotate-[-6deg] p-4 shadow-soft sm:block">
          <p className="text-[11px] uppercase tracking-wide text-text-charcoal/45">
            Authority
          </p>
          <p className="mt-1 font-display text-lg text-primary-rose">₹10,000 ceiling</p>
        </Card>

        <Card className="absolute -bottom-8 -right-6 hidden w-56 rotate-[5deg] p-4 shadow-soft sm:block">
          <p className="text-[11px] uppercase tracking-wide text-text-charcoal/45">
            Audit trail
          </p>
          <p className="mt-1 text-sm text-text-charcoal/75">Every action, timestamped.</p>
        </Card>
      </motion.div>
    </section>
  );
}
