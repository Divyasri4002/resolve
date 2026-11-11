"use client";

import { useState } from "react";
import clsx from "clsx";
import { AlertCircle, CheckCircle2, ChevronDown } from "lucide-react";
import type { AgentToolTraceEntry } from "@/lib/types";

function describe(step: AgentToolTraceEntry): string {
  const parts = [step.tool.replace(/_/g, " ")];
  if (step.state) parts.push(`→ ${step.state.replace(/_/g, " ")}`);
  if (step.error_code) parts.push(`(refused: ${step.error_code.replace(/_/g, " ")})`);
  return parts.join(" ");
}

/**
 * The tool steps RESOLVE actually ran, exactly as the recorded trace lists them — nothing
 * here is synthesized. Collapsed to one summary line (the trace is history by the time it
 * renders); expands on demand for the per-step outcomes.
 */
export function ToolActivity({ tools }: { tools: AgentToolTraceEntry[] }) {
  const [expanded, setExpanded] = useState(false);
  if (tools.length === 0) return null;

  const refused = tools.filter((t) => !t.success).length;

  return (
    <div className="mt-3 rounded-xl bg-neutral-sand/20">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
      >
        <span className="flex-1 text-[11px] font-semibold uppercase tracking-wider text-text-charcoal/60">
          {tools.length} {tools.length === 1 ? "step" : "steps"} the agent ran
          {refused > 0 ? ` · ${refused} refused` : ""}
        </span>
        <ChevronDown
          size={14}
          className={clsx(
            "shrink-0 text-text-charcoal/45 transition-transform",
            expanded && "rotate-180"
          )}
        />
      </button>
      {expanded && (
        <ul className="space-y-1 border-t border-text-charcoal/8 px-3 py-2.5">
          {tools.map((step, i) => (
            <li
              key={`${step.tool}-${i}`}
              className="flex items-start gap-1.5 text-xs text-text-charcoal/70"
            >
              {step.success ? (
                <CheckCircle2 size={13} className="mt-0.5 shrink-0 text-status-success" />
              ) : (
                <AlertCircle size={13} className="mt-0.5 shrink-0 text-status-failure" />
              )}
              <span>{describe(step)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
