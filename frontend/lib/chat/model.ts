import type { Artifact, PendingConfirm, ToolCallState } from "../types";

export interface ChatTextMsg {
  /** Confirmed missing content; independent of task execution status. */
  incomplete?: boolean;
  presentation?: { version: number; process_end: number };
  kind: "text";
  id: string;
  role: string;
  text: string;
  done: boolean;
  /** Product Task / AgentScope Reply association when it is known. */
  taskId?: string;
  runId?: string | null;
}

export interface ToolContentReference {
  sha256: string;
  sizeBytes: number;
  preview: string;
  incomplete: boolean;
}

export interface ChatToolMsg {
  taskId?: string;
  content?: Partial<Record<"arguments" | "result" | "metadata", ToolContentReference>>;
  kind: "tool";
  id: string;
  call: ToolCallState;
  /** Run (reply) this call belongs to; groups calls into per-run
   * summaries in the message stream. */
  runId?: string | null;
}

export interface ChatReasoningMsg {
  kind: "reasoning";
  id: string;
  text: string;
  done: boolean;
}

export type ChatItem = ChatTextMsg | ChatToolMsg | ChatReasoningMsg;

export interface RunState {
  active: boolean;
  status: string | null;
  error: string | null;
  runId: string | null;
}

/** One executed run (AgentScope reply) of the live stream. */
export interface RunEntry {
  optimisticId?: string;
  id: string;
  status: string;
  error: string | null;
  /** Product Task identity while a Reply id does not exist yet. */
  taskId?: string;
}

export interface ActivityStep {
  id: string;
  name: string;
  state: "running" | "completed" | "error";
}

export interface ChatState {
  realtime?: import("./realtime-reducer").RealtimeState;
  knowledgeProgress: Record<string, import("../knowledge").KnowledgeProgress>;
  knowledgeSources: import("../knowledge").TaskKnowledgeSource[];
  items: ChatItem[];
  run: RunState;
  runs: RunEntry[];
  /** decisionVersions already answered; replayed requests are ignored. */
  decidedConfirmVersions: string[];
  /** Explicit per-call decisions for a durable request, including partial approval. */
  decidedConfirmTools: Record<string, string[]>;
  pendingConfirm: PendingConfirm | null;
  artifacts: Artifact[];
  /** Conversation whose initial REST artifact hydration has finished. */
  artifactsHydratedFor: string | null;
  steps: ActivityStep[];
  pendingAssistantId: string | null;
}

export type ChatAction =
  | { type: "record-task"; task: import("../types").Task }
  | { type: "realtime"; name: string; value: unknown; cursor: string; conversationId: string }
  | { type: "reset" }
  | {
      type: "event";
      event: Record<string, unknown>;
      conversationId: string;
    }
  | {
      type: "optimistic-send";
      id: string;
      text: string;
      fileNames: string[];
      queued?: boolean;
    }
  | {
      type: "task-accepted";
      assistantId: string;
      taskId: string;
      queued: boolean;
    }
  | { type: "send-failed"; assistantId: string; message: string; queued?: boolean }
  | { type: "reconnecting" }
  | { type: "hydrate-confirm"; pending: PendingConfirm }
  | { type: "clear-confirm"; decisionVersion?: string }
  | { type: "restore-confirm"; pending: PendingConfirm | null; message: string }
  | { type: "hydrate-artifacts"; artifacts: Artifact[]; conversationId: string };
