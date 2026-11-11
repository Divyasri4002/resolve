import { type ButtonHTMLAttributes, forwardRef } from "react";
import clsx from "clsx";

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** The accessible name — required, since an icon alone carries no semantics. */
  label: string;
}

/** Square, borderless icon-only button. */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(
  ({ label, className, type, ...props }, ref) => (
    <button
      ref={ref}
      type={type ?? "button"}
      aria-label={label}
      title={label}
      className={clsx(
        "inline-flex h-9 w-9 items-center justify-center rounded-full text-text-charcoal/60 transition hover:bg-text-charcoal/8 hover:text-text-charcoal disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    />
  )
);
IconButton.displayName = "IconButton";
