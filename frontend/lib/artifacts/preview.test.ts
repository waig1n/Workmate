import { describe, expect, it } from "vitest";

import { parseCsvPreview } from "./preview";

describe("parseCsvPreview", () => {
  it("handles quoted commas and escaped quotes", () => {
    expect(parseCsvPreview('name,note\nAlice,"a,b"\nBob,"said ""hi"""').rows)
      .toEqual([
        ["name", "note"],
        ["Alice", "a,b"],
        ["Bob", 'said "hi"'],
      ]);
  });

  it("limits the number of rendered rows", () => {
    const result = parseCsvPreview("a\nb\nc", 2);
    expect(result.rows).toHaveLength(2);
    expect(result.truncated).toBe(true);
  });
});
