import type { Conversation, Task, TaskStatus } from "@/lib/types";

export interface ConversationTaskGroup {
  conversationId: string;
  conversation: Conversation | null;
  tasks: Task[];
  latestTask: Task;
  status: TaskStatus;
}
const ACTIVE_PRIORITY: TaskStatus[] = [
  "awaiting_permission",
  "cancelling",
  "running",
  "created",
];

export function projectedConversationStatus(tasks: readonly Task[]): TaskStatus {
  for (const status of ACTIVE_PRIORITY) {
    if (tasks.some((task) => task.status === status)) return status;
  }
  return tasks[0]?.status ?? "created";
}

export function groupTasksByConversation(
  tasks: readonly Task[],
  conversations: readonly Conversation[],
): ConversationTaskGroup[] {
  const conversationById = new Map(conversations.map((item) => [item.id, item]));
  const grouped = new Map<string, Task[]>();
  for (const task of tasks) {
    const list = grouped.get(task.conversation_id) ?? [];
    list.push(task);
    grouped.set(task.conversation_id, list);
  }

  return [...grouped.entries()].map(([conversationId, list]) => {
    const sorted = [...list].sort((left, right) => (
      Date.parse(right.created_at) - Date.parse(left.created_at)
    ));
    return {
      conversationId,
      conversation: conversationById.get(conversationId) ?? null,
      tasks: sorted,
      latestTask: sorted[0],
      status: projectedConversationStatus(sorted),
    };
  }).sort((left, right) => (
    Date.parse(right.latestTask.created_at) - Date.parse(left.latestTask.created_at)
  ));
}
