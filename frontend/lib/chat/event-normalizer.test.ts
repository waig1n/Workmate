import { describe, expect, it } from "vitest";

import { acceptsSseEventType, normalizeAguiEvent } from "./event-normalizer";

describe("normalizeAguiEvent", () => {
  it("accepts declared Product AG-UI events", () => {
    expect(normalizeAguiEvent({ type: "RUN_STARTED", runId: "reply-1" }))
      .toEqual({ type: "RUN_STARTED", runId: "reply-1" });
    expect(acceptsSseEventType("CUSTOM")).toBe(true);
  });

  it.each([null, [], "event", {}, { type: "RUNTIME_INTERNAL_EVENT" }])(
    "rejects malformed or Runtime-private values",
    (value) => expect(normalizeAguiEvent(value)).toBeNull(),
  );
});
