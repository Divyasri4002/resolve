"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { AlertCircle, ArrowLeft, Loader2 } from "lucide-react";

export default function LoginPage() {
  const router = useRouter();
  const { user, isLoading: isAuthLoading, login } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
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

    const cleanEmail = email.trim();
    if (!cleanEmail) {
      setError("Please enter your email address.");
      return;
    }
    if (!password) {
      setError("Please enter your password.");
      return;
    }

    setIsSubmitting(true);
    try {
      await login(cleanEmail, password);
      router.push("/workspace");
    } catch (err: unknown) {
      if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Invalid email or password. Please try again.");
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
            Welcome back
          </h2>
          <p className="mt-2 text-sm text-text-charcoal/60">
            Sign in to access your missions and agent workspace.
          </p>
        </div>

        <Card className="mt-8 p-8 shadow-soft">
          {error && (
            <div className="mb-6 flex items-start gap-2.5 rounded-xl bg-status-failure-soft p-4 text-status-failure">
              <AlertCircle size={18} className="mt-0.5 shrink-0" />
              <p className="text-sm font-medium">{error}</p>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-5">
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
                className="mt-2 block w-full rounded-xl border border-text-charcoal/15 bg-surface px-4 py-3 text-sm text-text-charcoal placeholder-text-charcoal/35 transition focus:border-accent-terracotta focus:outline-none focus:ring-2 focus:ring-accent-terracotta/20"
              />
            </div>

            <div>
              <div className="flex items-center justify-between">
                <label
                  htmlFor="password"
                  className="block text-xs font-semibold uppercase tracking-wider text-text-charcoal/70"
                >
                  Password
                </label>
              </div>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="mt-2 block w-full rounded-xl border border-text-charcoal/15 bg-surface px-4 py-3 text-sm text-text-charcoal placeholder-text-charcoal/35 transition focus:border-accent-terracotta focus:outline-none focus:ring-2 focus:ring-accent-terracotta/20"
              />
            </div>

            <div className="pt-2">
              <Button
                type="submit"
                variant="primary"
                disabled={isSubmitting}
                className="w-full justify-center py-3 text-sm font-semibold"
              >
                {isSubmitting ? (
                  <div className="flex items-center gap-2">
                    <Loader2 size={16} className="animate-spin" />
                    <span>Signing in...</span>
                  </div>
                ) : (
                  "Sign In"
                )}
              </Button>
            </div>
          </form>

          <div className="mt-6 text-center text-xs text-text-charcoal/60">
            Don&apos;t have an account?{" "}
            <Link
              href="/register"
              className="font-semibold text-accent-terracotta hover:underline"
            >
              Create an account
            </Link>
          </div>
        </Card>
      </div>
    </div>
  );
}

