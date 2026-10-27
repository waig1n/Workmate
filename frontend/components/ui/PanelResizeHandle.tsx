"use client";

import { useEffect, useRef } from "react";

import { cx } from "@/components/ui/Button";

export function PanelResizeHandle({
  edge,
  width,
  minWidth,
  maxWidth,
  label,
  onResize,
  onReset,
  className,
}: {
  edge: "left" | "right";
  width: number;
  minWidth: number;
  maxWidth: number;
  label: string;
  onResize: (delta: number) => void;
  onReset: () => void;
  className?: string;
}) {
  const cleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => () => cleanupRef.current?.(), []);

  function beginResize(event: React.PointerEvent<HTMLButtonElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    cleanupRef.current?.();
    const target = event.currentTarget;
    try {
      target.setPointerCapture(event.pointerId);
    } catch {
      // Window listeners below remain a safe fallback in older browsers.
    }
    let previousX = event.clientX;
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const move = (pointerEvent: PointerEvent) => {
      const movement = pointerEvent.clientX - previousX;
      previousX = pointerEvent.clientX;
      onResize(edge === "right" ? movement : -movement);
    };
    const finish = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      try {
        if (target.hasPointerCapture(event.pointerId)) target.releasePointerCapture(event.pointerId);
      } catch {
        // The pointer may already have been released by the browser.
      }
      cleanupRef.current = null;
    };
    cleanupRef.current = finish;
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLButtonElement>) {
    let delta = 0;
    if (event.key === "ArrowLeft") delta = edge === "left" ? 16 : -16;
    if (event.key === "ArrowRight") delta = edge === "right" ? 16 : -16;
    if (!delta) return;
    event.preventDefault();
    onResize(delta);
  }

  return (
    <button
      type="button"
      role="separator"
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={minWidth}
      aria-valuemax={maxWidth}
      aria-valuenow={Math.round(width)}
      title="拖动调整宽度，双击恢复默认"
      onPointerDown={beginResize}
      onDoubleClick={onReset}
      onKeyDown={handleKeyDown}
      className={cx(
        "group absolute inset-y-0 z-30 w-4 touch-none cursor-col-resize select-none items-center justify-center bg-transparent outline-none",
        edge === "left" ? "-left-1.5" : "-right-1.5",
        className,
      )}
    >
      <span className="h-12 w-1 rounded-full bg-[var(--line-strong)] shadow-sm transition-all group-hover:h-16 group-hover:bg-[var(--accent)] group-focus-visible:h-16 group-focus-visible:bg-[var(--accent)]" />
    </button>
  );
}
