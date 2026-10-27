import { describe, expect, it } from "vitest";

import type { Task } from "@/lib/types";
import { groupTasksByConversation, projectedConversationStatus } from "./taskBoard";

function task(id: string, conversationId: string, status: Task["status"], createdAt: string): Task {
  return {
    id,
    conversation_id: conversationId,
    title: id,
    status,
    run_id: null,
    approval_profile: "standard",
    error: null,
    duration_ms: null,
    steps: null,
    model_name: null,
    token_usage: null,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

describe("task board grouping", () => {
  it("projects an actionable status before terminal history", () => {
    expect(projectedConversationStatus([
      task("done", "c1", "completed", "2026-09-01T00:00:00Z"),
      task("approval", "c1", "awaiting_permission", "2026-09-02T00:00:00Z"),
      task("queued", "c1", "created", "2026-09-03T00:00:00Z"),
    ])).toBe("awaiting_permission");
  });

  it("groups tasks under their Conversation and keeps newest first", () => {
    const groups = groupTasksByConversation([
      task("c1-old", "c1", "completed", "2026-09-01T00:00:00Z"),
      task("c2-new", "c2", "running", "2026-09-03T00:00:00Z"),
      task("c1-new", "c1", "completed", "2026-09-02T00:00:00Z"),
    ], []);

    expect(groups.map((group) => group.conversationId)).toEqual(["c2", "c1"]);
    expect(groups[1].tasks.map((item) => item.id)).toEqual(["c1-new", "c1-old"]);
  });
});
