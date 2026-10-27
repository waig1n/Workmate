"use client";

import { useEffect, useId, useRef, type ReactNode } from "react";

import { createPortal } from "react-dom";

import { Icon } from "@/components/ui/Icons";

export function Dialog({ open, onClose, title, description, children, footer, width = "max-w-[520px]", side = false }: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: string;
  side?: boolean;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    const first = Array.from(panel?.querySelectorAll<HTMLElement>("input:not([disabled]), textarea:not([disabled]), select:not([disabled])") ?? []).find((element) => !element.closest("details:not([open])"))
      ?? panel?.querySelector<HTMLElement>("button:not([disabled]), a[href]");
    requestAnimationFrame(() => first?.focus());
    function onKeyDown(event: KeyboardEvent) {
      const dialogs = document.querySelectorAll('[role="dialog"][aria-modal="true"]');
      if (dialogs.item(dialogs.length - 1) !== panel) return;
      if (event.key === "Escape") onCloseRef.current();
      if (event.key !== "Tab" || !panel) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled])")).filter((element) => !element.closest("details:not([open])"));
      if (!focusable.length) return;
      const firstItem = focusable[0];
      const lastItem = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === firstItem) { event.preventDefault(); lastItem.focus(); }
      else if (!event.shiftKey && document.activeElement === lastItem) { event.preventDefault(); firstItem.focus(); }
    }
    document.addEventListener("keydown", onKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocusRef.current?.focus();
    };
  }, [open]);

  if (!open || typeof document === "undefined") return null;
  return createPortal(
    <div className={`popup-backdrop-enter fixed inset-0 z-50 grid ${side ? "justify-items-end" : "place-items-center p-4"} bg-[#15130f]/35`} onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={panelRef} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={description ? descriptionId : undefined} className={`${side ? "popup-sidecar-enter flex h-dvh flex-col" : "popup-dialog-enter rounded-[24px]"} panel-shadow product-surface w-full ${width} overflow-hidden`}>
        <div className="flex items-start gap-4 border-b border-[var(--line)] px-6 py-5">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-lg font-semibold tracking-[-0.02em]">{title}</h2>
            {description && <p id={descriptionId} className="mt-1.5 text-[13px] leading-5 text-[var(--ink-soft)]">{description}</p>}
          </div>
          <button type="button" onClick={onClose} aria-label="关闭" className="grid h-9 w-9 place-items-center rounded-xl text-[var(--ink-faint)] hover:bg-[var(--surface-subtle)] hover:text-[var(--ink)]"><Icon name="x" className="h-4 w-4" /></button>
        </div>
        <div className={`px-6 py-5 ${side ? "min-h-0 flex-1 overflow-y-auto" : ""}`}>{children}</div>
        {footer && <div className="flex items-center justify-end gap-2 border-t border-[var(--line)] bg-[var(--surface-subtle)] px-6 py-4">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
