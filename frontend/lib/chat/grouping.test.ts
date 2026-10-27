import { describe, expect, it } from "vitest";

import { groupChatItems } from "./grouping";
import type { ChatItem, ChatTextMsg, ChatToolMsg, RunEntry } from "./model";

type SummaryNode = Extract<ReturnType<typeof groupChatItems>[number], { type: "summary" }>;

function summariesOf(nodes: ReturnType<typeof groupChatItems>): SummaryNode[] {
  return nodes.filter((node): node is SummaryNode => node.type === "summary");
}

function tool(id: string, runId: string | null | undefined, state = "success"): ChatToolMsg {
  return {
    kind: "tool",
    id,
    runId,
    call: { toolCallId: id, toolCallName: "Read", args: "", result: "", state },
  };
}

function text(id: string, role: string): ChatTextMsg {
  return { kind: "text", id, role, text: "t", done: true };
}

describe("groupChatItems", () => {
  it("does not append an empty process to completed text-only replies", () => {
    expect(summariesOf(groupChatItems([text("answer", "assistant")], [{ id: "r", status: "RUN_FINISHED", error: null }]))).toEqual([]);
  });
  it("does not move existing streamed text when a tool is appended", () => {
    const narration = text("narration", "assistant");
    const before = groupChatItems([narration], []);
    const after = groupChatItems([narration, tool("later", "reply")], []);
    expect(after[0]).toEqual(before[0]);
    expect(after[1].type).toBe("summary");
  });
  const runs: RunEntry[] = [
    { id: "reply-1", status: "RUN_FINISHED", error: null },
    { id: "reply-2", status: "running", error: null },
  ];

  it("collapses each run's tool calls into one summary node in place", () => {
    const items: ChatItem[] = [
      text("u1", "user"),
      tool("t1", "reply-1"),
      tool("t2", "reply-1"),
      text("a1", "assistant"),
      tool("t3", "reply-2"),
      text("a2", "assistant"),
    ];

    const nodes = groupChatItems(items, runs);
    const summaries = summariesOf(nodes);
    expect(summaries).toHaveLength(2);
    expect(summaries[0].tools.map((item) => item.id)).toEqual(["t1", "t2"]);
    expect(summaries[0].run?.id).toBe("reply-1");
    expect(summaries[1].tools.map((item) => item.id)).toEqual(["t3"]);
    // Message order is preserved and tool cards never surface as items.
    expect(nodes.filter((node) => node.type === "item").map((node) => node.item.id))
      .toEqual(["u1", "a1", "a2"]);
  });

  it("appends a trailing summary for the latest run without tool calls", () => {
    const items: ChatItem[] = [text("u1", "user"), text("a1", "assistant")];
    const nodes = groupChatItems(items, runs);
    const last = nodes.at(-1);
    expect(last?.type).toBe("summary");
    if (last?.type !== "summary") throw new Error("expected summary node");
    expect(last.tools).toEqual([]);
    expect(last.run?.id).toBe("reply-2");
    expect(last.isLast).toBe(true);
  });

  it("starts a new group after a user message even without run ids", () => {
    const items: ChatItem[] = [
      text("u1", "user"),
      tool("t1", null),
      text("a1", "assistant"),
      text("u2", "user"),
      tool("t2", null),
    ];
    const nodes = groupChatItems(items, []);
    const summaries = summariesOf(nodes);
    expect(summaries).toHaveLength(2);
  });

  it("marks only the latest run as last", () => {
    const items: ChatItem[] = [
      tool("t1", "reply-1"),
      tool("t2", "reply-2"),
    ];
    const nodes = groupChatItems(items, runs);
    const summaries = summariesOf(nodes);
    expect(summaries.map((node) => node.isLast)).toEqual([false, true]);
  });

  it("keeps unannotated narration visible while consolidating a known reply", () => {
    const items: ChatItem[] = [
      text("u1", "user"),
      { ...text("progress-1", "assistant"), text: "我先读取文档。" },
      tool("t1", "reply-1"),
      { ...text("progress-2", "assistant"), text: "接下来提取正文。" },
      tool("t2", "reply-1"),
      { ...text("final", "assistant"), text: "文档主要介绍门店销售培训。" },
    ];

    const nodes = groupChatItems(items, [runs[0]]);
    const summary = summariesOf(nodes)[0];
    expect(summary.progress).toEqual([]);
    expect(summary.process.map((item) => item.id)).toEqual(["t1", "t2"]);
    expect(summariesOf(nodes)).toHaveLength(1);
    expect(nodes.filter((node) => node.type === "item").map((node) => node.item.id))
      .toEqual(["u1", "progress-1", "progress-2", "final"]);
  });
  it("places historical tail tools before the answer of their own reply", () => {
    const completed = runs.map((run) => ({ ...run, status: "RUN_FINISHED" }));
    const items: ChatItem[] = [
      text("u1", "user"),
      { ...text("a1", "assistant"), runId: "reply-1", text: "查询深圳。深圳晴。",
        presentation: { version: 1, process_end: 5 } },
      text("u2", "user"),
      { ...text("a2", "assistant"), runId: "reply-2", text: "查询广州。广州雨。",
        presentation: { version: 1, process_end: 5 } },
      tool("t1", "reply-1"), tool("t2", "reply-1"), tool("t3", "reply-2"),
    ];
    const nodes = groupChatItems(items, completed);
    expect(nodes.map((node) => node.type === "summary" ? `process:${node.key}` : node.item.id))
      .toEqual(["u1", "process:reply-1", "a1", "u2", "process:reply-2", "a2"]);
    const summaries = summariesOf(nodes);
    expect(summaries.map((node) => node.tools.map((item) => item.id)))
      .toEqual([["t1", "t2"], ["t3"]]);
    expect(summaries.map((node) => node.progress.map((item) => item.text)))
      .toEqual([["查询深圳。"], ["查询广州。"]]);
    expect(nodes.flatMap((node) => node.type === "item" && node.item.kind === "text"
      && node.item.role === "assistant" ? [node.item.text] : []))
      .toEqual(["深圳晴。", "广州雨。"]);
    expect(items[1]).toMatchObject({ text: "查询深圳。深圳晴。" });
  });

  it("splits confirmed process boundaries by Unicode code points", () => {
    const answer: ChatTextMsg = { ...text("a", "assistant"), runId: "reply-1",
      text: "🔎查询。结果🙂", presentation: { version: 1, process_end: 4 } };
    const nodes = groupChatItems([answer, tool("t", "reply-1")], [runs[0]]);
    expect(summariesOf(nodes)[0].progress.map((item) => item.text)).toEqual(["🔎查询。"]);
    expect(nodes.flatMap((node) => node.type === "item" && node.item.kind === "text"
      ? [node.item.text] : [])).toEqual(["结果🙂"]);
  });

  it.each([
    undefined,
    { version: 2, process_end: 1 },
    { version: 1, process_end: -1 },
    { version: 1, process_end: 1.5 },
    { version: 1, process_end: 99 },
    { version: 1, process_end: Number.NaN },
  ])("preserves all text when the process boundary is missing or invalid: %j", (presentation) => {
    const answer: ChatTextMsg = { ...text("a", "assistant"), runId: "reply-1",
      text: "完整回答", presentation };
    const nodes = groupChatItems([answer, tool("t", "reply-1")], [runs[0]]);
    expect(summariesOf(nodes)[0].progress).toEqual([]);
    expect(nodes.find((node) => node.type === "item")).toEqual({ type: "item", item: answer });
  });

});


it("streams an unclassified active reply directly without claiming a final answer", () => {
  const message: ChatTextMsg = { ...text("live-answer", "assistant"), runId: "live", text: "正在回答🙂", done: false };
  const nodes = groupChatItems([message], [{ id: "live", status: "running", error: null }]);
  expect(nodes.find(node => node.type === "item")).toEqual({ type: "item", item: message });
  expect(summariesOf(nodes).flatMap(node => node.progress)).toEqual([]);
});

it("shows the tail while running and archives it only when the next explicit boundary arrives", () => {
  const run = { id: "live", status: "running", error: null };
  const message: ChatTextMsg = { ...text("live-answer", "assistant"), runId: "live", text: "🔎查询。结果🙂", done: false };
  const grouped = (end: number) => groupChatItems([{ ...message, presentation: { version: 1, process_end: end } }, tool("t", "live")], [run]);
  const nodes = grouped(4);
  expect(summariesOf(nodes)[0].progress.map(item => item.text)).toEqual(["🔎查询。"]);
  expect(nodes.find(node => node.type === "item")).toMatchObject({ item: { text: "结果🙂", done: false } });
  expect(summariesOf(grouped(7))[0].progress[0].text).toBe(message.text);
  expect(grouped(7).find(node => node.type === "item")).toMatchObject({ item: { text: "" }, hideText: true });
  const completed = groupChatItems([{ ...message, presentation: { version: 1, process_end: 4 } }], [{ ...run, status: "RUN_FINISHED" }]);
  expect(completed.find(node => node.type === "item")).toMatchObject({ item: { text: "结果🙂" } });
});

it.each([undefined, { version: 2, process_end: 2 }, { version: 1, process_end: 99 }, { version: 1, process_end: 1.5 }])("preserves active Unicode text for invalid boundaries %j", presentation => {
  const message: ChatTextMsg = { ...text("a", "assistant"), runId: "r", text: "🙂完整回复", presentation };
  const nodes = groupChatItems([message], [{ id: "r", status: "running", error: null }]);
  expect(nodes.find(node => node.type === "item")).toEqual({ type: "item", item: message });
});

it("does not append an empty process to an active plain answer", () => {
  const message: ChatTextMsg = { ...text("plain", "assistant"), runId: "r", text: "实时回答", done: false };
  expect(summariesOf(groupChatItems([message], [{ id: "r", status: "running", error: null }]))).toEqual([]);
});

it("retains the status fallback before the first reply text", () => {
  expect(summariesOf(groupChatItems([], [{ id: "r", status: "starting", error: null }]))).toHaveLength(1);
});

it("keeps the assistant identity for approval without inventing a process", () => {
  const message: ChatTextMsg = { ...text("approval", "assistant"), runId: "r", text: "请确认操作", done: false };
  const nodes = groupChatItems([message], [{ id: "r", status: "awaiting_permission", error: null }]);
  expect(nodes).toEqual([{ type: "item", item: message }]);
});
