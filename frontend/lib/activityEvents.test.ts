import { describe, expect, it } from "vitest";
import { deliveryWarnings, visibleActivityEvents } from "./activityEvents";
import type { TaskEvent } from "./types";

const event = (event_type: string, payload = {}): TaskEvent => ({ id: 1, event_type, payload, seq: 1, task_id: "task", created_at: "2026-09-10" });
describe("activity presentation", () => {
  it("hides tool starts and routine read successes only in normal Activity", () => {
    const hidden = [event("tool_call", { tool: "Write" }), ...[
      "Read", "Glob", "Grep", "web_search", "web_fetch", "platform_get_context",
      "model_list", "capability_list", "agent_list", "agent_get", "agent_validate", "skill_validate",
    ].map((tool) => event("tool_result", { tool, status: "success" }))];
    const kept = [event("tool_result", { tool: "Read", status: "denied" }),
      event("tool_result", { tool: "web_search", status: "error" }),
      event("tool_result", { tool: "Write", status: "success" }),
      event("tool_result", { tool: "agent_create", status: "success" }),
      event("tool_result", { tool: "mcp__read", status: "success" }),
      event("tool_result", { status: "success" }),
      event("tool_result", { tool: "Read" })];
    const all = [...hidden, ...kept];
    expect(visibleActivityEvents(all)).toEqual(kept);
    expect(visibleActivityEvents(all, true)).toEqual(all);
    expect(all).toHaveLength(hidden.length + kept.length);
  });
  it("hides routine housekeeping without removing audit data", () => {
    const events = [event("run_metrics"), event("permission_mode_changed", { reason: "task_terminal" }), event("artifact_delivery", { source: "snapshot_fallback", warnings: ["present_files was not called with valid files during this run"] })];
    expect(visibleActivityEvents(events)).toEqual([]);
    expect(visibleActivityEvents(events, true)).toEqual(events);
  });
  it("keeps deliverables, approvals, errors, manual permission changes and strict failures", () => {
    const events = [event("artifact"), event("hitl_request"), event("stream_error"), event("permission_mode_changed", { reason: "user" }), event("artifact_delivery", { source: "strict_no_declaration" }), event("artifact_delivery", { warnings: ["Declared file missing"] })];
    expect(visibleActivityEvents(events)).toEqual(events);
    expect(deliveryWarnings(events.at(-1)!)).toEqual(["Declared file missing"]);
    expect(deliveryWarnings(event("artifact_delivery"))).toEqual([]);
    expect(visibleActivityEvents([])).toEqual([]);
  });
});
