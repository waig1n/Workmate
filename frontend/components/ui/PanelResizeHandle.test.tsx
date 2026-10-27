import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PanelResizeHandle } from "./PanelResizeHandle";

describe("PanelResizeHandle", () => {
  it("supports keyboard resizing for a panel's right edge", () => {
    const onResize = vi.fn();
    render(<PanelResizeHandle edge="right" width={292} minWidth={236} maxWidth={360} label="调整导航宽度" onResize={onResize} onReset={vi.fn()} />);

    const separator = screen.getByRole("separator", { name: "调整导航宽度" });
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(onResize.mock.calls).toEqual([[16], [-16]]);
  });

  it("grows a right panel when its left edge moves left and resets on double click", () => {
    Object.defineProperty(window, "PointerEvent", { configurable: true, value: MouseEvent });
    const onResize = vi.fn();
    const onReset = vi.fn();
    render(<PanelResizeHandle edge="left" width={460} minWidth={340} maxWidth={560} label="调整成果栏宽度" onResize={onResize} onReset={onReset} />);

    const separator = screen.getByRole("separator", { name: "调整成果栏宽度" });
    fireEvent.pointerDown(separator, { button: 0, pointerId: 1, clientX: 500 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 468 });
    fireEvent.pointerUp(window, { pointerId: 1, clientX: 468 });
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    fireEvent.doubleClick(separator);
    expect(onResize).toHaveBeenCalledWith(32);
    expect(onResize).toHaveBeenCalledWith(16);
    expect(onReset).toHaveBeenCalledOnce();
  });
});
