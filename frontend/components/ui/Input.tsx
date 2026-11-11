import { forwardRef, type InputHTMLAttributes, type TextareaHTMLAttributes } from "react";
import clsx from "clsx";

/**
 * The one field styling source — every text input and textarea in the app uses these
 * classes so focus treatment, borders and placeholder colors stay identical.
 */
export const inputClassName = clsx(
  "w-full rounded-xl border border-text-charcoal/15 bg-surface px-3.5 py-2.5 text-sm text-text-charcoal",
  "placeholder:text-text-charcoal/35 transition",
  "focus:border-accent-terracotta focus:outline-none focus:ring-2 focus:ring-accent-terracotta/20"
);

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input ref={ref} className={clsx(inputClassName, className)} {...props} />
  )
);
Input.displayName = "Input";

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea ref={ref} className={clsx(inputClassName, "resize-none", className)} {...props} />
  )
);
Textarea.displayName = "Textarea";
