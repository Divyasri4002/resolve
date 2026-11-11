"use client";

import { useEffect, useRef, type ReactNode } from "react";
import clsx from "clsx";
import { X } from "lucide-react";
import { IconButton } from "@/components/ui/IconButton";

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  side?: "left" | "right";
  children: ReactNode;
  className?: string;
}

/** Modal side panel — used for the mobile navigation and mission-context drawers. */
export function Drawer({ open, onClose, title, side = "right", children, className }: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panelRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[70]" role="dialog" aria-modal="true" aria-label={title}>
      <div
        className="absolute inset-0 bg-text-charcoal/30 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        tabIndex={-1}
        className={clsx(
          "absolute inset-y-0 flex w-80 max-w-[85vw] flex-col bg-surface shadow-card outline-none",
          side === "left" ? "left-0" : "right-0",
          className
        )}
      >
        {title && (
          <div className="flex items-center justify-between border-b border-text-charcoal/8 px-4 py-3">
            <p className="text-sm font-semibold text-text-charcoal">{title}</p>
            <IconButton label="Close panel" onClick={onClose}>
              <X size={16} />
            </IconButton>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}
