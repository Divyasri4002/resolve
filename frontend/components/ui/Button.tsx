import { type ButtonHTMLAttributes, forwardRef } from "react";
import clsx from "clsx";
import { Spinner } from "@/components/ui/Spinner";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "subtle" | "danger";
  size?: "sm" | "md" | "lg" | "icon";
  loading?: boolean;
}

/**
 * Re-skinned button — deliberately not the default shadcn radius/shadow. Uses the coral
 * design tokens only. `loading` disables the button and shows a spinner in place of
 * leading content; callers keep rendering their own label text.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "primary", size = "md", loading = false, disabled, children, ...props }, ref) => {
    const base =
      "inline-flex items-center justify-center gap-2 rounded-full font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed";
    const variants = {
      primary:
        "bg-accent-terracotta text-surface shadow-soft hover:brightness-105 active:scale-[0.98]",
      secondary:
        "bg-text-charcoal text-surface hover:bg-text-charcoal/90 active:scale-[0.98]",
      ghost:
        "border border-text-charcoal/15 text-text-charcoal hover:bg-text-charcoal/5",
      subtle:
        "text-accent-terracotta hover:bg-accent-terracotta/10",
      danger:
        "bg-status-failure text-surface hover:brightness-105 active:scale-[0.98]",
    };
    const sizes = {
      sm: "px-3.5 py-1.5 text-xs",
      md: "px-5 py-2.5 text-sm",
      lg: "px-6 py-3 text-sm",
      icon: "h-9 w-9 p-0",
    };
    return (
      <button
        ref={ref}
        disabled={disabled || loading}
        className={clsx(base, variants[variant], sizes[size], className)}
        {...props}
      >
        {loading ? (
          <>
            <Spinner size={14} />
            {children}
          </>
        ) : (
          children
        )}
      </button>
    );
  }
);
Button.displayName = "Button";
