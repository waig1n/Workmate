"use client";

import { useEffect, useRef, useState } from "react";

/** Follow growing output only while the reader stays near the bottom. */
export function useChatScroll(conversationId: string) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const scrollToLatest = () => {
    following.current = true;
    setShowLatest(false);
    const viewport = viewportRef.current;
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  };
  const onScroll = () => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const nearBottom = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop < 80;
    following.current = nearBottom;
    setShowLatest(!nearBottom);
  };
  useEffect(() => {
    scrollToLatest();
    const observer = new ResizeObserver(() => {
      if (following.current) scrollToLatest();
    });
    if (contentRef.current) observer.observe(contentRef.current);
    return () => observer.disconnect();
  }, [conversationId]);
  return { viewportRef, contentRef, showLatest, onScroll, scrollToLatest };
}
