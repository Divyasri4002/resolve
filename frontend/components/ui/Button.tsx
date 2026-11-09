import { type ButtonHTMLAttributes, forwardRef } from "react";
import clsx from "clsx";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost";
}

/**
 * Re-skinned button — deliberately not the default shadcn radius/shadow. Uses the coral
 * design tokens only.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "primary", ...props }, ref) => {
    const base =
      "inline-flex items-center justify-center gap-2 rounded-full px-6 py-3 text-sm font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed";
    const variants = {
      primary:
        "bg-accent-terracotta text-surface shadow-soft hover:brightness-105 active:scale-[0.98]",
      secondary:
        "bg-text-charcoal text-surface hover:bg-text-charcoal/90 active:scale-[0.98]",
      ghost:
        "border border-text-charcoal/15 text-text-charcoal hover:bg-text-charcoal/5",
    };
    return (
      <button ref={ref} className={clsx(base, variants[variant], className)} {...props} />
    );
  }
);
Button.displayName = "Button";
