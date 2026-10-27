import type { TaskEvent } from "@/lib/types";

// Display-only list: audit storage always retains these tool boundaries.
const ROUTINE_READ_TOOLS = new Set([
  "Read", "Glob", "Grep", "web_search", "web_fetch",
  "platform_get_context", "model_list", "capability_list", "agent_list",
  "agent_get", "agent_validate", "skill_validate",
]);

const LEGACY_NO_DECLARATION = "present_files was not called with valid files during this run";

/** Presentation only: never discard persisted audit events. */
export function deliveryWarnings(event: TaskEvent): string[] {
  const warnings = event.payload.warnings;
  if (!Array.isArray(warnings)) return [];
  return warnings.filter((warning): warning is string => typeof warning === "string" &&
    !(event.payload.source === "snapshot_fallback" && warning === LEGACY_NO_DECLARATION));
}

export function visibleActivityEvents(events: TaskEvent[], audit = false): TaskEvent[] {
  if (audit) return events;
  return events.filter((event) => {
    if (event.event_type === "tool_call") return false;
    if (event.event_type === "tool_result" && event.payload.status === "success" &&
      typeof event.payload.tool === "string" && ROUTINE_READ_TOOLS.has(event.payload.tool)) return false;
    if (["knowledge_sources", "knowledge_retrieval"].includes(event.event_type)) return false;
    if (event.event_type === "tool_result" && event.payload.tool === "search_knowledge") return false;
    if (event.event_type === "run_metrics") return false;
    if (event.event_type === "permission_mode_changed" && event.payload.reason === "task_terminal") return false;
    if (event.event_type === "artifact_delivery") {
      return deliveryWarnings(event).length > 0 || event.payload.source === "strict_no_declaration" || Boolean(event.payload.error);
    }
    return true;
  });
}
