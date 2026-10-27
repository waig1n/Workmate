import type { Task } from "@/lib/types";
import type { ChatItem, ChatTextMsg, ChatToolMsg, RunEntry } from "./model";

export type ChatNode =
  | { type: "item"; item: ChatItem; hideText?: boolean }
  | { type: "summary"; key: string; run: RunEntry | null;
      tools: ChatToolMsg[]; progress: ChatTextMsg[];
      process: Array<ChatTextMsg | ChatToolMsg>; isLast: boolean };

/** Split only the explicit, versioned boundary; never infer intent from prose. */
function splitProcess(item: ChatTextMsg) {
  const chars = Array.from(item.text);
  const boundary = item.presentation;
  const end = boundary?.process_end;
  if (boundary?.version !== 1 || !Number.isSafeInteger(end) || end! <= 0 || end! > chars.length)
    return { answer: item, progress: null };
  return {
    answer: { ...item, text: chars.slice(end).join("") },
    progress: { ...item, id: `${item.id}:process`, text: chars.slice(0, end).join("") },
  };
}

/** Reply identity, rather than arrival order, associates replayed tools. */
export function groupChatItems(items: ChatItem[], runs: RunEntry[], tasks: Task[] = []): ChatNode[] {
  const byId = new Map(runs.map(run => [run.id, run]));
  const byTask = new Map(runs.filter(run => run.taskId).map(run => [run.taskId, run.id]));
  for (const task of tasks) {
    if (["created", "running", "awaiting_permission", "cancelling"].includes(task.status) && !byTask.has(task.id)) {
      const key = task.run_id ?? `task:${task.id}`;
      byTask.set(task.id, key);
      if (!byId.has(key)) byId.set(key, { id: key, taskId: task.id, status: task.status, error: null });
    }
  }
  const keyFor = (item: ChatItem) => item.kind === "reasoning" ? null
    : item.runId ?? (item.kind === "text" && item.taskId ? byTask.get(item.taskId) : null);
  const groups = new Map<string, Extract<ChatNode, { type: "summary" }>>();
  const getGroup = (key: string) => {
    let group = groups.get(key);
    if (!group) {
      group = { type: "summary", key, run: byId.get(key) ?? null, tools: [], progress: [], process: [], isLast: false };
      groups.set(key, group);
    }
    return group;
  };
  const answers = new Map<string, ChatTextMsg>();
  const hiddenAnchors = new Set<string>();
  for (const item of items) {
    const key = keyFor(item);
    if (!key) continue;
    if (item.kind === "tool") getGroup(key).tools.push(item);
    if (item.kind === "text" && item.role === "assistant") {
      const { answer, progress } = splitProcess(item);
      if (progress && !answer.text) hiddenAnchors.add(item.id);
      answers.set(item.id, answer);
      if (progress) getGroup(key).progress.push(progress);
    }
  }
  // Snapshots retain a cumulative process prefix, not per-tool text offsets.
  for (const group of groups.values()) group.process = [...group.progress, ...group.tools];
  const nodes: ChatNode[] = [];
  const emitted = new Set<string>();
  const anchored = new Set(items.filter(i => i.kind === "text" && i.role === "assistant").map(keyFor));
  let unknownGroup: Extract<ChatNode, { type: "summary" }> | null = null;
  let segment = 0;
  const emit = (key: string) => {
    const group = groups.get(key);
    if (group && !emitted.has(key)) { nodes.push(group); emitted.add(key); }
  };
  for (const item of items) {
    if (item.kind === "reasoning") { unknownGroup = null; continue; }
    const key = keyFor(item);
    if (item.kind === "tool") {
      if (key) { if (!anchored.has(key)) emit(key); continue; }
      if (!unknownGroup) {
        unknownGroup = { type: "summary", key: `ungrouped-${segment++}`, run: null, tools: [], progress: [], process: [], isLast: false };
        nodes.push(unknownGroup);
      }
      unknownGroup.tools.push(item); unknownGroup.process.push(item);
      continue;
    }
    unknownGroup = null;
    if (item.role === "assistant" && key) emit(key);
    const answer = answers.get(item.id) ?? item;
    // Keep the identity anchor for approvals, deliveries and missing-content notices.
    nodes.push({ type: "item", item: answer, ...(hiddenAnchors.has(item.id) ? { hideText: true } : {}) });
  }
  const lastRun = runs.at(-1);
  const hasReplyText = lastRun && items.some(item => item.kind === "text" && item.role === "assistant" && keyFor(item) === lastRun.id && item.text.trim());
  if (lastRun && !hasReplyText && !["RUN_FINISHED", "completed", "failed", "cancelled", "interrupted", "RUN_ERROR", "RUN_CANCELLED"].includes(lastRun.status) && !emitted.has(lastRun.id)) {
    nodes.push({ type: "summary", key: lastRun.id, run: lastRun, tools: [], process: [], progress: [], isLast: true });
  }
  for (const node of nodes) if (node.type === "summary") node.isLast = node.key === lastRun?.id;
  return nodes;
}
