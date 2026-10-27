"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";

/** Hover for a quick explanation; click to keep it open on touch or keyboard. */
export function HelpTip({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  const root = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState(false);
  const [pinned, setPinned] = useState(false);
  const open = hovered || pinned;

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) {
        setPinned(false);
        setHovered(false);
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        setPinned(false);
        setHovered(false);
      }
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open]);

  return <div ref={root} className="relative shrink-0" onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}>
    <button type="button" aria-label={label} aria-expanded={open} aria-controls={open ? id : undefined}
      aria-describedby={open ? id : undefined}
      onClick={() => { setPinned(!pinned); setHovered(false); }}
      className="group grid h-6 w-6 place-items-center rounded-full text-[var(--ink-faint)] transition-colors hover:bg-[var(--surface-subtle)] hover:text-[var(--accent)] aria-expanded:bg-[var(--surface-subtle)] aria-expanded:text-[var(--accent)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]">
      <svg viewBox="0 0 20 20" fill="none" aria-hidden="true" className="h-4 w-4 transition-transform group-hover:scale-105">
        <circle cx="10" cy="10" r="7.25" stroke="currentColor" strokeWidth="1.25" />
        <path d="M7.9 7.5a2.1 2.1 0 0 1 4.2 0c0 1.45-2.1 1.65-2.1 3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        <circle cx="10" cy="13.2" r=".8" fill="currentColor" />
      </svg>
    </button>
    {open && <div id={id} role="tooltip" className="absolute right-0 top-full z-20 w-56 max-w-[calc(100vw-4rem)] rounded-xl border border-[var(--line)] bg-[var(--surface)] p-3.5 text-xs leading-5 text-[var(--ink-soft)] shadow-[0_8px_28px_rgba(0,0,0,0.10)]">
      {children}
    </div>}
  </div>;
}
