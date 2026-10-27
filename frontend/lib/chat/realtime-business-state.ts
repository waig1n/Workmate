/** Validate the public reset view before acknowledging any part of its cursor. */
import type { Artifact, PendingConfirm, Task } from '../types';
type ObjectValue = Record<string, unknown>;
const object = (v: unknown): v is ObjectValue => v !== null && typeof v === 'object' && !Array.isArray(v);
const string = (v: unknown): v is string => typeof v === 'string';
const integer = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
const nullable = (v: unknown, check: (x: unknown) => boolean) => v === null || check(v);
export type ResetMessage = {
  id: string;
  role: string;
  task_id: string;
  content: {
    text?: string;
    attachments?: {
      name: string;
    }[];
    presentation?: {
      version: number;
      process_end: number;
    };
  };
};
export type ResetAudit = {
  id: number;
  seq: number;
  task_id: string;
  event_type: string;
  payload: ObjectValue;
  created_at: string;
};
export type BusinessState = {
  messages: ResetMessage[];
  tasks: Task[];
  artifacts: Artifact[];
  activity: ResetAudit[];
  knowledge: ResetAudit[];
  pending_confirm: PendingConfirm | null;
  decided_confirm_tools: Record<string, string[]>;
  closed_confirm_versions: string[];
};
const activityKinds = new Set(['tool_call', 'tool_result', 'tool_content', 'run_started', 'run_resumed', 'run_finished', 'task_failed', 'task_cancelled', 'task_cancel_requested', 'execution_quarantined', 'permission_mode_changed', 'artifact_delivery']);
function task(v: unknown, cid: string): boolean {
  if (!object(v) || !['id', 'title', 'created_at', 'updated_at'].every(k => string(v[k])) || v.conversation_id !== cid)
    return false;
  if (!['created', 'running', 'awaiting_permission', 'cancelling', 'completed', 'failed', 'cancelled', 'interrupted'].includes(String(v.status)))
    return false;
  if (!['standard', 'strict', 'read_only', 'full_access', 'unattended'].includes(String(v.approval_profile)))
    return false;
  return nullable(v.run_id, string) && nullable(v.error, object) && nullable(v.duration_ms, integer) && nullable(v.steps, integer) && nullable(v.model_name, string) && nullable(v.token_usage, object);
}
function message(v: unknown): boolean {
  if (!object(v) || !['id', 'role', 'task_id'].every(k => string(v[k])) || !object(v.content))
    return false;
  const c = v.content;
  return (c.text === undefined || string(c.text)) && (c.attachments === undefined || (Array.isArray(c.attachments) && c.attachments.every(a => object(a) && string(a.name)))) && (c.presentation === undefined || (object(c.presentation) && integer(c.presentation.version) && integer(c.presentation.process_end)));
}
function artifact(v: unknown, cid: string): boolean {
  return object(v) && v.conversation_id === cid && ['id', 'task_id', 'name', 'mime_type', 'change_type', 'created_at'].every(k => string(v[k])) && integer(v.size) && integer(v.version) && object(v.metadata);
}
function audit(v: unknown, knowledge = false): boolean {
  if (!object(v) || !integer(v.id) || !integer(v.seq) || !['task_id', 'event_type', 'created_at'].every(k => string(v[k])) || !object(v.payload))
    return false;
  if (!(knowledge ? ['knowledge_sources', 'knowledge_retrieval'].includes(String(v.event_type)) : activityKinds.has(String(v.event_type))))
    return false;
  const p = v.payload;
  if (v.event_type === 'tool_call' || v.event_type === 'tool_result')
    return string(p.toolCallId) && string(p.tool) && (v.event_type !== 'tool_result' || string(p.status));
  if (v.event_type === 'knowledge_sources')
    return Array.isArray(p.sources);
  return true;
}
function pending(v: unknown): boolean {
  return v === null || (object(v) && string(v.decisionVersion) && string(v.taskId) && Array.isArray(v.toolCalls) && v.toolCalls.every(c => object(c) && string(c.id) && string(c.name) && string(c.input)));
}
export function parseBusinessState(value: unknown, cid: string): BusinessState | null {
  if (!object(value) || !Array.isArray(value.messages) || !value.messages.every(message) || !Array.isArray(value.tasks) || !value.tasks.every(t => task(t, cid)) || !Array.isArray(value.artifacts) || !value.artifacts.every(a => artifact(a, cid)) || !Array.isArray(value.activity) || !value.activity.every(e => audit(e)) || !Array.isArray(value.knowledge) || !value.knowledge.every(e => audit(e, true)) || !pending(value.pending_confirm) || !object(value.decided_confirm_tools) || !Object.values(value.decided_confirm_tools).every(v => Array.isArray(v) && v.every(string)) || !Array.isArray(value.closed_confirm_versions) || !value.closed_confirm_versions.every(string))
    return null;
  return value as unknown as BusinessState;
}
