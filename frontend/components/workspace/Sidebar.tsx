"use client";

import clsx from "clsx";
import type { IntentReceipt } from "@/lib/types";
import { StatusPill } from "@/components/ui/StatusPill";
import { Plus, LogOut, ShoppingBag, ShieldCheck } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import { useRouter } from "next/navigation";

interface Props {
  missions: IntentReceipt[];
  selectedId: string | null;
  isLoading?: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
}

export function Sidebar({ missions, selectedId, isLoading = false, onSelect, onNew }: Props) {
  const { user, logout } = useAuth();
  const router = useRouter();

  const handleLogout = async () => {
    await logout();
    router.push("/login");
  };

  const formatDate = (dateStr?: string | null) => {
    if (!dateStr) return null;
    try {
      const d = new Date(dateStr);
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    } catch {
      return null;
    }
  };

  return (
    <aside className="flex h-full w-80 shrink-0 flex-col border-r border-text-charcoal/8 bg-neutral-sand/15">
      <div className="flex items-center justify-between px-5 py-5 border-b border-text-charcoal/8 bg-surface/30">
        <div className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-primary-rose font-display text-xs text-surface">
            R
          </span>
          <span className="font-display text-base">RESOLVE</span>
        </div>
        <button
          onClick={onNew}
          className="flex h-7 w-7 items-center justify-center rounded-full bg-accent-terracotta text-surface transition hover:brightness-105 shadow-soft"
          aria-label="Create new mission"
          title="Create new mission"
        >
          <Plus size={15} />
        </button>
      </div>

      <div className="scrollbar-thin flex-1 overflow-y-auto px-3 pb-4 pt-3">
        <div className="flex items-center justify-between px-2 pb-2.5">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/50">
            My Missions
          </p>
          <span className="rounded-full bg-text-charcoal/8 px-2 py-0.5 text-[10px] font-medium text-text-charcoal/60">
            {missions.length}
          </span>
        </div>

        {isLoading ? (
          <div className="space-y-2 px-2 py-4">
            <div className="h-14 animate-pulse rounded-lg bg-surface/60" />
            <div className="h-14 animate-pulse rounded-lg bg-surface/60" />
          </div>
        ) : (
          <div className="space-y-1.5">
            {missions.length === 0 ? (
              <div className="px-3 py-10 text-center">
                <p className="text-sm font-medium text-text-charcoal/60">No missions yet</p>
                <p className="mt-1 text-xs text-text-charcoal/45">
                  Start an autonomous shopping intent or recovery mission.
                </p>
                <button
                  onClick={onNew}
                  className="mt-4 inline-flex items-center gap-1.5 rounded-full bg-accent-terracotta px-4 py-2 text-xs font-semibold text-surface shadow-soft transition hover:brightness-105 active:scale-[0.98]"
                >
                  <Plus size={13} />
                  New Mission
                </button>
              </div>
            ) : (
              missions.map((m) => {
                const isSelected = selectedId === m.mission_id;
                const formattedDate = formatDate(m.created_at);
                const isShopping = m.mission_type === "shopping";

                return (
                  <button
                    key={m.mission_id}
                    onClick={() => onSelect(m.mission_id)}
                    className={clsx(
                      "w-full rounded-xl px-3 py-3 text-left transition border",
                      isSelected
                        ? "bg-surface shadow-card border-accent-terracotta/30 border-l-4 border-l-accent-terracotta"
                        : "border-transparent hover:bg-surface/60"
                    )}
                  >
                    <div className="flex items-start justify-between gap-1.5">
                      <div className="flex items-start gap-1.5 min-w-0">
                        {isShopping ? (
                          <ShoppingBag size={14} className="mt-0.5 shrink-0 text-accent-terracotta" />
                        ) : (
                          <ShieldCheck size={14} className="mt-0.5 shrink-0 text-primary-rose" />
                        )}
                        <p className="line-clamp-2 text-xs font-medium text-text-charcoal leading-snug">
                          {m.objective}
                        </p>
                      </div>
                      {formattedDate && (
                        <span className="shrink-0 text-[10px] text-text-charcoal/40">
                          {formattedDate}
                        </span>
                      )}
                    </div>
                    <div className="mt-2 flex items-center justify-between gap-2">
                      <StatusPill status={m.status} />
                      <span className="truncate text-[10px] font-mono text-text-charcoal/35">
                        #{m.mission_id.slice(-8)}
                      </span>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        )}
      </div>

      {user && (
        <div className="border-t border-text-charcoal/8 bg-surface/50 p-4">
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-semibold text-text-charcoal">{user.name}</p>
              <p className="truncate text-[11px] text-text-charcoal/50">{user.email}</p>
            </div>
            <button
              onClick={handleLogout}
              title="Sign out"
              className="flex h-8 w-8 items-center justify-center rounded-lg text-text-charcoal/50 transition hover:bg-primary-rose/10 hover:text-primary-rose"
              aria-label="Sign out"
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      )}
    </aside>
  );
}
