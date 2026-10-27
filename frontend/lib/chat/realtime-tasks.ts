import type { Task } from '../types';

/** REST enriches metadata; the applied realtime cut owns status and Reply identity. */
export function mergeRealtimeTasks(live: Task[], fetched: Task[]): Task[] {
  const rows = new Map(fetched.map(task => [task.id, task]));
  for (const task of live) {
    const metadata = rows.get(task.id);
    const newer = metadata && metadata.updated_at >= task.updated_at ? metadata : task;
    rows.set(task.id, { ...task, ...newer, status: task.status, run_id: task.run_id ?? newer.run_id });
  }
  return [...rows.values()].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
}

/** Product translator/control events used when no new RUN_STARTED is emitted. */
export function taskControlStatus(name: unknown, value: Record<string, unknown>): Task['status'] | null {
  if (name === 'TASK_TIMELINE_EVENT' && value.phase === 'run_resumed') return 'running';
  if (name === 'TASK_CANCEL_REQUESTED' && ['cancelling', 'cancelled'].includes(String(value.status)))
    return value.status as 'cancelling' | 'cancelled';
  return null;
}

/** A terminal REST fact newer than our cut calls for atomic snapshot recovery. */
export function terminalTaskMismatch(live: Task[], fetched: Task[], conversationId: string): Task | undefined {
  const terminal = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
  return fetched.find(task => {
    const applied = live.find(row => row.id === task.id);
    return task.conversation_id === conversationId && terminal.has(task.status)
      && applied && !terminal.has(applied.status)
      && Date.parse(task.updated_at) >= Date.parse(applied.updated_at);
  });
}
