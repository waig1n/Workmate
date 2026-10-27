import { describe, expect, it } from "vitest";

import fixture from "../../test/fixtures/agui/p0-flow.json";
import { chatReducer, initialChatState } from "./reducer";

describe("P0 AG-UI contract fixture", () => {
  it("projects a complete run into messages, activity and artifacts", () => {
    const state = fixture.reduce(
      (current, event) => chatReducer(current, {
        type: "event",
        event,
        conversationId: "conversation-1",
      }),
      initialChatState(),
    );
    expect(state.run).toMatchObject({ active: false, status: "RUN_FINISHED" });
    expect(state.items.find((item) => item.kind === "text")).toMatchObject({
      text: "# 分析完成\n\n成果已生成。",
      done: true,
    });
    expect(state.steps).toEqual([
      expect.objectContaining({ name: "enterprise-model", state: "completed" }),
    ]);
    expect(state.artifacts).toEqual([
      expect.objectContaining({ id: "artifact-1", name: "report.md" }),
    ]);
  });
});
