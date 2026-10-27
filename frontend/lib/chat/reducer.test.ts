import { describe, expect, it } from "vitest";

import { chatReducer, initialChatState } from "./reducer";

function event(
  state: ReturnType<typeof initialChatState>,
  value: Record<string, unknown>,
) {
  return chatReducer(state, {
    type: "event",
    conversationId: "conversation-1",
    event: value,
  });
}

describe("chatReducer", () => {
  function runningWithQueue() {
    let state = chatReducer(initialChatState(), { type: "optimistic-send", id: "a", text: "A", fileNames: [] });
    state = chatReducer(state, { type: "task-accepted", assistantId: "assistant-pending-a", taskId: "task-a", queued: false });
    state = event(state, { type: "RUN_STARTED", runId: "reply-a" });
    for (const id of ["b", "c"]) {
      state = chatReducer(state, { type: "optimistic-send", id, text: id, fileNames: [], queued: true });
      state = chatReducer(state, { type: "task-accepted", assistantId: `assistant-pending-${id}`, taskId: `task-${id}`, queued: true });
    }
    return state;
  }

  it.each(["RUN_FINISHED", "RUN_CANCELLED", "RUN_ERROR"])("%s updates the executing Reply rather than a later queued Task", (type) => {
    const state = event(runningWithQueue(), { type, runId: "reply-a", message: "failed" });
    expect(state.runs[0].status).toBe(type);
    expect(state.runs.slice(1).map((run) => run.status)).toEqual(["queued", "queued"]);
  });

  it("same-Reply continuation keeps queued tasks and attributes subsequent text to the running task", () => {
    let state = event(runningWithQueue(), { type: "RUN_STARTED", runId: "reply-a" });
    state = event(state, { type: "TEXT_MESSAGE_START", messageId: "continued" });
    state = event(state, { type: "TEXT_MESSAGE_CONTENT", messageId: "continued", delta: "A answer" });
    expect(state.items.find((item) => item.id === "continued")).toMatchObject({ taskId: "task-a", runId: "reply-a" });
    state = event(state, { type: "TEXT_MESSAGE_START", messageId: "next-block" });
    state = event(state, { type: "TEXT_MESSAGE_CONTENT", messageId: "missing-start", delta: "another A block" });
    for (const id of ["next-block", "missing-start"]) {
      expect(state.items.find((item) => item.id === id)).toMatchObject({ taskId: "task-a", runId: "reply-a" });
    }
    expect(state.runs.slice(1).map((run) => run.status)).toEqual(["queued", "queued"]);
  });

  it("the next Reply consumes the oldest queued task after the active Reply finishes", () => {
    let state = event(runningWithQueue(), { type: "RUN_FINISHED", runId: "reply-a" });
    state = event(state, { type: "RUN_STARTED", runId: "reply-b" });
    expect(state.runs[1]).toMatchObject({ id: "reply-b", taskId: "task-b", status: "running" });
    expect(state.runs[2]).toMatchObject({ id: "task-c", status: "queued" });
  });

  it.each(["RUN_CANCELLED", "RUN_ERROR", "RUN_FINISHED"])("%s removes the empty assistant of A while queued B completes normally", (type) => {
    let state = runningWithQueue();
    // Cleanup must use Task/Reply ownership even if another transition cleared
    // the single optimistic pointer before the delayed terminal event.
    state = { ...state, pendingAssistantId: null };
    state = event(state, { type, runId: "reply-a", message: "interrupted" });
    expect(state.items.some((item) => item.id === "assistant-pending-a")).toBe(false);
    expect(state.items.some((item) => item.id === "user-a")).toBe(true);
    state = event(state, { type: "RUN_STARTED", runId: "reply-b", taskId: "task-b" });
    state = event(state, { type: "TEXT_MESSAGE_START", messageId: "answer-b" });
    state = event(state, { type: "TEXT_MESSAGE_CONTENT", messageId: "answer-b", delta: "B completed" });
    state = event(state, { type: "RUN_FINISHED", runId: "reply-b" });
    expect(state.items.filter((item) => item.kind === "text" && item.role === "assistant" && !item.done)).toEqual([]);
    expect(state.items.find((item) => item.id === "answer-b")).toMatchObject({ text: "B completed", done: true });
  });

  it("terminal cleanup preserves a different task's empty optimistic assistant", () => {
    let state = chatReducer(initialChatState(), { type: "optimistic-send", id: "local", text: "local", fileNames: [] });
    state = chatReducer(state, { type: "task-accepted", assistantId: "assistant-pending-local", taskId: "task-local", queued: false });
    state = event(state, { type: "RUN_STARTED", runId: "reply-other", taskId: "task-other" });
    state = event(state, { type: "RUN_CANCELLED", runId: "reply-other" });
    expect(state.items.find((item) => item.id === "assistant-pending-local")).toMatchObject({ done: false, taskId: "task-local" });
    expect(state.pendingAssistantId).toBe("assistant-pending-local");
  });

  it("Stop closes a partial answer and clears only its optimistic pointer", () => {
    let state = runningWithQueue();
    state = event(state, { type: "RUN_CANCELLED", runId: "reply-a" });
    expect(state.pendingAssistantId).toBeNull();
    state = event(state, { type: "RUN_STARTED", runId: "reply-b", taskId: "task-b" });
    state = event(state, { type: "TEXT_MESSAGE_CONTENT", messageId: "partial-b", delta: "partial answer" });
    state = event(state, { type: "RUN_CANCELLED", runId: "reply-b" });
    expect(state.items.find((item) => item.id === "partial-b")).toMatchObject({ text: "partial answer", done: true });
    expect(state.runs[2].status).toBe("queued");
  });

  it("native task identity prevents another tab's Reply from consuming a local queued task", () => {
    const state = event(runningWithQueue(), { type: "RUN_STARTED", runId: "reply-other", taskId: "task-other" });
    expect(state.runs.filter((run) => run.status === "queued").map((run) => run.taskId)).toEqual(["task-b", "task-c"]);
    expect(state.runs.at(-1)).toMatchObject({ id: "reply-other", taskId: "task-other", status: "running" });
  });

  it.each([false, true])("late task acceptance preserves a native Reply already completed (queued=%s)", (queued) => {
    let state = chatReducer(initialChatState(), { type: "optimistic-send", id: "race-native", text: "work", fileNames: [], queued });
    state = event(state, { type: "RUN_STARTED", runId: "reply-native", taskId: "task-native" });
    state = event(state, { type: "TEXT_MESSAGE_START", messageId: "answer-native" });
    state = event(state, { type: "RUN_FINISHED", runId: "reply-native" });
    state = chatReducer(state, { type: "task-accepted", assistantId: "assistant-pending-race-native", taskId: "task-native", queued });
    expect(state.runs).toHaveLength(1);
    expect(state.runs[0]).toMatchObject({ id: "reply-native", taskId: "task-native", status: "RUN_FINISHED" });
    expect(state.run.status).toBe("RUN_FINISHED");
  });

  it("another tab's native Reply cannot consume a local optimistic assistant", () => {
    let state = chatReducer(initialChatState(), { type: "optimistic-send", id: "local", text: "local work", fileNames: [] });
    state = chatReducer(state, { type: "task-accepted", assistantId: "assistant-pending-local", taskId: "task-local", queued: false });
    state = event(state, { type: "RUN_STARTED", runId: "reply-other", taskId: "task-other" });
    state = event(state, { type: "TEXT_MESSAGE_START", messageId: "answer-other" });
    expect(state.pendingAssistantId).toBe("assistant-pending-local");
    expect(state.items.find((item) => item.id === "assistant-pending-local")).toMatchObject({ taskId: "task-local" });
    expect(state.items.find((item) => item.id === "answer-other")).toMatchObject({ taskId: "task-other", runId: "reply-other" });
  });

  it("an unbound optimistic assistant waits for the receipt before native identity reconciliation", () => {
    let state = chatReducer(initialChatState(), { type: "optimistic-send", id: "late", text: "work", fileNames: [] });
    state = event(state, { type: "RUN_STARTED", runId: "reply-late", taskId: "task-late" });
    state = event(state, { type: "TEXT_MESSAGE_START", messageId: "answer-late" });
    state = event(state, { type: "TEXT_MESSAGE_CONTENT", messageId: "answer-late", delta: "answer" });
    expect(state.items.some((item) => item.id === "assistant-pending-late")).toBe(true);
    expect(state.pendingAssistantId).toBe("assistant-pending-late");
    state = chatReducer(state, { type: "task-accepted", assistantId: "assistant-pending-late", taskId: "task-late", queued: false });
    expect(state.items.some((item) => item.id === "assistant-pending-late")).toBe(false);
    expect(state.pendingAssistantId).toBeNull();
    expect(state.items.find((item) => item.id === "answer-late")).toMatchObject({ text: "answer", taskId: "task-late" });
    expect(state.items.find((item) => item.id === "user-late")).toMatchObject({ taskId: "task-late" });
  });

  it("a late receipt for a different task keeps its placeholder separate from another tab's answer", () => {
    let state = chatReducer(initialChatState(), { type: "optimistic-send", id: "local-late", text: "local work", fileNames: [] });
    state = event(state, { type: "RUN_STARTED", runId: "reply-other", taskId: "task-other" });
    state = event(state, { type: "TEXT_MESSAGE_START", messageId: "answer-other" });
    state = chatReducer(state, { type: "task-accepted", assistantId: "assistant-pending-local-late", taskId: "task-local", queued: false });
    expect(state.pendingAssistantId).toBe("assistant-pending-local-late");
    expect(state.items.find((item) => item.id === "assistant-pending-local-late")).toMatchObject({ taskId: "task-local" });
    expect(state.items.find((item) => item.id === "answer-other")).toMatchObject({ taskId: "task-other" });
  });

  it("native task identity selects the matching queue entry rather than FIFO", () => {
    const state = event(runningWithQueue(), { type: "RUN_STARTED", runId: "reply-c", taskId: "task-c" });
    expect(state.runs[1]).toMatchObject({ taskId: "task-b", status: "queued" });
    expect(state.runs[2]).toMatchObject({ id: "reply-c", taskId: "task-c", status: "running" });
  });

  it.each([undefined, "reply-a", "unknown-reply"])("knowledge sources use their Reply identity instead of the last queued task (%s)", (runId) => {
    const source = { source_id: "source", knowledge_base_id: "kb", document_id: "doc", filename: "guide.txt", excerpt: "evidence" };
    const state = event(runningWithQueue(), { type: "CUSTOM", name: "knowledge_sources", runId, value: { sources: [source] } });
    expect(state.knowledgeSources[0].taskId).toBe(runId === "unknown-reply" ? "" : "task-a");
  });

  it("legacy terminal events without a Reply id still target the known current Reply", () => {
    const state = event(runningWithQueue(), { type: "RUN_FINISHED" });
    expect(state.runs[0].status).toBe("RUN_FINISHED");
    expect(state.runs[2].status).toBe("queued");
  });

  it.each(["RUN_FINISHED", "RUN_CANCELLED"])("a stale %s cannot finish the new active Reply", (type) => {
    let state = event(runningWithQueue(), { type: "RUN_FINISHED", runId: "reply-a" });
    state = event(state, { type: "RUN_STARTED", runId: "reply-b" });
    const next = event(state, { type, runId: "reply-a" });
    expect(next).toBe(state);
    expect(next.run).toMatchObject({ active: true, runId: "reply-b" });
  });

  it("keeps Reply status when task acceptance arrives after stream completion", () => {
    let state = chatReducer(initialChatState(), { type: "optimistic-send", id: "race", text: "work", fileNames: [] });
    state = event(state, { type: "RUN_STARTED", runId: "reply-race" });
    state = event(state, { type: "RUN_FINISHED", runId: "reply-race" });
    state = chatReducer(state, { type: "task-accepted", assistantId: "assistant-pending-race", taskId: "task-race", queued: false });
    expect(state.runs[0]).toMatchObject({ id: "reply-race", taskId: "task-race", status: "RUN_FINISHED" });
  });
  it("uses the tool event Reply identity when replay has lost RUN_STARTED", () => {
    const state = event(initialChatState(), {
      type: "TOOL_CALL_START", toolCallId: "skill-1",
      toolCallName: "Skill", parentMessageId: "reply-completed",
    });
    expect(state.items[0]).toMatchObject({ runId: "reply-completed" });
  });
  it("merges a real message id into the optimistic assistant message", () => {
    let state = chatReducer(initialChatState(), {
      type: "optimistic-send",
      id: "100",
      text: "生成周报",
      fileNames: [],
    });

    state = event(state, {
      type: "TEXT_MESSAGE_START",
      messageId: "reply-block-1",
    });
    state = event(state, {
      type: "TEXT_MESSAGE_CONTENT",
      messageId: "reply-block-1",
      delta: "完成",
    });

    expect(state.items).toHaveLength(2);
    expect(state.items[1]).toMatchObject({
      kind: "text",
      id: "reply-block-1",
      text: "完成",
    });
  });

  it("does not clear accumulated output on same reply continuation", () => {
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-1",
    });
    state = event(state, {
      type: "TEXT_MESSAGE_START",
      messageId: "answer-1",
    });
    state = event(state, {
      type: "TEXT_MESSAGE_CONTENT",
      messageId: "answer-1",
      delta: "保留我",
    });
    state = event(state, {
      type: "CUSTOM",
      name: "HITL_APPROVAL_REQUEST",
      value: {
        toolCalls: [{ id: "tool-1", name: "write", input: "{}" }],
        decisionVersion: "v1",
      },
    });

    state = event(state, {
      type: "RUN_STARTED",
      runId: "reply-1",
    });

    expect(state.items[0]).toMatchObject({ text: "保留我" });
    expect(state.pendingConfirm?.decisionVersion).toBe("v1");
  });

  it("deduplicates replayed tool starts and artifacts", () => {
    const toolStart = {
      type: "TOOL_CALL_START",
      toolCallId: "tool-1",
      toolCallName: "Read",
    };
    const artifact = {
      type: "CUSTOM",
      name: "ARTIFACT_CREATED",
      value: {
        artifactId: "artifact-1",
        taskId: "task-1",
        name: "report.md",
        mimeType: "text/markdown",
        size: 12,
        version: 1,
      },
    };

    let state = event(initialChatState(), toolStart);
    state = event(state, toolStart);
    state = event(state, artifact);
    state = event(state, artifact);

    expect(state.items.filter((item) => item.kind === "tool")).toHaveLength(1);
    expect(state.artifacts).toHaveLength(1);
  });

  it("preserves the Product-assigned artifact version from the live event", () => {
    const state = event(initialChatState(), {
      type: "CUSTOM",
      name: "ARTIFACT_CREATED",
      value: {
        artifactId: "artifact-v3",
        taskId: "task-3",
        name: "report.md",
        mimeType: "text/markdown",
        size: 24,
        version: 3,
      },
    });

    expect(state.artifacts[0].version).toBe(3);
  });

  it("tracks public execution steps without exposing reasoning as a step", () => {
    let state = event(initialChatState(), {
      type: "STEP_STARTED",
      stepName: "model-call",
    });
    state = event(state, {
      type: "REASONING_MESSAGE_CONTENT",
      messageId: "reason-1",
      delta: "internal detail",
    });
    state = event(state, {
      type: "STEP_FINISHED",
      stepName: "model-call",
    });

    expect(state.steps).toEqual([
      expect.objectContaining({ name: "model-call", state: "completed" }),
    ]);
  });

  it("rolls back both optimistic bubbles when dispatch fails", () => {
    let state = chatReducer(initialChatState(), {
      type: "optimistic-send",
      id: "failed-1",
      text: "失败请求",
      fileNames: [],
    });
    state = chatReducer(state, {
      type: "send-failed",
      assistantId: "assistant-pending-failed-1",
      message: "network failed",
    });
    expect(state.items).toHaveLength(0);
    expect(state.run).toMatchObject({ active: false, status: "RUN_ERROR" });
  });

  it("queues a user message without replacing the active run", () => {
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-active",
    });
    state = chatReducer(state, {
      type: "optimistic-send",
      id: "queued-1",
      text: "下一项工作",
      fileNames: [],
      queued: true,
    });

    expect(state.items.at(-1)).toMatchObject({ role: "user", text: "下一项工作" });
    expect(state.pendingAssistantId).toBeNull();
    expect(state.run).toMatchObject({ active: true, runId: "reply-active" });
    expect(state.runs).toHaveLength(1);
  });

  it("corrects an unexpected optimistic start to the server queue state", () => {
    let state = chatReducer(initialChatState(), {
      type: "optimistic-send",
      id: "queued-after-probe",
      text: "继续生成 PDF",
      fileNames: [],
    });
    state = chatReducer(state, {
      type: "task-accepted",
      assistantId: "assistant-pending-queued-after-probe",
      taskId: "task-queued",
      queued: true,
    });

    expect(state.items).toHaveLength(1);
    expect(state.items[0]).toMatchObject({ taskId: "task-queued" });
    expect(state.run).toMatchObject({ active: false, status: "queued" });
    expect(state.runs).toEqual([expect.objectContaining({
      id: "task-queued",
      taskId: "task-queued",
      status: "queued",
    })]);
  });

  it("associates streamed assistant blocks with the accepted Product Task", () => {
    let state = chatReducer(initialChatState(), {
      type: "optimistic-send",
      id: "accepted-1",
      text: "生成报告",
      fileNames: [],
    });
    state = chatReducer(state, {
      type: "task-accepted",
      assistantId: "assistant-pending-accepted-1",
      taskId: "task-accepted",
      queued: false,
    });
    state = event(state, { type: "RUN_STARTED", runId: "reply-accepted" });
    state = event(state, { type: "TEXT_MESSAGE_START", messageId: "answer-accepted" });

    expect(state.items.at(-1)).toMatchObject({
      id: "answer-accepted",
      taskId: "task-accepted",
      runId: "reply-accepted",
    });
  });

  it("marks initial artifact hydration without treating it as a live event", () => {
    const state = chatReducer(initialChatState(), {
      type: "hydrate-artifacts",
      artifacts: [],
      conversationId: "conversation-1",
    });
    expect(state.artifactsHydratedFor).toBe("conversation-1");
  });
});

describe("chatReducer decided-approval guards", () => {
  it("ignores a replayed approval request whose decision is recorded", () => {
    let state = event(initialChatState(), {
      type: "CUSTOM",
      name: "HITL_APPROVAL_REQUEST",
      value: {
        toolCalls: [{ id: "tool-1", name: "Bash", input: "{}" }],
        decisionVersion: "v1",
      },
    });
    expect(state.pendingConfirm?.decisionVersion).toBe("v1");

    state = event(state, {
      type: "CUSTOM",
      name: "HITL_DECISION_RECORDED",
      value: { decisionVersion: "v1", approved: true },
    });
    expect(state.pendingConfirm).toBeNull();
    expect(state.decidedConfirmVersions).toContain("v1");

    // Replay after a page refresh delivers the request again.
    state = event(state, {
      type: "CUSTOM",
      name: "HITL_APPROVAL_REQUEST",
      value: {
        toolCalls: [{ id: "tool-1", name: "Bash", input: "{}" }],
        decisionVersion: "v1",
      },
    });
    expect(state.pendingConfirm).toBeNull();
  });

  it("keeps a pending card from a different, undecided request", () => {
    let state = event(initialChatState(), {
      type: "CUSTOM",
      name: "HITL_APPROVAL_REQUEST",
      value: {
        toolCalls: [{ id: "tool-1", name: "Bash", input: "{}" }],
        decisionVersion: "v1",
      },
    });
    state = event(state, {
      type: "CUSTOM",
      name: "HITL_DECISION_RECORDED",
      value: { decisionVersion: "other", approved: false },
    });
    expect(state.pendingConfirm?.decisionVersion).toBe("v1");
  });

  it("keeps decided versions across an answered second round", () => {
    let state = event(initialChatState(), {
      type: "CUSTOM",
      name: "HITL_DECISION_RECORDED",
      value: { decisionVersion: "v1", approved: true },
    });
    state = event(state, {
      type: "CUSTOM",
      name: "HITL_APPROVAL_REQUEST",
      value: {
        toolCalls: [{ id: "tool-2", name: "Write", input: "{}" }],
        decisionVersion: "v2",
      },
    });
    expect(state.pendingConfirm?.decisionVersion).toBe("v2");
    expect(state.decidedConfirmVersions).toEqual(["v1"]);
  });

  it("does not hydrate a REST card whose version was already decided", () => {
    let state = event(initialChatState(), {
      type: "CUSTOM",
      name: "HITL_DECISION_RECORDED",
      value: { decisionVersion: "v1", approved: true },
    });
    state = chatReducer(state, {
      type: "hydrate-confirm",
      pending: {
        toolCalls: [{ id: "tool-1", name: "Bash", input: "{}" }],
        decisionVersion: "v1",
      },
    });
    expect(state.pendingConfirm).toBeNull();
  });

  it("does not restore an approval card after the run is terminal", () => {
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-1",
    });
    state = event(state, { type: "RUN_FINISHED" });
    state = chatReducer(state, {
      type: "restore-confirm",
      pending: {
        toolCalls: [{ id: "tool-1", name: "Bash", input: "{}" }],
        decisionVersion: "v1",
      },
      message: "stale conflict",
    });
    expect(state.pendingConfirm).toBeNull();
    expect(state.run.status).toBe("RUN_FINISHED");
  });
});

describe("chatReducer run grouping state", () => {
  it("tags tool calls with the active run id", () => {
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-1",
    });
    state = event(state, {
      type: "TOOL_CALL_START",
      toolCallId: "tool-1",
      toolCallName: "Read",
    });

    const tool = state.items.find((item) => item.kind === "tool");
    expect(tool).toMatchObject({ kind: "tool", runId: "reply-1" });
  });

  it("tracks one run entry per reply and marks the terminal status", () => {
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-1",
    });
    state = event(state, { type: "RUN_FINISHED" });
    state = event(state, { type: "RUN_STARTED", runId: "reply-2" });

    expect(state.runs.map((run) => run.id)).toEqual(["reply-1", "reply-2"]);
    expect(state.runs[0].status).toBe("RUN_FINISHED");
    expect(state.runs[1].status).toBe("running");

    state = event(state, {
      type: "RUN_ERROR",
      message: "boom",
    });
    expect(state.runs.at(-1)).toMatchObject({
      status: "RUN_ERROR",
      error: "boom",
    });
  });

  it("does not append a second run entry for a HITL continuation", () => {
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-1",
    });
    state = event(state, { type: "RUN_STARTED", runId: "reply-1" });
    expect(state.runs).toHaveLength(1);
  });

  it("merges the optimistic starting entry into the real run", () => {
    let state = chatReducer(initialChatState(), {
      type: "optimistic-send",
      id: "100",
      text: "生成周报",
      fileNames: [],
    });
    state = event(state, { type: "RUN_STARTED", runId: "reply-1" });

    expect(state.runs).toHaveLength(1);
    expect(state.runs[0]).toMatchObject({ id: "reply-1", status: "running" });
  });
});

describe("RUN_ERROR run scoping", () => {
  it("ignores a replayed error frame from a different run", () => {
    // Refresh replays the buffer; a stale stream-disconnection error
    // from a previous run must not pose as the current run's failure.
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-current",
    });
    state = event(state, {
      type: "RUN_ERROR",
      message: "Runtime event stream disconnected",
      runId: "reply-old",
    });
    expect(state.run.active).toBe(true);
    expect(state.run.error).toBeNull();
  });

  it("applies an error frame scoped to the current run", () => {
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-current",
    });
    state = event(state, {
      type: "RUN_ERROR",
      message: "boom",
      runId: "reply-current",
    });
    expect(state.run.active).toBe(false);
    expect(state.run.error).toBe("boom");
  });

  it("applies an unscoped error frame (legacy frames carry no runId)", () => {
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-current",
    });
    state = event(state, { type: "RUN_ERROR", message: "boom" });
    expect(state.run.error).toBe("boom");
  });
});

describe("translator RUN_ERROR contract", () => {
  it("a replayed translator error frame cannot fail the next run", () => {
    // Frame shape produced by translator.py REPLY_END handling: the
    // terminal-tool-failure RUN_ERROR carries its reply's runId.
    let state = event(initialChatState(), {
      type: "RUN_STARTED",
      runId: "reply-1",
    });
    state = event(state, {
      type: "RUN_ERROR",
      message: "任务结束前的最后一次工具执行失败。",
      code: "incomplete_tool_execution",
      runId: "reply-1",
    });
    state = event(state, { type: "RUN_STARTED", runId: "reply-2" });

    // Buffer replay re-delivers the run-1 error while run 2 streams.
    state = event(state, {
      type: "RUN_ERROR",
      message: "任务结束前的最后一次工具执行失败。",
      code: "incomplete_tool_execution",
      runId: "reply-1",
    });

    expect(state.run.active).toBe(true);
    expect(state.run.runId).toBe("reply-2");
    expect(state.run.error).toBeNull();
    expect(state.runs.at(-1)).toMatchObject({
      id: "reply-2",
      status: "running",
      error: null,
    });
  });
});

it('deduplicates structured knowledge sources during SSE replay', () => {
 const source={source_id:'s',knowledge_base_id:'kb',document_id:'doc',filename:'手册.pdf',chunk_index:0,metadata:{page:1},excerpt:'制度',score:0.8};
 const incoming={type:'event' as const,conversationId:'c',event:{type:'CUSTOM',name:'knowledge_sources',taskId:'t',value:{sources:[source]}}};
 let state=chatReducer(initialChatState(),incoming);
 state=chatReducer(state,incoming);
 expect(state.knowledgeSources).toEqual([{taskId:'t',runId:null,source}]);
});

it("keeps unapproved tools under the original request version after a partial decision", () => {
  const calls = ["a", "b"].map(id => ({ id, name: "write", input: "{}" }));
  let state = event(initialChatState(), {
    type: "CUSTOM", name: "HITL_APPROVAL_REQUEST",
    value: { decisionVersion: "original", toolCalls: calls },
  });
  state = event(state, {
    type: "CUSTOM", name: "HITL_DECISION_RECORDED",
    value: { decisionVersion: "original", decidedToolCallIds: ["a"] },
  });
  expect(state.pendingConfirm?.toolCalls.map(c => c.id)).toEqual(["b"]);
  state = event(state, {
    type: "CUSTOM", name: "HITL_APPROVAL_REQUEST",
    value: { decisionVersion: "original", toolCalls: calls },
  });
  expect(state.pendingConfirm?.toolCalls.map(c => c.id)).toEqual(["b"]);
  state = event(state, {
    type: "CUSTOM", name: "HITL_DECISION_RECORDED",
    value: { decisionVersion: "original", decidedToolCallIds: ["b"] },
  });
  expect(state.pendingConfirm).toBeNull();
  state = chatReducer(state, { type: "hydrate-confirm", pending: { decisionVersion: "original", toolCalls: calls } });
  expect(state.pendingConfirm).toBeNull();
});

it('keeps compact retrieval status scoped to its reply during replay', () => {
 let state=initialChatState();
 for(const [runId,status] of [['old','completed'],['new','running'],['old','error']]){
  state=chatReducer(state,{type:'event',conversationId:'c',event:{type:'CUSTOM',name:'knowledge_status',runId,value:{status,search_count:1,failure_count:status==='error'?1:0}}});
 }
 expect(state.knowledgeProgress.new.status).toBe('running');
 expect(state.knowledgeProgress.old.status).toBe('error');
 expect(state.items).toEqual([]);
});

it("replaces streamed tool output with the marked final summary and retains error status", () => {
  let state = event(initialChatState(), { type: "RUN_STARTED", runId: "reply" });
  state = event(state, { type: "TOOL_CALL_START", toolCallId: "call", toolCallName: "Bash" });
  state = event(state, { type: "CUSTOM", name: "TOOL_RESULT_DELTA", value: { toolCallId: "call", delta: "streamed full output" } });
  const content = "streamed\n\n[工具结果过长，展示内容已截断]";
  state = event(state, { type: "TOOL_CALL_RESULT", toolCallId: "call", content });
  state = event(state, { type: "CUSTOM", name: "TOOL_RESULT_END", value: { toolCallId: "call", state: "error" } });
  expect(state.items.find(item => item.id === "call")).toMatchObject({
    kind: "tool", call: { result: content, state: "error" },
  });
});

it("clearing an accepted approval does not erase a newer pending version", () => {
  const pending = { decisionVersion: "next", toolCalls: [{ id: "next-call", name: "Bash", input: "{}" }] };
  const state = chatReducer({ ...initialChatState(), pendingConfirm: pending }, { type: "clear-confirm", decisionVersion: "previous" });
  expect(state.pendingConfirm).toEqual(pending);
  expect(state.decidedConfirmVersions).toContain("previous");
});

it("retains a shared revision in both tasks across SSE replay and REST hydration", () => {
  let state = initialChatState();
  for (const taskId of ["first", "second", "second"]) {
    state = event(state, { type: "CUSTOM", name: "ARTIFACT_CREATED", value: {
      artifactId: "md-v1", taskId, name: "report.md", version: 1, changeType: "presented",
    } });
  }
  expect(state.artifacts.map((a) => a.task_id)).toEqual(["first", "second"]);
  state = chatReducer(state, { type: "hydrate-artifacts", conversationId: "conversation-1",
    artifacts: [...state.artifacts].reverse() });
  expect(state.artifacts).toHaveLength(2);
  expect(state.artifacts.every((a) => a.id === "md-v1" && a.version === 1)).toBe(true);
});

it("stores tool content references with task identity and bounded preview", () => {
  let state = event(initialChatState(), { type: "TOOL_CALL_START", toolCallId: "write", toolCallName: "Write", taskId: "task" });
  state = event(state, { type: "CUSTOM", name: "TOOL_CONTENT_AVAILABLE", taskId: "task", value: { toolCallId: "write", kind: "arguments", sha256: "a".repeat(64), sizeBytes: 9000, preview: "preview", incomplete: false } });
  expect(state.items[0]).toMatchObject({ taskId: "task", content: { arguments: { sizeBytes: 9000, preview: "preview" } }, call: { args: "preview", state: "running" } });
});

it("keeps referenced metadata independent of the tool result", () => {
  let state = event(initialChatState(), { type: "TOOL_CALL_START", toolCallId: "write", toolCallName: "Write", taskId: "task" });
  state = event(state, { type: "TOOL_CALL_RESULT", toolCallId: "write", content: "success" });
  state = event(state, { type: "CUSTOM", name: "TOOL_CONTENT_AVAILABLE", taskId: "task", value: { toolCallId: "write", kind: "metadata", sha256: "b".repeat(64), sizeBytes: 9000, preview: "diff preview", incomplete: false } });
  expect(state.items[0]).toMatchObject({ content: { metadata: { preview: "diff preview" } }, call: { result: "success" } });
});
