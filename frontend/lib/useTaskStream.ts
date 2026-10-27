"use client";

// Product AG-UI SSE transport. Event projection lives in the pure chat
// reducer so replay, HITL continuation and rendering can be tested offline.

import { useCallback, useEffect, useRef, useState } from "react";

import { api, type ApiError, getToken, streamUrl } from "./api";
import { chatReducer, initialChatState } from "./chat/reducer";
import {
  acceptsSseEventType,
  normalizeAguiEvent,
} from "./chat/event-normalizer";
import { terminalTaskMismatch } from "./chat/realtime-tasks";
import type { ChatAction } from "./chat/model";
import type { ApprovalProfile, Artifact, PendingConfirm, Task } from "./types";

export type {
  ActivityStep,
  ChatItem,
  ChatReasoningMsg,
  ChatTextMsg,
  ChatToolMsg,
  RunEntry,
  RunState,
} from "./chat/model";

// Product emits a heartbeat every 15 seconds, including while awaiting approval.
const STREAM_IDLE_TIMEOUT_MS = 45_000;

export function useTaskStream(conversationId: string) {
  const stateRef = useRef(initialChatState());
  const [state, setState] = useState(stateRef.current);
  // Commit the pure transition and its acknowledged cursor together. Rendering
  // may be batched, but a reconnect never acknowledges the unprocessed queue.
  const dispatch = useCallback((action: ChatAction) => {
    stateRef.current = chatReducer(stateRef.current, action);
    setState(stateRef.current);
  }, []);
  const recoverRef = useRef<(() => void) | null>(null);
  const taskRequestsRef = useRef(new Set<string>());
  const conversationEpochRef = useRef(0);
  const [approvalSubmitting, setApprovalSubmitting] = useState(false);
  const approvalInFlight = useRef(false);
  const [approvalFailure, setApprovalFailure] = useState<{ version: string; message: string } | null>(null);
  const streamAbortRef = useRef<AbortController | null>(null);
  const eventQueueRef = useRef<Record<string, unknown>[]>([]);
  const flushTimerRef = useRef<number | null>(null);
  const artifactRequestRef = useRef(0);
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  const terminalCheckRef = useRef<number | null>(null);
  const fetchedTasksRef = useRef<Task[]>([]);
  const recoveryAttemptsRef = useRef(new Map<string, number>());

  // REST is a liveness signal, never a partial replacement for the applied cut.
  // Fetch a fresh RESET so messages, tools, approval and artifacts move together.
  const reconcileTasks = useCallback((tasks: Task[]) => {
    fetchedTasksRef.current = tasks;
    if (terminalCheckRef.current !== null) return;
    const check = () => {
      terminalCheckRef.current = null;
      const realtime = stateRef.current.realtime;
      if (!realtime) return;
      const mismatch = terminalTaskMismatch(realtime.tasks, fetchedTasksRef.current, conversationId);
      if (!mismatch) { setRecoveryError(null); return; }
      const key = `${mismatch.id}:${mismatch.status}:${mismatch.updated_at}`;
      const attempts = recoveryAttemptsRef.current.get(key) ?? 0;
      if (attempts >= 3) {
        setRecoveryError("执行结果尚未同步，当前显示可能不完整，请重新同步。");
        return;
      }
      recoveryAttemptsRef.current.set(key, attempts + 1);
      if (!realtime.needsReset) {
        eventQueueRef.current = [];
        if (flushTimerRef.current !== null) window.clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
        conversationEpochRef.current += 1;
        artifactRequestRef.current += 1;
        dispatch({ type: "realtime", name: "invalid", value: null, cursor: "", conversationId });
        recoverRef.current?.();
      }
      // The REST query may stop polling once every Task is terminal. Continue
      // checking boundedly if the projection briefly returned an older cut.
      terminalCheckRef.current = window.setTimeout(check, 15_000);
    };
    check();
  }, [conversationId, dispatch]);

  // A manual task recovery changes the current Runtime Session but keeps the
  // Conversation. Fetch one authoritative cut without clearing its history.
  const refreshConversation = useCallback(() => {
    eventQueueRef.current = [];
    if (flushTimerRef.current !== null) window.clearTimeout(flushTimerRef.current);
    flushTimerRef.current = null;
    conversationEpochRef.current += 1;
    artifactRequestRef.current += 1;
    dispatch({ type: "realtime", name: "invalid", value: null, cursor: "", conversationId });
    recoverRef.current?.();
  }, [conversationId, dispatch]);

  const retryRecovery = useCallback(() => {
    if (terminalCheckRef.current !== null) window.clearTimeout(terminalCheckRef.current);
    terminalCheckRef.current = null;
    recoveryAttemptsRef.current.clear();
    setRecoveryError(null);
    reconcileTasks(fetchedTasksRef.current);
  }, [reconcileTasks]);

  useEffect(() => {
    if (state.realtime && !state.realtime.needsReset
        && !terminalTaskMismatch(state.realtime.tasks, fetchedTasksRef.current, conversationId)) {
      setRecoveryError(null);
    }
  }, [state.realtime?.tasks, state.realtime?.needsReset, conversationId]);

  const hydrateArtifacts = useCallback(async (signal: AbortSignal) => {
    const request = ++artifactRequestRef.current;
    const stale = () => signal.aborted || request !== artifactRequestRef.current;
    const artifacts: Artifact[] = [];
    const limit = 200;
    try {
      for (let offset = 0; !stale(); offset += limit) {
        const page = await api.get<Artifact[]>(
          `/artifacts?conversation_id=${encodeURIComponent(conversationId)}&limit=${limit}&offset=${offset}`,
        );
        // The REST client does not cancel an in-flight request. Discard its
        // response and stop requesting pages when the subscription changes.
        if (stale()) return;
        artifacts.push(...page);
        if (page.length < limit) {
          dispatch({ type: "hydrate-artifacts", artifacts, conversationId });
          return;
        }
      }
    } catch {
      // Live events remain available; a partial history is not marked hydrated.
    }
  }, [conversationId]);

  const flushEvents = useCallback(() => {
    flushTimerRef.current = null;
    const queued = eventQueueRef.current;
    eventQueueRef.current = [];
    for (const event of queued) {
      if (event.type === "CUSTOM" && typeof event.name === "string" && event.name.startsWith("REALTIME_")) {
        dispatch({ type: "realtime", name: event.name, value: event.value,
          cursor: typeof event.__cursor === "string" ? event.__cursor : "", conversationId });
        if (!stateRef.current.realtime?.needsReset && event.name === "REALTIME_RESET") {
          conversationEpochRef.current += 1;
          taskRequestsRef.current.clear();
        }
        if (!stateRef.current.realtime?.needsReset && event.name === "REALTIME_EVENT") {
          const envelope = event.value as {task_id?: string; payload?: {type?:string}};
          const generation=stateRef.current.realtime?.generation;
          if (generation && envelope.payload?.type === "RUN_STARTED" && envelope.task_id
              && !stateRef.current.realtime?.tasks.some(task=>task.id===envelope.task_id)) {
            const taskId=envelope.task_id, key=`${generation}:${taskId}`, epoch=conversationEpochRef.current;
            if (!taskRequestsRef.current.has(key)) {
              taskRequestsRef.current.add(key);
              void api.get<Task>(`/tasks/${encodeURIComponent(taskId)}`).then(task=>{
                if (epoch!==conversationEpochRef.current || stateRef.current.realtime?.generation!==generation
                    || task.id!==taskId || task.conversation_id!==conversationId) return;
                dispatch({type:"record-task",task});
              }).catch(()=>undefined);
            }
          }
        }
        if (stateRef.current.realtime?.needsReset) {
          recoverRef.current?.();
          break;
        }
      } else dispatch({ type: "event", event, conversationId });
    }
  }, [conversationId]);

  const handleEvent = useCallback((event: Record<string, unknown>) => {
    eventQueueRef.current.push(event);
    if (flushTimerRef.current === null) {
      flushTimerRef.current = window.setTimeout(flushEvents, 40);
    }
  }, [flushEvents]);

  useEffect(() => {
    eventQueueRef.current = [];
    if (flushTimerRef.current !== null) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    conversationEpochRef.current += 1;
    taskRequestsRef.current.clear();
    setRecoveryError(null);
    fetchedTasksRef.current = [];
    recoveryAttemptsRef.current.clear();
    dispatch({ type: "reset" });
    return () => {
      if (terminalCheckRef.current !== null) window.clearTimeout(terminalCheckRef.current);
      terminalCheckRef.current = null;
    };
  }, [conversationId]);

  useEffect(() => {
    const controller = new AbortController();
    streamAbortRef.current = controller;
    let lastEventId = "";
    // Cursor namespace of the events already in the reducer. Native Product
    // keeps it stable across API restarts; a legacy buffer may change it and
    // replay from seq 1. Reset only on namespace change to avoid duplicating
    // non-idempotent text deltas while preserving durable reconnect state.
    let currentBoot = "";
    let retryDelay = 1000;
    let clearedInvalidCursor = false;

    const observeBoot = (boot: string | undefined) => {
      if (!boot) return;
      if (currentBoot && boot !== currentBoot) {
        eventQueueRef.current = [];
        if (flushTimerRef.current !== null) {
          window.clearTimeout(flushTimerRef.current);
          flushTimerRef.current = null;
        }
        dispatch({ type: "reset" });
        void hydrateArtifacts(controller.signal);
      }
      currentBoot = boot;
    };

    const waitBeforeReconnect = () =>
      new Promise<void>((resolve) => {
        const finish = () => {
          window.clearTimeout(timer);
          controller.signal.removeEventListener("abort", finish);
          resolve();
        };
        const timer = window.setTimeout(finish, retryDelay);
        controller.signal.addEventListener("abort", finish, { once: true });
      });

    const connect = async () => {
      while (!controller.signal.aborted) {
        // A stalled connection must not abort the Conversation subscription:
        // keep its cursor and open a fresh attempt after the idle deadline.
        const connection = new AbortController();
        let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
        let idleTimer: number | undefined;
        const cancelConnection = () => {
          connection.abort();
          // Also unblock readers whose underlying stream does not relay abort.
          void reader?.cancel().catch(() => undefined);
        };
        const observeTraffic = () => {
          window.clearTimeout(idleTimer);
          idleTimer = window.setTimeout(cancelConnection, STREAM_IDLE_TIMEOUT_MS);
        };
        recoverRef.current = cancelConnection;
        controller.signal.addEventListener("abort", cancelConnection, { once: true });
        observeTraffic();
        try {
          const token = getToken();
          const headers: Record<string, string> = { Accept: "text/event-stream" };
          if (token) headers.Authorization = `Bearer ${token}`;
          const applied = stateRef.current.realtime ? stateRef.current.realtime.cursor : lastEventId;
          if (applied) headers["Last-Event-ID"] = applied;

          const response = await fetch(streamUrl(conversationId), {
            headers,
            cache: "no-store",
            signal: connection.signal,
          });
          if (response.status === 401 || response.status === 403) {
            window.location.href = "/login";
            return;
          }
          if ([404, 413].includes(response.status)) {
            dispatch({ type: "event", event: {type:"RUN_ERROR", message: response.status === 404 ? "会话不可访问" : "会话恢复内容超出限制"}, conversationId });
            return;
          }
          if (response.status === 400 && stateRef.current.realtime && !clearedInvalidCursor) {
            clearedInvalidCursor = true;
            dispatch({type:"realtime",name:"invalid",value:null,cursor:"",conversationId});
          } else if (response.status === 400) {
            dispatch({type:"event",event:{type:"RUN_ERROR",message:"会话恢复失败"},conversationId});
            return;
          }
          if ([409,503].includes(response.status)) retryDelay=Math.min(30000,Math.max(retryDelay*2,Number(response.headers.get("Retry-After")||0)*1000));
          if (!response.ok || !response.body) {
            throw new Error(`SSE connection failed (${response.status})`);
          }

          retryDelay = 1000;
          reader = response.body.getReader();
          observeTraffic();
          const decoder = new TextDecoder();
          let buffer = "";
          const frameBoundary = () => {
            const match = /\r?\n\r?\n/.exec(buffer);
            return match?.index == null
              ? null
              : { index: match.index, length: match[0].length };
          };

          while (!controller.signal.aborted && !connection.signal.aborted) {
            const { done, value } = await reader.read();
            if (done) break;
            // Heartbeat comments count as traffic even though they do not
            // change reducer state or advance Last-Event-ID.
            if (value.byteLength) observeTraffic();
            buffer += decoder.decode(value, { stream: true });
            let boundary = frameBoundary();
            while (boundary) {
              const frame = buffer.slice(0, boundary.index);
              buffer = buffer.slice(boundary.index + boundary.length);
              let eventType = "message";
              let eventId = "";
              const data: string[] = [];
              for (const rawLine of frame.split("\n")) {
                const line = rawLine.endsWith("\r")
                  ? rawLine.slice(0, -1)
                  : rawLine;
                if (line.startsWith(":")) continue;
                if (line.startsWith("event:")) {
                  eventType = line.slice(6).trim();
                } else if (line.startsWith("id:")) {
                  eventId = line.slice(3).trim();
                } else if (line.startsWith("data:")) {
                  data.push(line.slice(5).trimStart());
                }
              }
              if (eventId && !eventId.startsWith("rt1.")) {
                // ``id`` is ``<namespace>:<seq>``. Native API restarts retain
                // this namespace; a changed namespace means a full replay.
                observeBoot(eventId.split(":")[0]);
                lastEventId = eventId;
              }
              // Native Product sends data-only SSE frames (default "message").
              // Both transports still require an accepted AG-UI JSON type.
              if (data.length && (eventType === "message" || acceptsSseEventType(eventType))) {
                try {
                  const event = normalizeAguiEvent(JSON.parse(data.join("\n")));
                  if (event) {
                    // REPLAY_DONE carries no SSE id: after a restart the
                    // replay may be empty (no id-bearing frames), and it
                    // is then the only frame that reveals the new boot.
                    if (
                      event.type === "CUSTOM"
                      && (event as {name?: unknown}).name === "REPLAY_DONE"
                    ) {
                      const value = (event as {value?: {bootId?: unknown}}).value;
                      if (value && typeof value.bootId === "string") {
                        observeBoot(value.bootId);
                      }
                    }
                    handleEvent({ ...event, __cursor: eventId });
                  } else if (eventId.startsWith("rt1.") || stateRef.current.realtime) {
                    dispatch({type:"realtime",name:"invalid",value:null,cursor:"",conversationId});
                    cancelConnection();
                  }
                } catch {
                  // Typed frames cannot acknowledge malformed application data.
                  if (eventId.startsWith("rt1.") || stateRef.current.realtime) {
                    dispatch({ type: "realtime", name: "invalid", value: null, cursor: "", conversationId });
                    cancelConnection();
                  }
                }
              }
              boundary = frameBoundary();
            }
          }
        } catch {
          if (controller.signal.aborted) return;
        } finally {
          window.clearTimeout(idleTimer);
          controller.signal.removeEventListener("abort", cancelConnection);
          cancelConnection();
          reader?.releaseLock();
        }
        if (!controller.signal.aborted) {
          dispatch({ type: "reconnecting" });
          await waitBeforeReconnect();
        }
      }
    };

    void connect();
    return () => {
      controller.abort();
      conversationEpochRef.current += 1;
      eventQueueRef.current = [];
      if (flushTimerRef.current !== null) window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
      streamAbortRef.current = null;
    };
  }, [conversationId, handleEvent, hydrateArtifacts]);

  useEffect(() => {
    const controller = new AbortController();
    void hydrateArtifacts(controller.signal);
    return () => {
      controller.abort();
    };
  }, [hydrateArtifacts]);

  const sendMessage = useCallback(
    async (
      text: string,
      files: File[] = [],
      approvalProfile: ApprovalProfile = "standard",
      onTask?: (task: Task, queued: boolean) => void,
      queueExpected = false,
    ) => {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const assistantId = `assistant-pending-${id}`;
      dispatch({
        type: "optimistic-send",
        id,
        text,
        fileNames: files.map((file) => file.name),
        queued: queueExpected,
      });
      try {
        let response: { task: Task; queued: boolean };
        if (files.length) {
          const form = new FormData();
          form.append("message", text);
          form.append("approval_profile", approvalProfile);
          for (const file of files) form.append("files", file, file.name);
          response = await api.upload<{ task: Task; queued: boolean }>(
            `/tasks/conversations/${conversationId}/tasks-with-files`,
            form,
          );
        } else {
          response = await api.post<{ task: Task; queued: boolean }>(
            `/tasks/conversations/${conversationId}/tasks`,
            { message: text, approval_profile: approvalProfile },
          );
        }
        dispatch({type:"record-task",task:response.task});
        dispatch({
          type: "task-accepted",
          assistantId,
          taskId: response.task.id,
          queued: response.queued,
        });
        onTask?.(response.task, response.queued);
        return response;
      } catch (error) {
        dispatch({
          type: "send-failed",
          assistantId,
          message: error instanceof Error ? error.message : "发送失败",
          queued: queueExpected,
        });
        throw error;
      }
    },
    [conversationId],
  );

  const confirm = useCallback(async (
    taskId: string,
    decisions: { tool_call_id: string; approved: boolean }[],
  ) => {
    const snapshot = state.pendingConfirm;
    if (approvalInFlight.current || !snapshot) return;
    approvalInFlight.current = true;
    setApprovalSubmitting(true);
    setApprovalFailure(null);
    try {
      await api.post(`/tasks/${taskId}/confirm`, {
        decisions,
        decision_version: snapshot?.decisionVersion
          ?? `ui-${taskId}-${decisions.map((item) => item.tool_call_id).join("-")}`.slice(0, 128),
      });
      dispatch({ type: "clear-confirm", decisionVersion: snapshot.decisionVersion });
    } catch (error) {
      const apiError = error as Partial<ApiError>;
      if (apiError.code !== "task_not_awaiting_permission") {
        setApprovalFailure({
          version: snapshot.decisionVersion,
          message: error instanceof Error ? error.message : "审批提交失败，请重试",
        });
      } else {
        dispatch({ type: "clear-confirm", decisionVersion: snapshot.decisionVersion });
      }
      throw error;
    } finally {
      approvalInFlight.current = false;
      setApprovalSubmitting(false);
    }
  }, [approvalSubmitting, state.pendingConfirm]);

  const enableFullAccess = useCallback(async (
    taskId: string,
    decisions: { tool_call_id: string; approved: boolean }[],
  ) => {
    const snapshot = state.pendingConfirm;
    if (approvalInFlight.current || !snapshot) return;
    approvalInFlight.current = true;
    setApprovalSubmitting(true);
    setApprovalFailure(null);
    try {
      await api.post(`/tasks/${taskId}/full-access`, {
        decisions,
        decision_version: snapshot?.decisionVersion
          ?? `ui-full-${taskId}-${decisions.map((item) => item.tool_call_id).join("-")}`.slice(0, 128),
      });
      dispatch({ type: "clear-confirm", decisionVersion: snapshot.decisionVersion });
    } catch (error) {
      const apiError = error as Partial<ApiError>;
      if (apiError.code !== "task_not_awaiting_permission") {
        setApprovalFailure({
          version: snapshot.decisionVersion,
          message: error instanceof Error ? error.message : "完全访问开启失败，请重试",
        });
      } else {
        dispatch({ type: "clear-confirm", decisionVersion: snapshot.decisionVersion });
      }
      throw error;
    } finally {
      approvalInFlight.current = false;
      setApprovalSubmitting(false);
    }
  }, [approvalSubmitting, state.pendingConfirm]);

  const stop = useCallback(async (taskId: string) => {
    await api.post(`/tasks/${taskId}/stop`);
  }, []);

  const hydratePendingConfirm = useCallback((pending: PendingConfirm) => {
    dispatch({ type: "hydrate-confirm", pending });
  }, []);

  return {
    realtime: state.realtime,
    knowledgeSources: state.knowledgeSources,
    knowledgeProgress: state.knowledgeProgress,
    items: state.items,
    run: state.run,
    runs: state.runs,
    pendingConfirm: state.pendingConfirm,
    approvalSubmitting,
    approvalError: approvalFailure?.version === state.pendingConfirm?.decisionVersion ? approvalFailure?.message : null,
    artifacts: state.artifacts,
    artifactsHydratedFor: state.artifactsHydratedFor,
    steps: state.steps,
    sendMessage,
    confirm,
    enableFullAccess,
    stop,
    hydratePendingConfirm,
    reconcileTasks,
    recoveryError,
    retryRecovery,
    refreshConversation,
  };
}
