import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import actualBusiness from "./chat/fixtures/reset-business-state.json";
import { useTaskStream } from "./useTaskStream";
import { api } from "./api";
import type { Artifact, PendingConfirm, Task } from "./types";

vi.mock("./api", () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    upload: vi.fn().mockResolvedValue({}),
  },
  getToken: () => "token",
  streamUrl: () => "/api/tasks/conversations/conversation-1/events",
}));

function sseResponse(chunks: string[], keepOpen = false): Response {
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index < chunks.length) {
        controller.enqueue(encoder.encode(chunks[index]));
        index += 1;
      } else if (!keepOpen) {
        controller.close();
      }
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

const sleep = (ms: number) => new Promise((resolve) => {
  setTimeout(resolve, ms);
});

describe("useTaskStream artifact history", () => {
  const delivery = (taskId: string): Artifact => ({
    id: "shared-version", task_id: taskId, conversation_id: "conversation-1",
    name: "report.md", mime_type: "text/markdown", size: 1, version: 1,
    change_type: "presented", metadata: {}, created_at: "2026-09-01T00:00:00Z",
  });

  beforeEach(() => {
    vi.mocked(api.get).mockReset().mockResolvedValue([]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([], true)));
  });
  afterEach(() => {
    vi.mocked(api.get).mockReset().mockResolvedValue([]);
    vi.unstubAllGlobals();
  });

  it("loads all pages while retaining shared versions in every task", async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce(Array.from({ length: 200 }, (_, index) => delivery(`task-${index}`)))
      .mockResolvedValueOnce([delivery("task-older")]);
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    await act(async () => {});
    expect(result.current.artifacts).toHaveLength(201);
    expect(api.get).toHaveBeenNthCalledWith(2, "/artifacts?conversation_id=conversation-1&limit=200&offset=200");
    unmount();
  });

  it("does not mark a partial history hydrated when a later page fails", async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce(Array.from({ length: 200 }, (_, index) => delivery(`task-${index}`)))
      .mockRejectedValueOnce(new Error("temporary failure"));
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    await act(async () => {});
    expect(result.current.artifactsHydratedFor).not.toBe("conversation-1");
    expect(result.current.artifacts).toHaveLength(0);
    unmount();
  });

  it("discards a pending history when a boot change starts a newer hydration", async () => {
    let resolve!: (items: Artifact[]) => void;
    vi.mocked(api.get)
      .mockReturnValueOnce(new Promise((done) => { resolve = done; }))
      .mockResolvedValueOnce([delivery("task-current")]);
    const stream = controlledSse();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(stream.response));
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    await act(async () => {
      for (const bootId of ["boot-a", "boot-b"]) {
        stream.push(`data: ${JSON.stringify({ type: "CUSTOM", name: "REPLAY_DONE", value: { bootId } })}\n\n`);
      }
    });
    await act(async () => {
      resolve(Array.from({ length: 200 }, (_, index) => delivery(`task-stale-${index}`)));
    });
    expect(result.current.artifacts.map((item) => item.task_id)).toEqual(["task-current"]);
    expect(api.get).toHaveBeenCalledTimes(2);
    unmount();
  });

  it.each(["switch", "unmount"])("ignores an old page after %s and stops pagination", async (action) => {
    let resolve!: (items: Artifact[]) => void;
    vi.mocked(api.get).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const { result, rerender, unmount } = renderHook(({ id }) => useTaskStream(id), {
      initialProps: { id: "conversation-1" },
    });
    if (action === "switch") rerender({ id: "conversation-2" });
    else unmount();
    await act(async () => {
      resolve(Array.from({ length: 200 }, (_, index) => delivery(`task-${index}`)));
    });
    expect(api.get).toHaveBeenCalledTimes(action === "switch" ? 2 : 1);
    expect(result.current.artifacts).toHaveLength(0);
    if (action === "switch") {
      expect(result.current.artifactsHydratedFor).toBe("conversation-2");
      unmount();
    }
  });
});

describe("useTaskStream product actions", () => {
  beforeEach(() => {
    vi.mocked(api.post).mockReset();
    vi.mocked(api.upload).mockReset();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([], true)));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("submits a standard message, associates the accepted task, and sends Stop", async () => {
    const accepted = { task: { id: "task-new" } as Task, queued: false };
    vi.mocked(api.post).mockResolvedValueOnce(accepted).mockResolvedValueOnce({});
    const onTask = vi.fn();
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    await act(async () => {
      expect(await result.current.sendMessage("hello", [], "standard", onTask)).toEqual(accepted);
    });
    expect(api.post).toHaveBeenNthCalledWith(1, "/tasks/conversations/conversation-1/tasks", {
      message: "hello", approval_profile: "standard",
    });
    expect(onTask).toHaveBeenCalledWith(accepted.task, false);
    expect(result.current.items.some((item) => item.kind === "text"
      && item.text === "hello" && item.taskId === "task-new")).toBe(true);
    await act(async () => { await result.current.stop("task-new"); });
    expect(api.post).toHaveBeenLastCalledWith("/tasks/task-new/stop");
    unmount();
  });

  it("uploads attachments with Full Access intent and preserves queued task state", async () => {
    vi.mocked(api.upload).mockResolvedValue({ task: { id: "queued-task" }, queued: true });
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    await act(async () => { await result.current.sendMessage("review", [file], "full_access", undefined, true); });
    const [url, form] = vi.mocked(api.upload).mock.calls[0];
    expect(url).toBe("/tasks/conversations/conversation-1/tasks-with-files");
    expect((form as FormData).get("message")).toBe("review");
    expect((form as FormData).get("approval_profile")).toBe("full_access");
    expect(((form as FormData).get("files") as File).name).toBe("notes.txt");
    expect(result.current.runs.some((run) => run.taskId === "queued-task" && run.status === "queued")).toBe(true);
    expect(api.post).not.toHaveBeenCalled();
    unmount();
  });

  it("removes optimistic messages and exposes an admission failure", async () => {
    vi.mocked(api.post).mockRejectedValue(new Error("model unavailable"));
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    await act(async () => {
      await expect(result.current.sendMessage("will fail")).rejects.toThrow("model unavailable");
    });
    expect(result.current.items).toEqual([]);
    expect(result.current.run.active).toBe(false);
    expect(result.current.run.error).toBe("model unavailable");
    unmount();
  });

  const pending: PendingConfirm = {
    taskId: "parked-task", decisionVersion: "durable-decision-version",
    toolCalls: [{ id: "original-call", name: "Bash", input: "{}" }],
  };
  const decisions = [{ tool_call_id: "original-call", approved: true }];

  it.each(["confirm", "enableFullAccess"] as const)("%s submits the durable decision version and clears the card", async (action) => {
    vi.mocked(api.post).mockResolvedValue({});
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    act(() => result.current.hydratePendingConfirm(pending));
    expect(result.current.pendingConfirm?.decisionVersion).toBe(pending.decisionVersion);
    await act(async () => { await result.current[action]("parked-task", decisions); });
    expect(api.post).toHaveBeenCalledWith(`/tasks/parked-task/${action === "confirm" ? "confirm" : "full-access"}`, {
      decisions, decision_version: pending.decisionVersion,
    });
    expect(result.current.pendingConfirm).toBeNull();
    expect(result.current.approvalSubmitting).toBe(false);
    unmount();
  });

  it("keeps the pending card mounted during submission and prevents stale restoration", async () => {
    let resolve!: (value: unknown) => void;
    vi.mocked(api.post).mockReturnValue(new Promise((done) => { resolve = done; }));
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    act(() => result.current.hydratePendingConfirm(pending));
    let request!: Promise<void>;
    act(() => { request = result.current.confirm("parked-task", decisions); });
    expect(result.current.pendingConfirm).toEqual(pending);
    expect(result.current.approvalSubmitting).toBe(true);
    await act(async () => { resolve({}); await request; });
    act(() => result.current.hydratePendingConfirm(pending));
    expect(result.current.pendingConfirm).toBeNull();
    unmount();
  });

  it.each(["confirm", "enableFullAccess"] as const)("%s restores the original card after a retryable failure", async (action) => {
    vi.mocked(api.post).mockRejectedValue(new Error("temporary failure"));
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    act(() => result.current.hydratePendingConfirm(pending));
    await act(async () => {
      await expect(result.current[action]("parked-task", decisions)).rejects.toThrow("temporary failure");
    });
    expect(result.current.pendingConfirm).toEqual(pending);
    expect(result.current.approvalError).toBe("temporary failure");
    expect(result.current.run.error).not.toBe("temporary failure");
    expect(result.current.approvalSubmitting).toBe(false);
    unmount();
  });

  it.each(["confirm", "enableFullAccess"] as const)("%s does not restore an approval that the server says has already ended", async (action) => {
    vi.mocked(api.post).mockRejectedValue(Object.assign(new Error("already answered"), {
      code: "task_not_awaiting_permission",
    }));
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    act(() => result.current.hydratePendingConfirm(pending));
    await act(async () => {
      await expect(result.current[action]("parked-task", decisions)).rejects.toThrow("already answered");
    });
    expect(result.current.pendingConfirm).toBeNull();
    expect(result.current.approvalSubmitting).toBe(false);
    unmount();
  });
});

describe("useTaskStream boot switch", () => {
  beforeEach(() => {
    let connection = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => {
        connection += 1;
        if (connection === 1) {
          // Old process: two id-bearing frames, then the stream drops.
          return Promise.resolve(sseResponse([
            "id: bootA:1\nevent: RUN_STARTED\ndata: "
              + JSON.stringify({type: "RUN_STARTED", runId: "reply-old"})
              + "\n\n",
            "id: bootA:2\nevent: TEXT_MESSAGE_START\ndata: "
              + JSON.stringify({
                type: "TEXT_MESSAGE_START",
                messageId: "msg-old",
              })
              + "\n\n",
            "id: bootA:3\nevent: TEXT_MESSAGE_CONTENT\ndata: "
              + JSON.stringify({
                type: "TEXT_MESSAGE_CONTENT",
                messageId: "msg-old",
                delta: "pre-restart answer",
              })
              + "\n\n",
          ]));
        }
        // Reconnect after the product restarted: the buffer is empty
        // and nothing has happened since, so REPLAY_DONE is the ONLY
        // frame on the connection - no id-bearing frame ever arrives.
        return Promise.resolve(sseResponse([
          "event: CUSTOM\ndata: "
            + JSON.stringify({
              type: "CUSTOM",
              name: "REPLAY_DONE",
              value: {bootId: "bootB"},
            })
            + "\n\n",
        ], true));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("resets reducer state when REPLAY_DONE reveals a new boot on an empty replay", async () => {
    const { result } = renderHook(() => useTaskStream("conversation-1"));

    // Boot A events land.
    await act(async () => { await sleep(300); });
    expect(result.current.run.runId).toBe("reply-old");
    expect(
      result.current.items.some(
        (item) => item.kind === "text" && item.text === "pre-restart answer",
      ),
    ).toBe(true);

    // Stream dropped (fetch resolves, reader ends); the hook waits 1s
    // and reconnects into the restarted product.
    await act(async () => { await sleep(1800); });

    // The empty replay carries no events, but its REPLAY_DONE revealed
    // the boot switch: the pre-restart run state, streaming items and
    // text must be cleared so a later full replay cannot double-append
    // non-idempotent deltas on top of them.
    expect(result.current.run.runId).toBeNull();
    expect(result.current.run.active).toBe(false);
    expect(
      result.current.items.some(
        (item) => item.kind === "text" && item.text === "pre-restart answer",
      ),
    ).toBe(false);
  });

  it("consumes native data-only frames and resumes its durable cursor without resetting text", async () => {
    const frame = (event: Record<string, unknown>, seq?: number) =>
      (seq === undefined ? "" : `id: product-conversation:${seq}\n`)
      + `data: ${JSON.stringify(event)}\n\n`;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(sseResponse([
        frame({ type: "RUN_STARTED", runId: "reply-native" }, 1),
        frame({ type: "TEXT_MESSAGE_START", messageId: "native-message" }, 2),
        frame({ type: "TEXT_MESSAGE_CONTENT", messageId: "native-message", delta: "原生" }, 3),
        frame({ type: "CUSTOM", name: "REPLAY_DONE", value: { bootId: "product-conversation", end_seq: 3 } }),
      ]))
      .mockResolvedValue(sseResponse([
        ": heartbeat\n\n",
        frame({ type: "CUSTOM", name: "REPLAY_DONE", value: { bootId: "product-conversation", end_seq: 3 } }),
        frame({ type: "TEXT_MESSAGE_CONTENT", messageId: "native-message", delta: "续接" }, 4),
      ], true));
    vi.stubGlobal("fetch", fetchMock);
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    await act(async () => { await sleep(300); });
    expect(result.current.run.runId).toBe("reply-native");
    expect(result.current.items.some((item) => item.kind === "text" && item.text === "原生")).toBe(true);
    await act(async () => { await sleep(1200); });
    expect(fetchMock.mock.calls[1][1].headers["Last-Event-ID"]).toBe("product-conversation:3");
    expect(result.current.items.some((item) => item.kind === "text" && item.text === "原生续接")).toBe(true);
    unmount();
  });
});

function controlledSse() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const cancel = vi.fn();
  const response = new Response(new ReadableStream<Uint8Array>({
    start(value) { controller = value; }, cancel,
  }), { headers: { "content-type": "text/event-stream" } });
  return { response, cancel, push(text: string) { controller.enqueue(new TextEncoder().encode(text)); } };
}
const durableFrame = (seq: number, event: Record<string, unknown>) =>
  `id: product-conversation:${seq}\ndata: ${JSON.stringify(event)}\n\n`;
const tick = async (ms: number) => {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
};

describe("useTaskStream silent connection recovery", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

  it("resumes a silently stalled approval stream and renders the tool result and final answer", async () => {
    const first = controlledSse();
    const second = controlledSse();
    const fetchMock = vi.fn().mockResolvedValueOnce(first.response).mockResolvedValue(second.response);
    vi.stubGlobal("fetch", fetchMock);
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    try {
      first.push(durableFrame(1, { type: "RUN_STARTED", runId: "reply", taskId: "task" })
        + durableFrame(2, { type: "TOOL_CALL_START", toolCallId: "write", toolCallName: "Write", parentMessageId: "reply" })
        + durableFrame(3, { type: "CUSTOM", name: "HITL_APPROVAL_REQUEST", value: {
          taskId: "task", decisionVersion: "decision", toolCalls: [{ id: "write", name: "Write", input: "{}" }],
        } }));
      await tick(40);
      expect(result.current.pendingConfirm?.decisionVersion).toBe("decision");
      expect(result.current.items.some((item) => item.kind === "tool" && item.call.state === "running")).toBe(true);
      vi.mocked(api.post).mockResolvedValue({});
      await act(async () => { await result.current.confirm("task", [{ tool_call_id: "write", approved: true }]); });
      await tick(44_959);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await tick(1_001);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock.mock.calls[1][1].headers["Last-Event-ID"]).toBe("product-conversation:3");
      expect(first.cancel).toHaveBeenCalledTimes(1);
      second.push(durableFrame(4, { type: "CUSTOM", name: "TOOL_RESULT_END", value: { toolCallId: "write", delta: "saved", state: "success" } })
        + durableFrame(5, { type: "TEXT_MESSAGE_START", messageId: "answer" })
        + durableFrame(6, { type: "TEXT_MESSAGE_CONTENT", messageId: "answer", delta: "文件已保存" })
        + durableFrame(7, { type: "TEXT_MESSAGE_END", messageId: "answer" })
        + durableFrame(8, { type: "RUN_FINISHED", runId: "reply" }));
      await tick(40);
      expect(result.current.items.some((item) => item.kind === "tool" && item.call.state === "success")).toBe(true);
      expect(result.current.items.some((item) => item.kind === "text" && item.text === "文件已保存")).toBe(true);
      expect(result.current.run.active).toBe(false);
      expect(result.current.pendingConfirm).toBeNull();
    } finally { unmount(); }
  });

  it("keeps a two-hour approval connection alive on heartbeat comments and times out after they stop", async () => {
    const first = controlledSse();
    const fetchMock = vi.fn().mockResolvedValueOnce(first.response).mockResolvedValue(sseResponse([], true));
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = renderHook(() => useTaskStream("conversation-1"));
    try {
      await tick(0);
      for (let heartbeat = 0; heartbeat < 240; heartbeat += 1) {
        await tick(30_000);
        first.push(": heartbeat\n\n");
        await tick(0);
      }
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await tick(44_999);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await tick(1_001);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally { unmount(); }
  });

  it("uses the last complete frame cursor when a partial next frame stalls", async () => {
    const first = controlledSse();
    const second = controlledSse();
    const fetchMock = vi.fn().mockResolvedValueOnce(first.response).mockResolvedValue(second.response);
    vi.stubGlobal("fetch", fetchMock);
    const { result, unmount } = renderHook(() => useTaskStream("conversation-1"));
    try {
      first.push(durableFrame(1, { type: "RUN_STARTED", runId: "reply" })
        + durableFrame(2, { type: "TEXT_MESSAGE_START", messageId: "answer" })
        + durableFrame(3, { type: "TEXT_MESSAGE_CONTENT", messageId: "answer", delta: "原有" })
        + 'id: product-conversation:4\ndata: {"type":"TEXT_MESSAGE_CONTENT"');
      await tick(46_000);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock.mock.calls[1][1].headers["Last-Event-ID"]).toBe("product-conversation:3");
      second.push(durableFrame(4, { type: "TEXT_MESSAGE_CONTENT", messageId: "answer", delta: "续接" }));
      await tick(40);
      expect(result.current.items.some((item) => item.kind === "text" && item.text === "原有续接")).toBe(true);
    } finally { unmount(); }
  });

  it("reconnects when the server never returns response headers", async () => {
    const fetchMock = vi.fn().mockImplementationOnce((_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      })).mockResolvedValue(sseResponse([], true));
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = renderHook(() => useTaskStream("conversation-1"));
    try {
      await tick(44_999);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await tick(1_001);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    } finally { unmount(); }
  });

  it.each([30_000, 45_500])("does not reconnect after unmount at %i ms", async (elapsed) => {
    const first = controlledSse();
    const fetchMock = vi.fn().mockResolvedValue(first.response);
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = renderHook(() => useTaskStream("conversation-1"));
    await tick(elapsed);
    unmount();
    await tick(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(first.cancel).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
  });
});

import { encodeRealtimeCursor, realtimeMessageId } from './chat/realtime-protocol';
describe('typed realtime acknowledgement', () => {
  const cid='conversation-1',generation='11111111-1111-4111-8111-111111111111';
  const cursor=(n:number)=>encodeRealtimeCursor({schema_version:1,conversation_id:cid,generation,stream_id:`${n}-0`});
  const reset={schema_version:1,conversation_id:cid,generation,reason:'initial',snapshots:[],business_state:{messages:[],tasks:[],artifacts:[],activity:[],knowledge:[],pending_confirm:null,decided_confirm_tools:{},closed_confirm_versions:[]},business_seq:0,sync_status:'synced'};
  const frame=(name:string,value:unknown,id='')=>(id?`id: ${id}\n`:'')+`data: ${JSON.stringify({type:'CUSTOM',name,value})}\n\n`;
  const body=(content:boolean,version=1)=>({schema_version:1,event_id:content?`delta-${version}`:'start',conversation_id:cid,task_id:'t',reply_id:'r',source:'worker',execution_epoch:1,payload:{type:content?'TEXT_MESSAGE_CONTENT':'TEXT_MESSAGE_START',messageId:'b',...(content?{delta:'你😀'}:{})},...(content?{text:{message_id:realtimeMessageId('t','r'),block_id:'b',content_version:version,from_offset:0,to_offset:7}}:{})});
  beforeEach(()=>{vi.useFakeTimers();vi.mocked(api.get).mockResolvedValue([]);});
  afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();vi.clearAllMocks();});
  it('does not acknowledge the 40ms queue and resumes only successful state',async()=>{
    const first=controlledSse(),second=controlledSse();
    const fetchMock=vi.fn().mockResolvedValueOnce(first.response).mockResolvedValue(second.response);
    vi.stubGlobal('fetch',fetchMock);
    const {result,unmount}=renderHook(()=>useTaskStream(cid));
    try {
      await tick(0);
      first.push(frame('REALTIME_RESET',reset,cursor(1))+frame('REALTIME_EVENT',body(false),cursor(2))+frame('REALTIME_EVENT',body(true),cursor(3)));
      await tick(0);
      expect(result.current.realtime).toBeUndefined();
      await tick(40);
      expect(result.current.realtime?.cursor).toBe(cursor(3));
      expect(result.current.items.find(i=>i.kind==='text')).toMatchObject({text:'你😀'});
      await tick(46_000);
      expect(fetchMock.mock.calls[1][1].headers['Last-Event-ID']).toBe(cursor(3));
      second.push(frame('REALTIME_EVENT',body(true),cursor(4)));
      await tick(40);
      expect(result.current.items.find(i=>i.kind==='text')).toMatchObject({text:'你😀'});
    } finally {unmount();}
  });
  it('content gap cancels only connection and reconnects without cursor',async()=>{
    const first=controlledSse();const fetchMock=vi.fn().mockResolvedValueOnce(first.response).mockResolvedValue(sseResponse([],true));
    vi.stubGlobal('fetch',fetchMock);const {result,unmount}=renderHook(()=>useTaskStream(cid));
    try {
      await tick(0);
      first.push(frame('REALTIME_RESET',reset,cursor(1))+frame('REALTIME_EVENT',body(false),cursor(2))+frame('REALTIME_EVENT',body(true,3),cursor(3)));
      await tick(40);expect(result.current.realtime?.needsReset).toBe(true);
      await tick(1000);expect(fetchMock.mock.calls[1][1].headers['Last-Event-ID']).toBeUndefined();
    } finally {unmount();}
  });
  it.each([404,413])('does not endlessly retry permanent HTTP %s',async(status)=>{
    const fetchMock=vi.fn().mockResolvedValue(new Response('',{status}));vi.stubGlobal('fetch',fetchMock);
    const {unmount}=renderHook(()=>useTaskStream(cid));
    try {await tick(10000);expect(fetchMock).toHaveBeenCalledTimes(1);}finally{unmount();}
  });
});

describe('typed cross-tab Task metadata',()=>{
 afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();vi.clearAllMocks();});
 it('streams first body while one metadata lookup is pending, preserving live status on receipt',async()=>{
  vi.useFakeTimers();
  const connection=controlledSse();vi.stubGlobal('fetch',vi.fn().mockResolvedValue(connection.response));
  let complete!:(value:Task)=>void;
  vi.mocked(api.get).mockImplementation((path)=>path==='/tasks/t'?new Promise(resolve=>{complete=resolve as typeof complete;}):Promise.resolve([]));
  const {result,unmount}=renderHook(()=>useTaskStream('conversation-1'));
  const generation='11111111-1111-4111-8111-111111111111';
  const cursor=(n:number)=>encodeRealtimeCursor({schema_version:1,conversation_id:'conversation-1',generation,stream_id:`${n}-0`});
  const frame=(n:number,name:string,value:unknown)=>`id: ${cursor(n)}\ndata: ${JSON.stringify({type:'CUSTOM',name,value})}\n\n`;
  const envelope={schema_version:1,conversation_id:'conversation-1',task_id:'t',reply_id:'r'};
  try {
   await tick(0);
   connection.push(frame(1,'REALTIME_RESET',{schema_version:1,conversation_id:'conversation-1',generation,reason:'initial',snapshots:[],business_state:{messages:[],tasks:[],artifacts:[],activity:[],knowledge:[],pending_confirm:null,decided_confirm_tools:{},closed_confirm_versions:[]},business_seq:0,sync_status:'synced'})+
    frame(2,'REALTIME_EVENT',{...envelope,event_id:'run',source:'business',business_seq:1,payload:{type:'RUN_STARTED'}})+
    frame(3,'REALTIME_EVENT',{...envelope,event_id:'start',source:'worker',execution_epoch:1,payload:{type:'TEXT_MESSAGE_START',messageId:'b'}})+
    frame(4,'REALTIME_EVENT',{...envelope,event_id:'delta',source:'worker',execution_epoch:1,payload:{type:'TEXT_MESSAGE_CONTENT',messageId:'b',delta:'now'},text:{message_id:realtimeMessageId('t','r'),block_id:'b',content_version:1,from_offset:0,to_offset:3}}));
   await tick(40);
   expect(result.current.items.some(i=>i.kind==='text'&&i.text==='now')).toBe(true);
   expect(result.current.realtime?.needsReset).toBe(false);
   expect(vi.mocked(api.get).mock.calls.filter(([path])=>path==='/tasks/t')).toHaveLength(1);
   await act(async()=>{complete({...actualBusiness.tasks[0],conversation_id:'conversation-1',status:'created',approval_profile:'standard',execution_phase:null,run_id:null} satisfies Task);});
   expect(result.current.realtime?.tasks[0]).toMatchObject({id:'t',status:'running',run_id:'r'});
  }finally{unmount();}
 });
});

describe('REST terminal reconciliation', () => {
  const cid = 'conversation-1', generation = '11111111-1111-4111-8111-111111111111';
  const cursor = (n: number) => encodeRealtimeCursor({ schema_version: 1, conversation_id: cid, generation, stream_id: `${n}-0` });
  const frame = (n: number, business: unknown) => `id: ${cursor(n)}\ndata: ${JSON.stringify({type:'CUSTOM',name:'REALTIME_RESET',value:{schema_version:1,conversation_id:cid,generation,reason:'initial',snapshots:[],business_state:business,business_seq:n,sync_status:'synced'}})}\n\n`;
  const task = {...actualBusiness.tasks[0], conversation_id:cid, status:'running', approval_profile:'standard'} as Task;
  const business = (status: string) => ({...actualBusiness,tasks:[{...task,status}],pending_confirm:null,artifacts:actualBusiness.artifacts.map(a=>({...a,conversation_id:cid}))});
  beforeEach(()=>{vi.useFakeTimers();vi.mocked(api.get).mockResolvedValue([]);});
  afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();vi.clearAllMocks();});
  it.each(['completed', 'interrupted'] as const)('requests an atomic cut for newer REST %s, preserving current state until reset',async(status)=>{
    const first=controlledSse(),second=controlledSse();
    const fetchMock=vi.fn().mockResolvedValueOnce(first.response).mockResolvedValue(second.response);
    vi.stubGlobal('fetch',fetchMock);
    const {result,unmount}=renderHook(()=>useTaskStream(cid));
    try {
      await tick(0);first.push(frame(1,business('running')));await tick(40);
      act(()=>result.current.reconcileTasks([{...task,status,updated_at:'2026-10-02T00:00:00Z'}]));
      expect(result.current.realtime?.tasks[0].status).toBe('running');
      expect(result.current.realtime?.needsReset).toBe(true);
      await tick(1000);
      expect(fetchMock.mock.calls[1][1].headers['Last-Event-ID']).toBeUndefined();
      second.push(frame(2,business('completed')));await tick(40);
      expect(result.current.realtime?.tasks[0].status).toBe('completed');
      expect(result.current.artifacts).toHaveLength(actualBusiness.artifacts.length);
      await tick(30000);expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {unmount();}
  });
  it('refreshes the same conversation after task recovery without clearing applied history', async () => {
    const first=controlledSse(), second=controlledSse();
    const fetchMock=vi.fn().mockResolvedValueOnce(first.response).mockResolvedValue(second.response);
    vi.stubGlobal('fetch',fetchMock);
    const {result,unmount}=renderHook(()=>useTaskStream(cid));
    try {
      await tick(0); first.push(frame(1,business('completed'))); await tick(40);
      const previous=result.current.realtime?.tasks;
      act(()=>result.current.refreshConversation());
      expect(result.current.realtime?.tasks).toEqual(previous);
      expect(result.current.realtime?.needsReset).toBe(true);
      await tick(1000);
      expect(fetchMock.mock.calls[1][1].headers['Last-Event-ID']).toBeUndefined();
      second.push(frame(2,business('running'))); await tick(40);
      expect(result.current.realtime?.needsReset).toBe(false);
      expect(result.current.realtime?.tasks[0].status).toBe('running');
    } finally {unmount();}
  });
  it('bounds repeated stale reset recovery and cancels pending checks on unmount',async()=>{
    const connections=Array.from({length:5},()=>controlledSse());
    let index=0;const fetchMock=vi.fn().mockImplementation(()=>Promise.resolve(connections[index++].response));
    vi.stubGlobal('fetch',fetchMock);
    const {result,unmount}=renderHook(()=>useTaskStream(cid));
    await tick(0);connections[0].push(frame(1,business('running')));await tick(40);
    act(()=>result.current.reconcileTasks([{...task,status:'completed',updated_at:'2026-10-02T00:00:00Z'}]));
    await tick(1000);connections[1].push(frame(2,business('running')));await tick(40);
    await tick(15000);connections[2].push(frame(3,business('running')));await tick(40);
    await tick(15000);connections[3].push(frame(4,business('running')));await tick(40);
    await tick(15000);
    act(()=>result.current.reconcileTasks([{...task,status:'completed',updated_at:'2026-10-02T00:00:00Z'}]));
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(result.current.recoveryError).toContain('同步');
    act(()=>result.current.retryRecovery());
    expect(result.current.recoveryError).toBeNull();
    await tick(1000);expect(fetchMock).toHaveBeenCalledTimes(5);
    connections[4].push(frame(5,business('completed')));await tick(40);
    expect(result.current.recoveryError).toBeNull();
    unmount();await tick(60000);expect(fetchMock).toHaveBeenCalledTimes(5);
  });
  it('ignores stale, foreign and active REST records',async()=>{
    const first=controlledSse(),fetchMock=vi.fn().mockResolvedValue(first.response);vi.stubGlobal('fetch',fetchMock);
    const {result,unmount}=renderHook(()=>useTaskStream(cid));
    try {
      await tick(0);first.push(frame(1,business('running')));await tick(40);
      act(()=>result.current.reconcileTasks([{...task,status:'completed',updated_at:'2020-01-01T00:00:00Z'},{...task,id:'foreign',status:'completed'},{...task,status:'running'}]));
      expect(result.current.realtime?.needsReset).toBe(false);expect(fetchMock).toHaveBeenCalledTimes(1);
    }finally{unmount();}
  });
});
