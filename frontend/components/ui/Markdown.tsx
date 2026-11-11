import type { ReactNode } from "react";
import clsx from "clsx";

/**
 * Minimal markdown rendering for agent messages: paragraphs, headers, bold/italic/
 * inline code, links, fenced code blocks, and simple lists. Everything renders as
 * React text nodes — message content is never turned into HTML, so nothing can inject.
 * Links are restricted to http(s)/root-relative targets and open with rel=noopener.
 */

const INLINE_PATTERN =
  /(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|`[^`]+`|\[[^\]]+\]\((?:https?:\/\/|\/)[^)\s]*\))/g;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const re = new RegExp(INLINE_PATTERN);
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) nodes.push(text.slice(lastIndex, match.index));
    const token = match[0];
    const key = `${keyPrefix}-${match.index}`;
    if (token.startsWith("**")) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`")) {
      nodes.push(
        <code
          key={key}
          className="rounded bg-text-charcoal/8 px-1 py-0.5 font-mono text-[0.85em]"
        >
          {token.slice(1, -1)}
        </code>
      );
    } else if (token.startsWith("[")) {
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token);
      if (link) {
        const external = link[2].startsWith("http");
        nodes.push(
          <a
            key={key}
            href={link[2]}
            target={external ? "_blank" : undefined}
            rel="noopener noreferrer"
            className="font-medium text-accent-terracotta underline underline-offset-2"
          >
            {link[1]}
          </a>
        );
      } else {
        nodes.push(token);
      }
    } else {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>);
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return nodes;
}

const STRUCTURAL =
  /^\s*(?:[-*]\s+|\d+[.)]\s+|#{1,4}\s+|```)/;

function renderBlocks(content: string): ReactNode[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.trim() === "") {
      i++;
      continue;
    }

    if (line.trim().startsWith("```")) {
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // closing fence (or end of content)
      blocks.push(
        <pre
          key={key++}
          className="overflow-x-auto rounded-xl bg-text-charcoal/95 p-3 text-xs leading-relaxed text-surface"
        >
          <code className="font-mono">{codeLines.join("\n")}</code>
        </pre>
      );
      continue;
    }

    const header = /^(#{1,4})\s+(.*)$/.exec(line);
    if (header) {
      const Tag = header[1].length <= 2 ? "h3" : "h4";
      blocks.push(
        <Tag
          key={key++}
          className={
            header[1].length <= 2
              ? "text-base font-semibold text-text-charcoal"
              : "text-sm font-semibold text-text-charcoal"
          }
        >
          {renderInline(header[2], `h${key}`)}
        </Tag>
      );
      i++;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i++;
      }
      blocks.push(
        <ul key={key++} className="list-disc space-y-1 pl-5">
          {items.map((item, j) => (
            <li key={j}>{renderInline(item, `li${key}-${j}`)}</li>
          ))}
        </ul>
      );
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ""));
        i++;
      }
      blocks.push(
        <ol key={key++} className="list-decimal space-y-1 pl-5">
          {items.map((item, j) => (
            <li key={j}>{renderInline(item, `oli${key}-${j}`)}</li>
          ))}
        </ol>
      );
      continue;
    }

    const paraLines: string[] = [];
    while (i < lines.length && lines[i].trim() !== "" && !STRUCTURAL.test(lines[i])) {
      paraLines.push(lines[i]);
      i++;
    }
    blocks.push(
      <p key={key++}>
        {paraLines.map((l, j) => (
          <span key={j}>
            {j > 0 && <br />}
            {renderInline(l, `p${key}-${j}`)}
          </span>
        ))}
      </p>
    );
  }

  return blocks;
}

export function Markdown({ content, className }: { content: string; className?: string }) {
  return <div className={clsx("space-y-2 text-sm leading-relaxed", className)}>{renderBlocks(content)}</div>;
}
