import type { HTMLAttributes } from "react";
import clsx from "clsx";

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={clsx(
        "rounded-xl2 border border-text-charcoal/8 bg-surface shadow-card",
        className
      )}
      {...props}
    />
  );
}
