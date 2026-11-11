"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ArrowUp } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";

interface Props {
  onSubmit: (text: string) => void;
  placeholder?: string;
  /** True while the operation this input would start is already running. */
  disabled?: boolean;
}

const MAX_TEXTAREA_HEIGHT_PX = 160;

/**
 * Fixed-bottom composer. Enter sends, Shift+Enter adds a line, and the textarea grows
 * with its content up to a capped height (then scrolls internally) so the surrounding
 * layout never jumps. While a turn is running, the measured elapsed seconds are shown —
 * an honest clock, never a fabricated progress state.
 */
export function PromptBox({ onSubmit, placeholder, disabled = false }: Props) {
  const [value, setValue] = useState("");
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!disabled) {
      setElapsedSeconds(0);
      return;
    }
    const startedAt = Date.now();
    const timer = setInterval(
      () => setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000)),
      1000
    );
    return () => clearInterval(timer);
  }, [disabled]);

  const submit = () => {
    if (!value.trim() || disabled) return;
    onSubmit(value.trim());
    setValue("");
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) el.style.height = "auto";
    });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="border-t border-text-charcoal/8 bg-surface px-3 pb-3 pt-3 sm:px-6">
      <div className="mx-auto flex max-w-3xl items-end gap-2 rounded-2xl border border-text-charcoal/10 bg-neutral-sand/10 px-3.5 py-2.5 shadow-card transition focus-within:border-accent-terracotta/50">
        <label htmlFor="composer-input" className="sr-only">
          Message RESOLVE
        </label>
        <textarea
          id="composer-input"
          ref={textareaRef}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            const el = e.target;
            el.style.height = "auto";
            el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`;
          }}
          onKeyDown={onKeyDown}
          rows={1}
          disabled={disabled}
          placeholder={placeholder ?? "Buy this camera from Japan. Budget ₹1,20,000..."}
          className="max-h-40 flex-1 resize-none bg-transparent py-1.5 text-sm leading-relaxed text-text-charcoal placeholder:text-text-charcoal/35 focus:outline-none disabled:opacity-60"
        />
        <button
          onClick={submit}
          disabled={!value.trim() || disabled}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-accent-terracotta text-surface transition hover:brightness-105 disabled:opacity-30"
          aria-label="Send message"
        >
          {disabled ? <Spinner size={15} /> : <ArrowUp size={16} />}
        </button>
      </div>
      <p
        className="mx-auto mt-2 max-w-3xl text-center text-[10px] text-text-charcoal/35"
        aria-live="polite"
      >
        {disabled
          ? `Working… ${elapsedSeconds}s — a slow reply means the reasoning service is busy, not that RESOLVE is stuck.`
          : "Enter to send · Shift+Enter for a new line — RESOLVE executes only what the Authority Engine approves."}
      </p>
    </div>
  );
}
