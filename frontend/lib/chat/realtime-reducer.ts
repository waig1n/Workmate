import { taskControlStatus } from "./realtime-tasks";
import { parseBusinessState } from "./realtime-business-state";
/** Atomic typed presentation transitions. Cursor acknowledgement belongs to the same cut. */
import type { ChatState, ChatItem } from './model';
import type { Task, Artifact, PendingConfirm } from '../types';
import { applyTextDelta, compareStreamIds, decodeRealtimeCursor, parseMessageSnapshot, parseRealtimeEnvelope, parseRealtimeReset, realtimeMessageId, type MessageSnapshot } from './realtime-protocol';
export type RealtimeState = {
  generation: string;
  cursor: string;
  baseline: number;
  needsReset: boolean;
  sync: string;
  messages: Record<string, MessageSnapshot>;
  seen: Record<string, true>;
  exact: Record<string, boolean>;
  tasks: Task[];
};
type Reduce = (state: ChatState, event: Record<string, unknown>, cid: string) => ChatState;
const encoder = new TextEncoder();
function fail(state: ChatState): ChatState { return { ...state, realtime: { generation: '', baseline: 0, sync: 'awaiting_snapshot', messages: {}, seen: {}, exact: {}, tasks: [], ...state.realtime, cursor: '', needsReset: true } }; }
function present(state: ChatState, snapshot: MessageSnapshot, exact = false): ChatState {
  const ids = new Set([snapshot.message_id, ...snapshot.blocks.map(b => `${snapshot.message_id}:${b.block_id}`)]);
  const previous = state.realtime?.messages[snapshot.message_id];
  previous?.blocks.forEach(b => ids.add(`${snapshot.message_id}:${b.block_id}`));
  const pending = state.items.find(i => i.id === state.pendingAssistantId && i.kind === 'text' && i.taskId === snapshot.task_id);
  if (pending) ids.add(pending.id);
  const items = state.items.filter(i => !ids.has(i.id));
  const rawText = snapshot.blocks.filter(b => b.kind === 'text').map(b => b.text).join('');
  const terminal = ['final', 'interrupted'].includes(snapshot.phase);
  const text = terminal ? rawText.trim() : rawText;
  const removedPrefix = terminal ? Array.from(rawText).length - Array.from(rawText.trimStart()).length : 0;
  const processEnd = Math.min(Array.from(text).length, Math.max(0, (snapshot.process_end ?? 0) - removedPrefix));
  const projected: ChatItem[] = snapshot.blocks.filter(b => b.kind === 'reasoning').map(b => ({ kind: 'reasoning', id: `${snapshot.message_id}:${b.block_id}`, text: b.text, done: b.closed }));
  if (text || snapshot.incomplete)
    projected.push({ kind: 'text', id: snapshot.message_id, role: 'assistant', text, incomplete: snapshot.incomplete, done: ['final', 'interrupted'].includes(snapshot.phase) || snapshot.blocks.filter(b => b.kind === 'text').every(b => b.closed), taskId: snapshot.task_id, runId: snapshot.reply_id, presentation: { version: 1, process_end: processEnd } });
  // Keep message position stable when subsequent body frames arrive.
  const index = state.items.findIndex(i => ids.has(i.id));
  items.splice(index < 0 ? items.length : Math.min(index, items.length), 0, ...projected);
  return { ...state, items, pendingAssistantId: pending ? null : state.pendingAssistantId, realtime: { ...state.realtime!, messages: { ...state.realtime!.messages, [snapshot.message_id]: snapshot }, exact: { ...state.realtime!.exact, [snapshot.message_id]: exact } } };
}
export function reduceRealtime(state: ChatState, name: string, value: unknown, cursor: string, cid: string, reduce: Reduce, empty: () => ChatState): ChatState {
  try {
    const position = cursor ? decodeRealtimeCursor(cursor, cid) : null;
    if (name === 'REALTIME_RESET') {
      const reset = parseRealtimeReset(value, cid);
      if (!reset || !position || position.generation !== reset.generation)
        return fail(state);
      const b = parseBusinessState(reset.business_state, cid);
      if (!b)
        return fail(state);
      let next: ChatState = { ...empty(), realtime: { generation: reset.generation, cursor, baseline: reset.business_seq, needsReset: false, sync: reset.sync_status, messages: {}, seen: {}, exact: {}, tasks: [...b.tasks].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id)) }, artifacts: b.artifacts as Artifact[], artifactsHydratedFor: cid, pendingConfirm: (b.pending_confirm ?? null) as PendingConfirm | null, decidedConfirmTools: b.decided_confirm_tools as Record<string, string[]>, decidedConfirmVersions: b.closed_confirm_versions as string[] };
      for (const m of b.messages)
        next.items.push({ kind: 'text', id: m.id, role: m.role, taskId: m.task_id,
          runId: b.tasks.find(t => t.id === m.task_id)?.run_id,
          presentation: m.content.presentation,
          text: [m.content.text ?? '', ...(m.content.attachments ?? []).map(f => `📎 ${f.name}`)].filter(Boolean).join('\n\n'), done: true });
      for (const task of b.tasks as Task[])
        next.runs.push({ id: task.run_id ?? task.id, taskId: task.id, status: task.status, error: null });
      const active = b.tasks.find(t => ['running', 'awaiting_permission', 'cancelling'].includes(t.status)) ?? b.tasks.find(t => t.status === 'created');
      if (active)
        next.run = { active: true, status: active.status, runId: active.run_id ?? null, error: null };
      for (const snapshot of reset.snapshots)
        next = present(next, snapshot, true);
      const order = new Map([...b.tasks].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id)).map((t, i) => [t.id, i]));
      const owner = new Map(reset.snapshots.flatMap(s => [[s.message_id, s.task_id] as [
          string,
          string
        ], ...s.blocks.map(bl => [`${s.message_id}:${bl.block_id}`, s.task_id] as [
          string,
          string
        ])]));
      next.items.sort((a, b) => { const ta = a.kind === 'text' ? a.taskId : owner.get(a.id), tb = b.kind === 'text' ? b.taskId : owner.get(b.id); return (order.get(ta ?? '') ?? 0) - (order.get(tb ?? '') ?? 0) || Number(!(a.kind === 'text' && a.role === 'user')) - Number(!(b.kind === 'text' && b.role === 'user')); });
      for (const event of b.activity) {
        if (event.event_type === 'tool_call')
          next.items.push({ kind: 'tool', id: String(event.payload.toolCallId), taskId: event.task_id, runId: b.tasks.find(t => t.id === event.task_id)?.run_id, call: { toolCallId: String(event.payload.toolCallId), toolCallName: String(event.payload.tool), args: '', result: '', state: 'running' } });
        if (event.event_type === 'tool_content')
          next = reduce(next, { type: 'CUSTOM', name: 'TOOL_CONTENT_AVAILABLE', taskId: event.task_id, value: event.payload }, cid);
        if (event.event_type === 'tool_result')
          next.items = next.items.map(i => i.kind === 'tool' && i.id === event.payload.toolCallId ? { ...i, call: { ...i.call, state: String(event.payload.status) } } : i);
        if (event.event_type === 'tool_call')
          next.steps.push({ id: `${event.task_id}:${String(event.payload.toolCallId)}`, name: String(event.payload.tool ?? 'Tool'), state: 'running' });
        if (event.event_type === 'tool_result') {
          const step = next.steps.findLast(s => s.id === `${event.task_id}:${String(event.payload.toolCallId)}` && s.state === 'running');
          if (step)
            step.state = event.payload.status === 'error' ? 'error' : 'completed';
        }
      }
      const terminalTasks = new Set(b.tasks.filter(t => ['completed','failed','cancelled','interrupted'].includes(t.status)).map(t => t.id));
      const completeMessages = new Set(reset.snapshots.filter(s => s.phase === 'final').map(s => s.message_id));
      const uncertainTasks = new Set(b.tasks.filter(t => t.error?.code === 'runtime_execution_quarantined').map(t => t.id));
      next.items = next.items.map(item => item.kind === 'tool' && item.taskId && terminalTasks.has(item.taskId) && item.call.state === 'running'
        ? { ...item, call: { ...item.call, state: 'unknown' } }
        : item.kind === 'text' && item.role === 'assistant' && item.taskId && uncertainTasks.has(item.taskId) && !completeMessages.has(item.id)
          ? { ...item, done: true, incomplete: true } : item);
      // Compact audit payloads contain retrieval facts, not knowledge_status counters.
      for (const task of b.tasks) {
        if (!task.run_id) continue;
        const facts = [...b.activity, ...b.knowledge].filter(e => e.task_id === task.id);
        const retrievals = facts.filter(e => e.event_type === 'knowledge_retrieval' || e.payload.tool === 'search_knowledge');
        if (!retrievals.length) continue;
        const finished = new Map<string, string>();
        for (const event of retrievals) {
          if (event.event_type === 'tool_result' || event.payload.phase === 'finished') {
            finished.set(String(event.payload.toolCallId ?? 'static'), String(event.payload.status ?? 'error'));
          }
        }
        const failures = [...finished.values()].filter(status => !['success', 'completed'].includes(status)).length;
        const status = failures ? 'error' : task.status === 'completed' ? 'completed'
          : ['failed', 'cancelled', 'interrupted'].includes(task.status) ? 'interrupted' : 'running';
        next = reduce(next, { type: 'CUSTOM', name: 'knowledge_status', runId: task.run_id,
          value: { status, search_count: finished.size, failure_count: failures } }, cid);
      }
      for (const event of b.knowledge.filter(e => e.event_type === 'knowledge_sources'))
        next = reduce(next, { type: 'CUSTOM', name: 'knowledge_sources', taskId: event.task_id,
          runId: b.tasks.find(t => t.id === event.task_id)?.run_id, value: event.payload }, cid);
      // Unaccepted local sends are not part of the database baseline yet.
      const local = state.runs.filter(r => r.optimisticId && !r.taskId);
      const localIds = new Set(local.flatMap(r => [r.optimisticId!, r.optimisticId!.replace('assistant-pending-', 'user-')]));
      const pendingUsers = state.items.filter(i => i.kind === 'text' && i.role === 'user' && !i.taskId && i.id.startsWith('user-'));
      next.items.push(...state.items.filter(i => localIds.has(i.id) && !pendingUsers.includes(i)), ...pendingUsers);
      next.runs.push(...local);
      if (local.length) {
        next.run = state.run;
        next.pendingAssistantId = state.pendingAssistantId;
      }
      return next;
    }
    if (!state.realtime || state.realtime.needsReset)
      return fail(state);
    if (name === 'REALTIME_SNAPSHOT') {
      const s = parseMessageSnapshot(value, cid);
      if (!s || cursor)
        return fail(state);
      const old = state.realtime.messages[s.message_id];
      if (old && (s.execution_epoch < old.execution_epoch || s.snapshot_version < old.snapshot_version))
        return state;
      if (old && old.blocks.some(b => { const n = s.blocks.find(x => x.block_id === b.block_id); return !n || n.content_version < b.content_version; }))
        return state;
      if (old && old.blocks.some(b => { const n = s.blocks.find(x => x.block_id === b.block_id)!; return n.kind !== b.kind || !n.text.startsWith(b.text) || (b.closed && n.text !== b.text); }))
        return fail(state);
      return present(state, s, true);
    }
    if (name !== 'REALTIME_EVENT' || !position || position.generation !== state.realtime.generation)
      return fail(state);
    const event = parseRealtimeEnvelope(value, cid);
    if (!event)
      return fail(state);
    const previous = decodeRealtimeCursor(state.realtime.cursor, cid);
    if (previous && compareStreamIds(position.stream_id, previous.stream_id) <= 0)
      return state;
    let next = state;
    if (event.source === 'business') {
      if (event.business_seq! > state.realtime.baseline && !state.realtime.seen[event.event_id]) {
        if (Object.keys(state.realtime.seen).length >= 8192)
          return fail(state);
        const payload = event.payload;
        const value = (payload.value ?? {}) as Record<string, unknown>;
        const controlStatus = taskControlStatus(payload.name, value);
        const boundary = payload.name === 'HITL_APPROVAL_REQUEST' || payload.name === 'TASK_COMPLETED' || ['RUN_FINISHED', 'RUN_ERROR', 'RUN_CANCELLED'].includes(String(payload.type));
        // Quarantine reports lost authority/unknown outcome, not a completed
        // Reply. It preserves partial content and never invents a final cut.
        const uncertain = payload.type === 'RUN_ERROR' && payload.code === 'runtime_execution_quarantined'
          && (payload.context as Record<string, unknown> | undefined)?.resumable === false;
        if (boundary && !uncertain && payload.display_snapshot === undefined && payload.display_empty === undefined)
          return fail(state);
        if (payload.display_snapshot !== undefined) {
          const ref = payload.display_snapshot as Record<string, unknown>;
          if (!ref || typeof ref !== 'object' || typeof ref.message_id !== 'string' || ref.message_id !== realtimeMessageId(event.task_id!, event.reply_id!) || typeof ref.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(ref.sha256))
            return fail(state);
          const committed = state.realtime.messages[ref.message_id];
          const phases = payload.name === 'HITL_APPROVAL_REQUEST' ? ['approval'] : ['final', 'interrupted'];
          if (!committed || !state.realtime.exact[ref.message_id] || committed.snapshot_version !== ref.snapshot_version || committed.execution_epoch !== ref.execution_epoch || !phases.includes(committed.phase))
            return fail(state);
        }
        if (payload.display_empty !== undefined && (payload.display_empty !== true || payload.display_snapshot !== undefined))
          return fail(state);
        if (payload.type === 'TOOL_CALL_START' && event.task_id && event.reply_id) {
          const key = realtimeMessageId(event.task_id, event.reply_id), message = state.realtime.messages[key];
          if (message)
            state = present(state, { ...message, process_end: Array.from(message.blocks.filter(b => b.kind === 'text').map(b => b.text).join('')).length });
        }
        const currentTask = state.runs.find(r => r.id === state.run.runId)?.taskId;
        const foreign = Boolean(state.run.active && event.task_id && currentTask && event.task_id !== currentTask);
        const target = state.runs.find(r => r.taskId === event.task_id);
        const terminal = state.realtime!.messages[realtimeMessageId(event.task_id ?? 'unknown', event.reply_id ?? 'unknown')];
        // Late starts/approval must not revive a committed terminal message.
        const obsolete = terminal && ['final', 'interrupted'].includes(terminal.phase) && (payload.type === 'RUN_STARTED' || payload.name === 'HITL_APPROVAL_REQUEST' || controlStatus !== null);
        if (!obsolete) {
          const focused = foreign ? { ...state, run: { ...state.run, runId: event.reply_id ?? target?.id ?? null } } : state;
          next = reduce(focused, { ...payload, taskId: event.task_id ?? payload.taskId, runId: event.reply_id ?? payload.runId }, cid);
          if (foreign)
            next = { ...next, run: state.run, pendingConfirm: state.pendingConfirm, pendingAssistantId: state.pendingAssistantId };
        }
        const taskStatus = controlStatus ?? (payload.name === 'TASK_COMPLETED' ? value.taskStatus : payload.type === 'RUN_ERROR' ? 'failed' : payload.type === 'RUN_CANCELLED' ? 'cancelled' : payload.type === 'RUN_FINISHED' ? 'completed' : payload.type === 'RUN_STARTED' ? 'running' : payload.name === 'HITL_APPROVAL_REQUEST' ? 'awaiting_permission' : null);
        if (typeof taskStatus === 'string' && !obsolete)
          next = { ...next, realtime: { ...next.realtime!, tasks: next.realtime!.tasks.map(t => t.id === event.task_id ? { ...t, status: taskStatus as Task['status'], run_id: payload.type === 'RUN_STARTED' ? event.reply_id ?? t.run_id : t.run_id } : t) } };
        next = { ...next, realtime: { ...next.realtime!, seen: { ...next.realtime!.seen, [event.event_id]: true } } };
      }
    }
    else {
      const key = realtimeMessageId(event.task_id!, event.reply_id!);
      const old = state.realtime.messages[key];
      const covered = old?.covered_cursor;
      const alreadyCovered = covered?.generation === position.generation && compareStreamIds(position.stream_id, covered.stream_id) <= 0;
      if (!alreadyCovered && (!old || event.execution_epoch! >= old.execution_epoch) && !['final', 'interrupted'].includes(old?.phase ?? '')) {
        const snapshot: MessageSnapshot = old ? { ...old, blocks: old.blocks.map(b => ({ ...b })), execution_epoch: event.execution_epoch! } : { schema_version: 1, message_id: key, conversation_id: cid, task_id: event.task_id!, reply_id: event.reply_id!, execution_epoch: event.execution_epoch!, snapshot_version: 0, blocks: [], covered_cursor: null, business_seq: 0, phase: 'partial', incomplete: false, process_end: 0 };
        const kind = String(event.payload.type), blockId = String(event.payload.messageId ?? '');
        if (!['TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END', 'REASONING_MESSAGE_START', 'REASONING_MESSAGE_CONTENT', 'REASONING_MESSAGE_END'].includes(kind) || !blockId)
          return fail(state);
        const block = snapshot.blocks.find(b => b.block_id === blockId);
        if (block && block.kind !== (kind.startsWith('REASONING') ? 'reasoning' : 'text'))
          return fail(state);
        if (kind.endsWith('_START')) {
          if (!block)
            snapshot.blocks.push({ block_id: blockId, kind: kind.startsWith('REASONING') ? 'reasoning' : 'text', text: '', content_version: 0, closed: false });
        }
        else if (kind.endsWith('_CONTENT')) {
          if (!block || !event.text)
            return fail(state);
          const result = applyTextDelta({ message_id: key, block_id: blockId, content: block.text, content_version: block.content_version, end_utf8: encoder.encode(block.text).length, closed: block.closed, execution_epoch: old?.execution_epoch ?? event.execution_epoch! }, event);
          if (result.action === 'reset' || result.action === 'reject')
            return fail(state);
          block.text = result.state.content;
          block.content_version = result.state.content_version;
        }
        else if (kind.endsWith('_END')) {
          if (!block)
            return fail(state);
          block.closed = true;
        }
        else
          return fail(state);
        next = present(state, snapshot, Boolean(old && state.realtime?.exact[key] && JSON.stringify(old) === JSON.stringify(snapshot)));
      }
    }
    return { ...next, realtime: { ...next.realtime!, cursor } };
  }
  catch {
    return fail(state);
  }
}
