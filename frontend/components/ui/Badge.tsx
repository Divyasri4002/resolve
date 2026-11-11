import type { HTMLAttributes } from "react";
import clsx from "clsx";
import { TONE_CLASSES, type StatusTone } from "@/lib/statusTone";

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: StatusTone;
}

/** Compact uppercase state chip — the one styling source for small status labels. */
export function Badge({ tone = "neutral", className, ...props }: BadgeProps) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
        TONE_CLASSES[tone],
        className
      )}
      {...props}
    />
  );
}
