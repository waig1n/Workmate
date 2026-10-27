const AGUI_EVENT_TYPES = new Set([
  "RUN_STARTED", "RUN_FINISHED", "RUN_ERROR", "RUN_CANCELLED",
  "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
  "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT",
  "REASONING_MESSAGE_START", "REASONING_MESSAGE_CONTENT",
  "REASONING_MESSAGE_END", "STEP_STARTED", "STEP_FINISHED", "CUSTOM",
]);

export function normalizeAguiEvent(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const event = value as Record<string, unknown>;
  if (typeof event.type !== "string" || !AGUI_EVENT_TYPES.has(event.type)) {
    return null;
  }
  return event;
}

export function acceptsSseEventType(value: string): boolean {
  return AGUI_EVENT_TYPES.has(value);
}
