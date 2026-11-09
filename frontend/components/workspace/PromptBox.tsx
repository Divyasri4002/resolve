"use client";

import { useState, type KeyboardEvent } from "react";
import { ArrowUp, Loader2 } from "lucide-react";

interface Props {
  onSubmit: (text: string) => void;
  placeholder?: string;
  /** True while the operation this input would start is already running. */
  disabled?: boolean;
}

/**
 * Bottom-anchored prompt box — used both to start a brand-new mission ("Buy this camera from
 * Japan, budget ₹1,20,000...") and to give RESOLVE a follow-up instruction mid-mission, exactly
 * like a chat input.
 */
export function PromptBox({ onSubmit, placeholder, disabled = false }: Props) {
  const [value, setValue] = useState("");

  const submit = () => {
    if (!value.trim() || disabled) return;
    onSubmit(value.trim());
    setValue("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="border-t border-text-charcoal/8 bg-surface px-6 py-4">
      <div className="mx-auto flex max-w-3xl items-end gap-3 rounded-2xl border border-text-charcoal/10 bg-neutral-sand/10 px-4 py-3 shadow-card">
        <textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          disabled={disabled}
          placeholder={placeholder ?? "Buy this camera from Japan. Budget ₹1,20,000..."}
          className="flex-1 resize-none bg-transparent text-sm text-text-charcoal placeholder:text-text-charcoal/35 focus:outline-none disabled:opacity-60"
        />
        <button
          onClick={submit}
          disabled={!value.trim() || disabled}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent-terracotta text-surface transition disabled:opacity-30"
          aria-label="Send"
        >
          {disabled ? <Loader2 size={15} className="animate-spin" /> : <ArrowUp size={15} />}
        </button>
      </div>
    </div>
  );
}
