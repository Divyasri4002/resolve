"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const BOTTOM_THRESHOLD_PX = 80;

/**
 * Scroll management for the conversation column:
 * - follows new content only while the reader is already at the bottom,
 * - always lands on the latest line when the mission (resetKey) changes,
 * - exposes a jump-to-latest affordance once the reader has scrolled away.
 * Nothing here force-scrolls a reader who has deliberately scrolled up.
 */
export function useConversationScroll(followDeps: readonly unknown[], resetKey: unknown) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);

  const scrollToBottom = useCallback((behavior: ScrollBehavior) => {
    const el = containerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onScroll = () => {
      const atBottom =
        el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD_PX;
      stickToBottomRef.current = atBottom;
      setShowJumpToLatest(!atBottom);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // Opening a different mission always lands on its latest line.
  useEffect(() => {
    stickToBottomRef.current = true;
    setShowJumpToLatest(false);
    const frame = requestAnimationFrame(() => scrollToBottom("auto"));
    return () => cancelAnimationFrame(frame);
  }, [resetKey, scrollToBottom]);

  // While reading from the bottom, new content follows; scrolled up, the view stays put.
  useEffect(() => {
    if (stickToBottomRef.current) scrollToBottom("smooth");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, followDeps);

  const jumpToLatest = useCallback(() => {
    stickToBottomRef.current = true;
    setShowJumpToLatest(false);
    scrollToBottom("smooth");
  }, [scrollToBottom]);

  /** Re-engages following — called when the user sends a message. */
  const stickNow = useCallback(() => {
    stickToBottomRef.current = true;
    setShowJumpToLatest(false);
  }, []);

  return { containerRef, showJumpToLatest, jumpToLatest, stickNow };
}
