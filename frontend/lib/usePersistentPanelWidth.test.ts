import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { usePersistentPanelWidth } from "./usePersistentPanelWidth";

describe("usePersistentPanelWidth", () => {
  beforeEach(() => window.localStorage.clear());

  it("restores a saved width and clamps it to the supported range", async () => {
    window.localStorage.setItem("panel", "900");
    const { result } = renderHook(() => usePersistentPanelWidth({ storageKey: "panel", defaultWidth: 292, minWidth: 236, maxWidth: 360 }));

    await waitFor(() => expect(result.current.width).toBe(360));
  });

  it("persists resizing and supports restoring the default", () => {
    const { result } = renderHook(() => usePersistentPanelWidth({ storageKey: "panel", defaultWidth: 292, minWidth: 236, maxWidth: 360 }));

    act(() => result.current.resizeBy(24));
    expect(result.current.width).toBe(316);
    expect(window.localStorage.getItem("panel")).toBe("316");
    act(() => result.current.reset());
    expect(result.current.width).toBe(292);
  });
});
