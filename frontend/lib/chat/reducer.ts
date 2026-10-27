import { taskControlStatus } from "./realtime-tasks";
import { reduceRealtime } from "./realtime-reducer";
import { parseKnowledgeSources } from "../knowledge";
import type { Artifact, PendingConfirm } from "../types";
import type {
  ActivityStep,
  ChatAction,
  ChatItem,
  ChatState,
  ChatTextMsg,
  RunEntry,
} from "./model";

export function initialChatState(): ChatState {
  return {
    knowledgeSources: [],
    knowledgeProgress: {},
    items: [],
    run: { active: false, status: null, error: null, runId: null },
    runs: [],
    decidedConfirmVersions: [],
    decidedConfirmTools: {},
    pendingConfirm: null,
    artifacts: [],
    artifactsHydratedFor: null,
    steps: [],
    pendingAssistantId: null,
  };
}


function remainingConfirm(state: ChatState, pending: PendingConfirm): PendingConfirm | null {
  if (state.decidedConfirmVersions.includes(pending.decisionVersion)) return null;
  const decided = state.decidedConfirmTools[pending.decisionVersion] ?? [];
  const toolCalls = pending.toolCalls.filter(call => !decided.includes(call.id));
  return toolCalls.length ? { ...pending, toolCalls } : null;
}

function updateLastRun(
  runs: RunEntry[],
  update: Partial<RunEntry>,
): RunEntry[] {
  if (!runs.length) return runs;
  const last = runs.at(-1)!;
  return [...runs.slice(0, -1), { ...last, ...update }];
}

function currentRun(state: ChatState): RunEntry | undefined {
  return state.run.runId
    ? state.runs.find((run) => run.id === state.run.runId)
    : state.runs.findLast((run) => run.status !== "queued");
}

function updateCurrentRun(state: ChatState, update: Partial<RunEntry>): RunEntry[] {
  const current = currentRun(state);
  return state.runs.map((run) => run === current ? { ...run, ...update } : run);
}

function finishCurrentMessages(state: ChatState, incomplete = false) {
  const current = currentRun(state);
  let pendingAssistantId = state.pendingAssistantId;
  const items = state.items.flatMap((item): ChatItem[] => {
    if (item.kind === "tool") {
      const owned = item.taskId && current?.taskId ? item.taskId === current.taskId
        : item.runId != null && item.runId === state.run.runId;
      return [owned && item.call.state === "running" ? { ...item, call: { ...item.call, state: "unknown" } } : item];
    }
    if (item.kind !== "text" || item.role !== "assistant") return [item];
    const owned = item.taskId && current?.taskId
      ? item.taskId === current.taskId
      : item.runId
        ? item.runId === state.run.runId
        : item.id === current?.optimisticId || item.id === current?.id;
    if (!owned) return [item];
    if (item.id === pendingAssistantId) pendingAssistantId = null;
    // A terminal Reply cannot keep an empty "thinking" bubble. Retain any
    // partial answer as finished even when TEXT_MESSAGE_END never arrived.
    return item.text.trim() ? [{ ...item, done: true, ...(incomplete ? { incomplete: true } : {}) }] : [];
  });
  return { items, pendingAssistantId };
}

function updateItem(
  items: ChatItem[],
  predicate: (item: ChatItem) => boolean,
  update: (item: ChatItem) => ChatItem,
): ChatItem[] {
  let changed = false;
  const next = items.map((item) => {
    if (!predicate(item)) return item;
    changed = true;
    return update(item);
  });
  return changed ? next : items;
}

function appendText(
  state: ChatState,
  messageId: string,
  delta: string,
): ChatState {
  let found = false;
  const items = updateItem(
    state.items,
    (item) => item.kind === "text" && item.id === messageId,
    (item) => {
      found = true;
      return item.kind === "text"
        ? { ...item, text: item.text + delta }
        : item;
    },
  );
  if (found) return { ...state, items };
  return {
    ...state,
    items: [
      ...state.items,
      {
        kind: "text",
        id: messageId,
        role: "assistant",
        text: delta,
        done: false,
        runId: state.run.runId,
        taskId: currentRun(state)?.taskId,
      },
    ],
  };
}

function upsertArtifact(artifacts: Artifact[], artifact: Artifact): Artifact[] {
  const index = artifacts.findIndex((candidate) => candidate.id === artifact.id && candidate.task_id === artifact.task_id);
  if (index < 0) return [...artifacts, artifact];
  const next = [...artifacts];
  next[index] = artifact;
  return next;
}

function finishStep(steps: ActivityStep[], name: string): ActivityStep[] {
  const index = steps.findLastIndex(
    (step) => step.name === name && step.state === "running",
  );
  if (index < 0) return steps;
  const next = [...steps];
  next[index] = { ...next[index], state: "completed" };
  return next;
}

function reduceEvent(
  state: ChatState,
  event: Record<string, unknown>,
  conversationId: string,
): ChatState {
  const type = String(event.type ?? "");
  switch (type) {
    case "RUN_STARTED": {
      const runId = typeof event.runId === "string" ? event.runId : state.run.runId;
      const taskId = typeof event.taskId === "string" ? event.taskId : undefined;
      const continuation = Boolean(runId && runId === state.run.runId);
      let runs = state.runs;
      if (!continuation) {
        const pending = taskId
          ? state.runs.find((run) => run.taskId === taskId)
          : state.runs.find((run) => ["starting", "queued"].includes(run.status));
        if (pending) {
          // Native identity is authoritative across tabs and HTTP/SSE races.
          // FIFO remains only for older events without a Product Task id.
          runs = state.runs.map((run) => run === pending
            ? { ...run, id: runId ?? run.id, status: "running" } : run);
        } else {
          runs = [
            ...state.runs,
            { id: runId ?? `run-${state.runs.length}`, taskId, status: "running", error: null },
          ];
        }
      }
      return {
        ...state,
        run: { active: true, status: null, error: null, runId: runId ?? null },
        runs,
        pendingConfirm: continuation ? state.pendingConfirm : null,
        steps: continuation ? state.steps : [],
      };
    }
    case "TEXT_MESSAGE_START": {
      const messageId = String(event.messageId ?? "");
      if (!messageId) return state;
      if (state.items.some((item) => item.kind === "text" && item.id === messageId)) {
        return state;
      }
      const activeTaskId = currentRun(state)?.taskId;
      const placeholder = state.items.find((item) => item.id === state.pendingAssistantId);
      const matchesTask = !activeTaskId || (placeholder?.kind === "text" && placeholder.taskId === activeTaskId);
      if (state.pendingAssistantId && matchesTask) {
        return {
          ...state,
          pendingAssistantId: null,
          items: updateItem(
            state.items,
            (item) => item.kind === "text" && item.id === state.pendingAssistantId,
            (item) => item.kind === "text"
              ? {
                  ...item,
                  id: messageId,
                  runId: state.run.runId,
                  taskId: item.taskId ?? currentRun(state)?.taskId,
                }
              : item,
          ),
        };
      }
      return {
        ...state,
        items: [
          ...state.items,
          {
            kind: "text",
            id: messageId,
            role: "assistant",
            text: "",
            done: false,
            runId: state.run.runId,
            taskId: currentRun(state)?.taskId,
          },
        ],
      };
    }
    case "TEXT_MESSAGE_CONTENT":
      return appendText(
        state,
        String(event.messageId ?? ""),
        String(event.delta ?? ""),
      );
    case "TEXT_MESSAGE_END": {
      const messageId = String(event.messageId ?? "");
      return {
        ...state,
        items: updateItem(
          state.items,
          (item) => item.kind === "text" && item.id === messageId,
          (item) => item.kind === "text" ? { ...item, done: true } : item,
        ),
      };
    }
    case "TOOL_CALL_START": {
      const id = String(event.toolCallId ?? "");
      if (!id || state.items.some((item) => item.kind === "tool" && item.id === id)) {
        return state;
      }
      return {
        ...state,
        items: [
          ...state.items,
          {
            kind: "tool",
            id,
            taskId: typeof event.taskId === "string" ? event.taskId : currentRun(state)?.taskId,
            runId: typeof event.parentMessageId === "string"
              ? event.parentMessageId : state.run.runId,
            call: {
              toolCallId: id,
              toolCallName: String(event.toolCallName ?? ""),
              args: "",
              result: "",
              state: "running",
            },
          },
        ],
      };
    }
    case "TOOL_CALL_ARGS": {
      const id = String(event.toolCallId ?? "");
      return {
        ...state,
        items: updateItem(
          state.items,
          (item) => item.kind === "tool" && item.id === id,
          (item) => item.kind === "tool"
            ? { ...item, call: { ...item.call, args: item.call.args + String(event.delta ?? "") } }
            : item,
        ),
      };
    }
    case "TOOL_CALL_RESULT": {
      const id = String(event.toolCallId ?? "");
      return {
        ...state,
        items: updateItem(
          state.items,
          (item) => item.kind === "tool" && item.id === id,
          (item) => item.kind === "tool"
            ? {
                ...item,
                call: {
                  ...item.call,
                  result: String(event.content ?? ""),
                  state: "success",
                },
              }
            : item,
        ),
      };
    }
    case "REASONING_MESSAGE_START": {
      const id = String(event.messageId ?? "");
      if (!id || state.items.some((item) => item.kind === "reasoning" && item.id === id)) {
        return state;
      }
      return {
        ...state,
        items: [...state.items, { kind: "reasoning", id, text: "", done: false }],
      };
    }
    case "REASONING_MESSAGE_CONTENT": {
      const id = String(event.messageId ?? "");
      const existing = state.items.some(
        (item) => item.kind === "reasoning" && item.id === id,
      );
      if (!existing) {
        return {
          ...state,
          items: [
            ...state.items,
            { kind: "reasoning", id, text: String(event.delta ?? ""), done: false },
          ],
        };
      }
      return {
        ...state,
        items: updateItem(
          state.items,
          (item) => item.kind === "reasoning" && item.id === id,
          (item) => item.kind === "reasoning"
            ? { ...item, text: item.text + String(event.delta ?? "") }
            : item,
        ),
      };
    }
    case "REASONING_MESSAGE_END": {
      const id = String(event.messageId ?? "");
      return {
        ...state,
        items: updateItem(
          state.items,
          (item) => item.kind === "reasoning" && item.id === id,
          (item) => item.kind === "reasoning" ? { ...item, done: true } : item,
        ),
      };
    }
    case "STEP_STARTED": {
      const name = String(event.stepName ?? "执行步骤");
      const id = `step-${state.steps.length}-${name}`;
      return { ...state, steps: [...state.steps, { id, name, state: "running" }] };
    }
    case "STEP_FINISHED":
      return {
        ...state,
        steps: finishStep(state.steps, String(event.stepName ?? "执行步骤")),
      };
    case "CUSTOM": {
      const name = String(event.name ?? "");
      const value = (event.value ?? {}) as Record<string, unknown>;
      if (name === "TOOL_CONTENT_AVAILABLE") {
        const kind = value.kind;
        if ((kind !== "arguments" && kind !== "result" && kind !== "metadata") || typeof value.toolCallId !== "string"
          || typeof value.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256)
          || typeof value.sizeBytes !== "number" || !Number.isSafeInteger(value.sizeBytes) || value.sizeBytes < 0
          || typeof value.preview !== "string" || typeof value.incomplete !== "boolean") return state;
        const ref = { sha256: value.sha256, sizeBytes: value.sizeBytes, preview: value.preview, incomplete: value.incomplete };
        return { ...state, items: updateItem(state.items,
          item => item.kind === "tool" && item.id === value.toolCallId,
          item => item.kind === "tool" ? { ...item,
            taskId: String(event.taskId ?? value.taskId ?? item.taskId ?? ""),
            content: { ...item.content, [kind]: ref },
            call: kind === "metadata" ? item.call : { ...item.call, [kind === "arguments" ? "args" : "result"]: ref.preview },
          } : item) };
      }
      const controlStatus = taskControlStatus(name, value);
      if (controlStatus) {
        const taskId = String(event.taskId ?? value.taskId ?? "");
        const target = state.runs.find(run => run.taskId === taskId);
        const runId = typeof event.runId === "string" ? event.runId : target?.id ?? state.run.runId;
        return { ...state,
          run: { ...state.run, active: controlStatus !== "cancelled", status: controlStatus, runId },
          runs: state.runs.map(run => run.taskId === taskId ? { ...run, status: controlStatus } : run),
          pendingConfirm: state.pendingConfirm?.taskId === taskId ? null : state.pendingConfirm,
        };
      }
      if (name === "knowledge_status") {
        const runId = typeof event.runId === "string" ? event.runId : "";
        if (!runId) return state;
        return { ...state, knowledgeProgress: { ...state.knowledgeProgress, [runId]: {
          status: String(value.status ?? ""), search_count: Number(value.search_count ?? 0),
          failure_count: Number(value.failure_count ?? 0),
        } } };
      }
      if (name === "knowledge_sources") {
        const runId = event.runId ? String(event.runId) : state.run.runId;
        const sourceRun = runId ? state.runs.find((run) => run.id === runId) : currentRun(state);
        const taskId = String(event.taskId ?? value.task_id ?? value.taskId ?? sourceRun?.taskId ?? "");
        const sources = parseKnowledgeSources(value.sources).map(source => ({taskId, runId, source}));
        const merged = new Map(state.knowledgeSources.map(item => [`${item.taskId}:${item.runId}:${item.source.source_id}`, item]));
        for (const item of sources) merged.set(`${item.taskId}:${item.runId}:${item.source.source_id}`, item);
        return {...state, knowledgeSources: [...merged.values()]};
      }
      if (name === "REASONING_DELTA") {
        const last = state.items.at(-1);
        if (last?.kind === "reasoning") {
          return {
            ...state,
            items: [
              ...state.items.slice(0, -1),
              { ...last, text: last.text + String(value.delta ?? "") },
            ],
          };
        }
        return {
          ...state,
          items: [
            ...state.items,
            {
              kind: "reasoning",
              id: `reason-${state.items.length}`,
              text: String(value.delta ?? ""),
              done: false,
            },
          ],
        };
      }
      if (name === "TOOL_RESULT_DELTA" || name === "TOOL_RESULT_END") {
        const id = String(value.toolCallId ?? "");
        return {
          ...state,
          items: updateItem(
            state.items,
            (item) => item.kind === "tool" && item.id === id,
            (item) => item.kind === "tool"
              ? {
                  ...item,
                  call: {
                    ...item.call,
                    result: name === "TOOL_RESULT_DELTA"
                      ? item.call.result + String(value.delta ?? "")
                      : item.call.result,
                    state: name === "TOOL_RESULT_END"
                      ? String(value.state ?? "success")
                      : item.call.state,
                  },
                }
              : item,
          ),
        };
      }
      if (name === "HITL_APPROVAL_REQUEST") {
        // A replayed request that was already answered must not bring the
        // approval card back after a page refresh.
        const pendingConfirm = remainingConfirm(state, value as unknown as PendingConfirm);
        return pendingConfirm ? { ...state, pendingConfirm } : state;
      }
      if (name === "HITL_DECISION_RECORDED") {
        const version = String(value.decisionVersion ?? "");
        if (!version) return state;
        if (Array.isArray(value.decidedToolCallIds)) {
          const ids = value.decidedToolCallIds;
          if (!ids.every(id => typeof id === "string")) return state;
          const next = { ...state, decidedConfirmTools: {
            ...state.decidedConfirmTools,
            [version]: [...new Set([...(state.decidedConfirmTools[version] ?? []), ...ids])],
          } };
          return { ...next, pendingConfirm: state.pendingConfirm
            ? remainingConfirm(next, state.pendingConfirm) : null };
        }
        const decidedConfirmVersions = state.decidedConfirmVersions.includes(version)
          ? state.decidedConfirmVersions
          : [...state.decidedConfirmVersions, version];
        const pendingConfirm = state.pendingConfirm?.decisionVersion === version
          ? null
          : state.pendingConfirm;
        return { ...state, decidedConfirmVersions, pendingConfirm };
      }
      if (name === "ARTIFACT_CREATED") {
        const artifact: Artifact = {
          id: String(value.artifactId ?? ""),
          task_id: String(value.taskId ?? ""),
          conversation_id: conversationId,
          name: String(value.name ?? ""),
          mime_type: String(value.mimeType ?? ""),
          size: Number(value.size ?? 0),
          version: Number(value.version ?? 1),
          change_type: String(value.changeType ?? "created"),
          metadata: {},
          created_at: typeof value.createdAt === "string"
            ? value.createdAt
            : new Date().toISOString(),
        };
        return artifact.id
          ? { ...state, artifacts: upsertArtifact(state.artifacts, artifact) }
          : state;
      }
      return state;
    }
    case "RUN_FINISHED":
    case "RUN_CANCELLED": {
      if (typeof event.runId === "string" && state.run.runId && event.runId !== state.run.runId) return state;
      return {
        ...state,
        ...finishCurrentMessages(state),
        run: { ...state.run, active: false, status: type, error: null },
        runs: updateCurrentRun(state, { status: type, error: null }),
        pendingConfirm: null,
      };
    }
    case "RUN_ERROR": {
      const uncertain = event.code === "runtime_execution_quarantined";
      const message = String(event.message ?? "run failed");
      const eventRunId = typeof event.runId === "string" ? event.runId : null;
      // A replayed error frame (e.g. a stream disconnection that the
      // buffer replays on every refresh) must not pose as the failure
      // of the run currently on screen.
      if (eventRunId && state.run.runId && eventRunId !== state.run.runId) {
        return state;
      }
      return {
        ...state,
        ...finishCurrentMessages(state, uncertain),
        pendingConfirm: uncertain ? null : state.pendingConfirm,
        run: {
          ...state.run,
          active: false,
          status: type,
          error: message,
        },
        runs: updateCurrentRun(state, {
          status: type,
          error: message,
        }),
      };
    }
    default:
      return state;
  }
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  if (action.type === "record-task") {
    if (!state.realtime) return state;
    const known = state.realtime.tasks.find(task => task.id === action.task.id);
    const live = state.runs.find(run => run.taskId === action.task.id);
    const statuses: Record<string, import("../types").TaskStatus> = {
      running: "running", awaiting_permission: "awaiting_permission", cancelling: "cancelling",
      RUN_FINISHED: "completed", RUN_CANCELLED: "cancelled", RUN_ERROR: "failed",
    };
    const task = { ...action.task, status: known?.status ?? statuses[live?.status ?? ""] ?? action.task.status,
      run_id: known?.run_id ?? (live && !live.id.startsWith("assistant-pending-") && live.id !== action.task.id ? live.id : action.task.run_id) };
    return { ...state, realtime: { ...state.realtime, tasks: [task, ...state.realtime.tasks.filter(t => t.id !== task.id)] } };
  }
  if (action.type === "realtime") return reduceRealtime(state, action.name, action.value, action.cursor, action.conversationId, reduceEvent, initialChatState);
  if (state.realtime && ["hydrate-confirm", "hydrate-artifacts"].includes(action.type)) return state;
  switch (action.type) {
    case "reset":
      return initialChatState();
    case "event":
      return reduceEvent(state, action.event, action.conversationId);
    case "optimistic-send": {
      const assistantId = `assistant-pending-${action.id}`;
      const attachmentText = action.fileNames.map((name) => `📎 ${name}`).join("\n");
      const text = [action.text || (action.fileNames.length ? "请处理这些附件" : ""), attachmentText]
        .filter(Boolean)
        .join("\n\n");
      const userMessage: ChatTextMsg = {
        kind: "text", id: `user-${action.id}`, role: "user", text, done: true,
      };
      if (action.queued) {
        return { ...state, items: [...state.items, userMessage] };
      }
      const optimistic: ChatTextMsg[] = [
        userMessage,
        { kind: "text", id: assistantId, role: "assistant", text: "", done: false },
      ];
      return {
        ...state,
        items: [...state.items, ...optimistic],
        pendingAssistantId: assistantId,
        run: { active: true, status: "starting", error: null, runId: null },
        runs: [
          ...state.runs,
          { id: assistantId, optimisticId: assistantId, status: "starting", error: null },
        ],
      };
    }
    case "task-accepted": {
      const index = state.runs.findLastIndex(
        (candidate) => candidate.id === action.assistantId || candidate.optimisticId === action.assistantId,
      );
      const started = state.runs.find((candidate) => candidate.taskId === action.taskId);
      const acceptedRun: RunEntry = {
        id: action.queued ? action.taskId : action.assistantId,
        taskId: action.taskId,
        status: action.queued ? "queued" : "starting",
        error: null,
      };
      const runs = [...state.runs];
      if (started) {
        // SSE may have already started or completed this Task before POST
        // returns its original queued receipt. Keep the authoritative Reply.
        if (index >= 0 && runs[index] !== started) runs.splice(index, 1);
      } else if (index >= 0) runs[index] = action.queued ? acceptedRun : { ...runs[index], taskId: action.taskId };
      else if (action.queued) runs.push(acceptedRun);

      let items = updateItem(
        state.items,
        (item) => item.id === action.assistantId
          || item.id === action.assistantId.replace("assistant-pending-", "user-"),
        (item) => item.kind === "text" ? { ...item, taskId: action.taskId } : item,
      );
      if (state.realtime) {
        const localUserId = action.assistantId.replace("assistant-pending-", "user-");
        if (items.some(item => item.kind === "text" && item.role === "user" && item.taskId === action.taskId && item.id !== localUserId)) {
          items = items.filter(item => item.id !== localUserId);
        }
      }
      if (started) items = items.filter((item) => item.id !== action.assistantId);
      if (!action.queued) return { ...state, runs, items,
        pendingAssistantId: started && state.pendingAssistantId === action.assistantId ? null : state.pendingAssistantId };
      const ownsOptimisticRun = state.pendingAssistantId === action.assistantId;
      return {
        ...state,
        items: items.filter((item) => item.id !== action.assistantId),
        pendingAssistantId: ownsOptimisticRun ? null : state.pendingAssistantId,
        run: ownsOptimisticRun && !started
          ? { active: false, status: "queued", error: null, runId: null }
          : state.run,
        runs,
      };
    }
    case "send-failed":
      if (action.queued) {
        return {
          ...state,
          items: state.items.filter(
            (item) => item.id !== action.assistantId.replace(
              "assistant-pending-", "user-",
            ),
          ),
        };
      }
      return {
        ...state,
        items: state.items.filter(
          (item) => item.id !== action.assistantId
            && item.id !== action.assistantId.replace("assistant-pending-", "user-"),
        ),
        pendingAssistantId: null,
        run: { ...state.run, active: false, status: "RUN_ERROR", error: action.message },
        runs: updateLastRun(state.runs, {
          status: "RUN_ERROR",
          error: action.message,
        }),
      };
    case "reconnecting":
      return state.run.active
        ? { ...state, run: { ...state.run, status: "reconnecting" } }
        : state;
    case "hydrate-confirm":
      if (
        state.pendingConfirm
        || state.decidedConfirmVersions.includes(action.pending.decisionVersion)
        || ["RUN_FINISHED", "RUN_CANCELLED"].includes(state.run.status ?? "")
      ) return state;
      return { ...state, pendingConfirm: remainingConfirm(state, action.pending) };
    case "clear-confirm":
      return { ...state,
        pendingConfirm: !action.decisionVersion || state.pendingConfirm?.decisionVersion === action.decisionVersion ? null : state.pendingConfirm,
        decidedConfirmVersions: action.decisionVersion && !state.decidedConfirmVersions.includes(action.decisionVersion) ? [...state.decidedConfirmVersions, action.decisionVersion] : state.decidedConfirmVersions,
      };
    case "restore-confirm":
      if (
        !action.pending
        || state.decidedConfirmVersions.includes(action.pending.decisionVersion)
        || ["RUN_FINISHED", "RUN_CANCELLED"].includes(state.run.status ?? "")
      ) return state;
      return {
        ...state,
        pendingConfirm: remainingConfirm(state, action.pending),
        run: { ...state.run, status: "RUN_ERROR", error: action.message },
      };
    case "hydrate-artifacts":
      return {
        ...state,
        artifacts: action.artifacts.reduce(upsertArtifact, state.artifacts),
        artifactsHydratedFor: action.conversationId,
      };
  }
}
