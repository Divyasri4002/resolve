"use client";

import Link from "next/link";
import clsx from "clsx";
import { IconButton } from "@/components/ui/IconButton";
import { StatusPill } from "@/components/ui/StatusPill";
import type { IntentReceipt } from "@/lib/types";
import { Menu, PanelRight, RefreshCw, X } from "lucide-react";

interface WorkspaceHeaderProps {
  mission: IntentReceipt | null;
  /** The brand mark lives in the sidebar; the header shows it when the sidebar is collapsed. */
  showBrand: boolean;
  isRefreshing: boolean;
  onOpenMissions: () => void;
  /** Toggles the desktop context panel on xl screens; opens the drawer below xl. */
  onToggleContext: () => void;
  onRefresh: () => void;
  onCloseMission: () => void;
}

/** Compact fixed header over the conversation — the workspace's only chrome. */
export function WorkspaceHeader({
  mission,
  showBrand,
  isRefreshing,
  onOpenMissions,
  onToggleContext,
  onRefresh,
  onCloseMission,
}: WorkspaceHeaderProps) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-2 border-b border-text-charcoal/8 bg-surface/90 px-2 backdrop-blur-sm sm:px-4">
      <IconButton label="Open mission list" onClick={onOpenMissions} className="lg:hidden">
        <Menu size={16} />
      </IconButton>

      <div className={clsx("items-center", showBrand ? "flex" : "hidden lg:flex")}>
        <Link href="/" aria-label="RESOLVE home" className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-primary-rose font-display text-[11px] text-surface">
            R
          </span>
          <span className="hidden font-display text-sm tracking-tight sm:inline">RESOLVE</span>
        </Link>
      </div>

      {mission ? (
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <span className="truncate text-sm font-medium text-text-charcoal">
            {mission.objective}
          </span>
          <span className="hidden shrink-0 md:inline-flex">
            <StatusPill status={mission.status} />
          </span>
        </div>
      ) : (
        <div className="flex min-w-0 flex-1 items-center">
          <span className="truncate text-xs font-semibold uppercase tracking-wider text-accent-terracotta">
            Missions dashboard
          </span>
        </div>
      )}

      <div className="flex shrink-0 items-center gap-1">
        {mission && (
          <IconButton label="Toggle mission context panel" onClick={onToggleContext}>
            <PanelRight size={16} />
          </IconButton>
        )}
        <IconButton label="Refresh from RESOLVE" onClick={onRefresh} disabled={isRefreshing}>
          <RefreshCw size={15} className={isRefreshing ? "animate-spin" : ""} />
        </IconButton>
        {mission && (
          <IconButton label="Close this mission" onClick={onCloseMission}>
            <X size={16} />
          </IconButton>
        )}
      </div>
    </header>
  );
}
