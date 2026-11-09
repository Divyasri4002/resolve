"use client";

import Link from "next/link";
import { Button } from "@/components/ui/Button";
import { useAuth } from "@/context/AuthContext";

const LINKS = [
  { href: "#how-it-works", label: "How it works" },
  { href: "#safety", label: "Why it's safe" },
  { href: "#merchants", label: "For merchants" },
];

export function Navbar() {
  const { user, logout } = useAuth();

  return (
    <header className="sticky top-0 z-50 w-full border-b border-text-charcoal/8 bg-surface/85 backdrop-blur-md">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4 lg:px-10">
        <Link href="/" className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary-rose font-display text-sm text-surface">
            R
          </span>
          <span className="font-display text-lg tracking-tight">RESOLVE</span>
        </Link>

        <nav className="hidden items-center gap-8 text-sm text-text-charcoal/70 md:flex">
          {LINKS.map((l) => (
            <a key={l.href} href={l.href} className="transition-colors hover:text-text-charcoal">
              {l.label}
            </a>
          ))}
        </nav>

        <div className="flex items-center gap-3">
          {user ? (
            <>
              <span className="hidden text-xs text-text-charcoal/60 md:inline-block">
                {user.name}
              </span>
              <Button
                variant="ghost"
                onClick={() => logout()}
                className="hidden sm:inline-flex text-xs py-2 px-4"
              >
                Sign out
              </Button>
              <Link href="/workspace">
                <Button variant="primary">Workspace</Button>
              </Link>
            </>
          ) : (
            <>
              <Link href="/login">
                <Button variant="ghost" className="hidden sm:inline-flex">
                  Sign in
                </Button>
              </Link>
              <Link href="/register">
                <Button variant="ghost" className="hidden md:inline-flex">
                  Create Account
                </Button>
              </Link>
              <Link href="/workspace">
                <Button variant="primary">Start a Mission</Button>
              </Link>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
