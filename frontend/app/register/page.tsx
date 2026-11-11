"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { AlertCircle, ArrowLeft, Loader2 } from "lucide-react";

export default function RegisterPage() {
  const router = useRouter();
  const { user, isLoading: isAuthLoading, register } = useAuth();

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  // If already authenticated, redirect to workspace
  useEffect(() => {
    if (!isAuthLoading && user) {
      router.replace("/workspace");
    }
  }, [user, isAuthLoading, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    const cleanName = name.trim();
    const cleanEmail = email.trim();

    if (!cleanName || cleanName.length < 2) {
      setError("Please enter your full name (at least 2 characters).");
      return;
    }

    if (!cleanEmail) {
      setError("Please enter a valid email address.");
      return;
    }

    if (!password || password.length < 8) {
      setError("Password must be at least 8 characters long.");
      return;
    }

    if (password !== confirmPassword) {
      setError("Passwords do not match. Please verify both entries.");
      return;
    }

    setIsSubmitting(true);
    try {
      await register(cleanName, cleanEmail, password);
      router.push("/workspace");
    } catch (err: unknown) {
      if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Registration failed. Please check your information and try again.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  if (isAuthLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-surface">
        <div className="flex items-center gap-2 text-text-charcoal/60">
          <Loader2 size={20} className="animate-spin text-accent-terracotta" />
          <span className="text-sm font-medium">Checking session...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-surface px-6 py-12 lg:px-8">
      <div className="mx-auto w-full max-w-md">
        <Link
          href="/"
          className="inline-flex items-center gap-2 text-sm text-text-charcoal/60 transition hover:text-text-charcoal"
        >
          <ArrowLeft size={16} />
          Back to home
        </Link>
      </div>

      <div className="mx-auto mt-8 w-full max-w-md">
        <div className="text-center">
          <Link href="/" className="inline-flex items-center gap-2">
            <span className="flex h-10 w-10 items-center justify-center rounded-full bg-primary-rose font-display text-base text-surface shadow-soft">
              R
            </span>
            <span className="font-display text-2xl tracking-tight">RESOLVE</span>
          </Link>
          <h2 className="mt-6 font-display text-2xl tracking-tight text-text-charcoal sm:text-3xl">
            Create your account
          </h2>
          <p className="mt-2 text-sm text-text-charcoal/60">
            Start autonomous recovery and protection for your purchases.
          </p>
        </div>

        <Card className="mt-8 p-8 shadow-soft">
          {error && (
            <div className="mb-6 flex items-start gap-2.5 rounded-xl bg-status-failure-soft p-4 text-status-failure">
              <AlertCircle size={18} className="mt-0.5 shrink-0" />
              <p className="text-sm font-medium">{error}</p>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label
                htmlFor="name"
                className="block text-xs font-semibold uppercase tracking-wider text-text-charcoal/70"
              >
                Full Name
              </label>
              <input
                id="name"
                name="name"
                type="text"
                autoComplete="name"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Jane Doe"
                className="mt-1.5 block w-full rounded-xl border border-text-charcoal/15 bg-surface px-4 py-2.5 text-sm text-text-charcoal placeholder-text-charcoal/35 transition focus:border-accent-terracotta focus:outline-none focus:ring-2 focus:ring-accent-terracotta/20"
              />
            </div>

            <div>
              <label
                htmlFor="email"
                className="block text-xs font-semibold uppercase tracking-wider text-text-charcoal/70"
              >
                Email address
              </label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className="mt-1.5 block w-full rounded-xl border border-text-charcoal/15 bg-surface px-4 py-2.5 text-sm text-text-charcoal placeholder-text-charcoal/35 transition focus:border-accent-terracotta focus:outline-none focus:ring-2 focus:ring-accent-terracotta/20"
              />
            </div>

            <div>
              <label
                htmlFor="password"
                className="block text-xs font-semibold uppercase tracking-wider text-text-charcoal/70"
              >
                Password (min. 8 characters)
              </label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="new-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="mt-1.5 block w-full rounded-xl border border-text-charcoal/15 bg-surface px-4 py-2.5 text-sm text-text-charcoal placeholder-text-charcoal/35 transition focus:border-accent-terracotta focus:outline-none focus:ring-2 focus:ring-accent-terracotta/20"
              />
            </div>

            <div>
              <label
                htmlFor="confirmPassword"
                className="block text-xs font-semibold uppercase tracking-wider text-text-charcoal/70"
              >
                Confirm Password
              </label>
              <input
                id="confirmPassword"
                name="confirmPassword"
                type="password"
                autoComplete="new-password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="••••••••"
                className="mt-1.5 block w-full rounded-xl border border-text-charcoal/15 bg-surface px-4 py-2.5 text-sm text-text-charcoal placeholder-text-charcoal/35 transition focus:border-accent-terracotta focus:outline-none focus:ring-2 focus:ring-accent-terracotta/20"
              />
            </div>

            <div className="pt-3">
              <Button
                type="submit"
                variant="primary"
                disabled={isSubmitting}
                className="w-full justify-center py-3 text-sm font-semibold"
              >
                {isSubmitting ? (
                  <div className="flex items-center gap-2">
                    <Loader2 size={16} className="animate-spin" />
                    <span>Creating account...</span>
                  </div>
                ) : (
                  "Create Account"
                )}
              </Button>
            </div>
          </form>

          <div className="mt-6 text-center text-xs text-text-charcoal/60">
            Already have an account?{" "}
            <Link
              href="/login"
              className="font-semibold text-accent-terracotta hover:underline"
            >
              Sign in
            </Link>
          </div>
        </Card>
      </div>
    </div>
  );
}

