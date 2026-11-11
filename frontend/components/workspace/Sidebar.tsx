"use client";

import Link from "next/link";
import clsx from "clsx";
import { useAuth } from "@/context/AuthContext";
import { StatusPill } from "@/components/ui/StatusPill";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { Spinner } from "@/components/ui/Spinner";
import type { IntentReceipt } from "@/lib/types";
import {
  History,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  ShieldCheck,
  ShoppingBag,
} from "lucide-react";

interface SidebarProps {
  missions: IntentReceipt[];
  selectedId: string | null;
  isLoading: boolean;
  collapsed: boolean;
  onToggleCollapse?: () => void;
  onSelect: (id: string) => void;
  onNew: () => void;
}

/**
 * The buyer's own missions, exactly as the authenticated list returns them.
 * Expanded on desktop, a narrow icon rail when collapsed, and always expanded
 * inside the mobile drawer (which passes collapsed={false}).
 */
export function Sidebar({
  missions,
  selectedId,
  isLoading,
  collapsed,
  onToggleCollapse,
  onSelect,
  onNew,
}: SidebarProps) {
  const { user, logout } = useAuth();

  if (collapsed) {
    return (
      <div className="flex h-full w-16 shrink-0 flex-col items-center border-r border-text-charcoal/8 bg-surface py-4">
        <button
          onClick={onNew}
          aria-label="Missions dashboard"
          title="Missions dashboard"
          className="flex h-8 w-8 items-center justify-center rounded-full bg-primary-rose font-display text-sm text-surface"
        >
          R
        </button>

        <button
          onClick={onNew}
          aria-label="New mission"
          title="New mission"
          className="mt-4 flex h-9 w-9 items-center justify-center rounded-xl border border-accent-terracotta/40 text-accent-terracotta transition hover:bg-accent-terracotta hover:text-surface"
        >
          <Plus size={16} />
        </button>

        <nav
          aria-label="Mission history"
          className="scrollbar-thin mt-4 flex min-h-0 flex-1 flex-col items-center gap-2 overflow-y-auto"
        >
          {missions.map((m) => {
            const isShop = m.mission_type === "shopping";
            const active = m.mission_id === selectedId;
            return (
              <button
                key={m.mission_id}
                onClick={() => onSelect(m.mission_id)}
                title={m.objective}
                aria-current={active ? "true" : undefined}
                className={clsx(
                  "flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border transition",
                  active
                    ? "border-accent-terracotta/50 bg-accent-terracotta/10 text-accent-terracotta"
                    : "border-transparent text-text-charcoal/45 hover:bg-neutral-sand/25 hover:text-text-charcoal"
                )}
              >
                {isShop ? <ShoppingBag size={15} /> : <ShieldCheck size={15} />}
              </button>
            );
          })}
        </nav>

        <div className="flex flex-col items-center gap-2 border-t border-text-charcoal/8 pt-3">
          {onToggleCollapse && (
            <IconButton label="Expand sidebar" onClick={onToggleCollapse}>
              <PanelLeftOpen size={16} />
            </IconButton>
          )}
          {user && (
            <span
              title={user.name}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-neutral-blush/50 text-xs font-bold text-primary-rose"
            >
              {user.name.trim().charAt(0).toUpperCase()}
            </span>
          )}
          <IconButton label="Sign out" onClick={() => logout()}>
            <LogOut size={15} />
          </IconButton>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full w-72 shrink-0 flex-col border-r border-text-charcoal/8 bg-surface">
      <div className="flex items-center justify-between px-4 pb-2 pt-4">
        <Link href="/" className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary-rose font-display text-sm text-surface">
            R
          </span>
          <span className="font-display text-lg tracking-tight">RESOLVE</span>
        </Link>
        {onToggleCollapse && (
          <IconButton
            label="Collapse sidebar"
            onClick={onToggleCollapse}
            className="hidden lg:inline-flex"
          >
            <PanelLeftClose size={16} />
          </IconButton>
        )}
      </div>

      <div className="px-3">
        <Button onClick={onNew} className="w-full">
          <Plus size={15} />
          New mission
        </Button>
      </div>

      <div className="mt-5 flex items-center gap-1.5 px-4 pb-2 text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/45">
        <History size={12} />
        Mission history
      </div>

      <nav
        aria-label="Mission history"
        className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-3 pb-3"
      >
        {isLoading ? (
          <div className="flex items-center gap-2 px-1 py-2 text-xs text-text-charcoal/50">
            <Spinner size={13} />
            Loading missions…
          </div>
        ) : missions.length === 0 ? (
          <p className="px-1 py-2 text-xs leading-relaxed text-text-charcoal/45">
            No missions yet. Start one with “New mission”.
          </p>
        ) : (
          <ul className="space-y-1">
            {missions.map((m) => {
              const isShop = m.mission_type === "shopping";
              const active = m.mission_id === selectedId;
              return (
                <li key={m.mission_id}>
                  <button
                    onClick={() => onSelect(m.mission_id)}
                    aria-current={active ? "true" : undefined}
                    className={clsx(
                      "w-full rounded-xl px-3 py-2.5 text-left transition",
                      active ? "bg-accent-terracotta/10" : "hover:bg-neutral-sand/25"
                    )}
                  >
                    <div className="flex items-start gap-2">
                      {isShop ? (
                        <ShoppingBag
                          size={14}
                          className={clsx(
                            "mt-0.5 shrink-0",
                            active ? "text-accent-terracotta" : "text-text-charcoal/40"
                          )}
                        />
                      ) : (
                        <ShieldCheck
                          size={14}
                          className={clsx(
                            "mt-0.5 shrink-0",
                            active ? "text-primary-rose" : "text-text-charcoal/40"
                          )}
                        />
                      )}
                      <div className="min-w-0 flex-1">
                        <p
                          className={clsx(
                            "line-clamp-2 text-xs font-medium leading-snug",
                            active ? "text-text-charcoal" : "text-text-charcoal/75"
                          )}
                        >
                          {m.objective}
                        </p>
                        <div className="mt-1.5 flex items-center justify-between gap-2">
                          <StatusPill status={m.status} />
                          <span className="shrink-0 font-mono text-[10px] text-text-charcoal/35">
                            …{m.mission_id.slice(-6)}
                          </span>
                        </div>
                      </div>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </nav>

      <div className="flex items-center justify-between gap-2 border-t border-text-charcoal/8 px-4 py-3">
        {user ? (
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-neutral-blush/50 text-xs font-bold text-primary-rose">
              {user.name.trim().charAt(0).toUpperCase()}
            </span>
            <div className="min-w-0">
              <p className="truncate text-xs font-semibold text-text-charcoal">{user.name}</p>
              <p className="truncate text-[11px] text-text-charcoal/50">{user.email}</p>
            </div>
          </div>
        ) : null}
        <IconButton label="Sign out" onClick={() => logout()}>
          <LogOut size={15} />
        </IconButton>
      </div>
    </div>
  );
}
