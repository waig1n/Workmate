import { act, renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useChatScroll } from "./useChatScroll";

it("follows growth until the reader scrolls up and resumes only on request", () => {
  let resize!: () => void;
  const disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class { constructor(callback: () => void) { resize = callback; } observe() {} disconnect = disconnect; });
  const { result, unmount } = renderHook(() => useChatScroll("conversation"));
  const viewport = { scrollHeight: 1200, clientHeight: 400, scrollTop: 800 };
  Object.defineProperty(result.current.viewportRef, "current", { value: viewport, writable: true });
  act(() => resize());
  expect(viewport.scrollTop).toBe(1200);
  viewport.scrollTop = 100;
  act(() => result.current.onScroll());
  expect(result.current.showLatest).toBe(true);
  viewport.scrollHeight = 1600;
  act(() => resize());
  expect(viewport.scrollTop).toBe(100);
  act(() => result.current.scrollToLatest());
  expect(viewport.scrollTop).toBe(1600);
  expect(result.current.showLatest).toBe(false);
  unmount();
  expect(disconnect).toHaveBeenCalled();
  vi.unstubAllGlobals();
});
