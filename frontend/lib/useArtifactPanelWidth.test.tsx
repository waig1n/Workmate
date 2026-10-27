import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import { useArtifactPanelWidth } from "./useArtifactPanelWidth";

it("allows half-screen previews and clamps saved widths when the window shrinks", () => {
  localStorage.clear();
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1920 });
  const { result } = renderHook(() => useArtifactPanelWidth());
  expect(result.current.maxWidth).toBe(960);
  act(() => result.current.resizeBy(1000));
  expect(result.current.width).toBe(960);
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1280 });
  act(() => window.dispatchEvent(new Event("resize")));
  expect(result.current.width).toBe(640);
  expect(result.current.maxWidth).toBe(640);
  act(() => result.current.reset());
  expect(result.current.width).toBe(460);
});
