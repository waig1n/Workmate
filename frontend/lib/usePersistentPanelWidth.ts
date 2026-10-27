"use client";

import { useCallback, useEffect, useState } from "react";

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function usePersistentPanelWidth({
  storageKey,
  defaultWidth,
  minWidth,
  maxWidth,
}: {
  storageKey: string;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
}) {
  const [width, setWidth] = useState(defaultWidth);

  useEffect(() => {
    const stored = Number(window.localStorage.getItem(storageKey));
    if (Number.isFinite(stored) && stored > 0) {
      setWidth(clamp(stored, minWidth, maxWidth));
    }
  }, [maxWidth, minWidth, storageKey]);

  const update = useCallback((next: number | ((current: number) => number)) => {
    setWidth((current) => {
      const value = typeof next === "function" ? next(current) : next;
      const clamped = clamp(value, minWidth, maxWidth);
      window.localStorage.setItem(storageKey, String(clamped));
      return clamped;
    });
  }, [maxWidth, minWidth, storageKey]);

  const resizeBy = useCallback((delta: number) => {
    update((current) => current + delta);
  }, [update]);

  const reset = useCallback(() => update(defaultWidth), [defaultWidth, update]);

  return { width, resizeBy, reset };
}
