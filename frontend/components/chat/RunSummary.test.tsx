import userEvent from "@testing-library/user-event";
import { fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { RunSummary } from "./RunSummary";
import type { ChatTextMsg, ChatToolMsg, RunEntry } from "@/lib/chat/model";

function tool(id: string, name: string, state = "success", result = ""): ChatToolMsg {
  return {
    kind: "tool",
    id,
    runId: "reply-1",
    call: { toolCallId: id, toolCallName: name, args: "{}", result, state },
  };
}

describe("RunSummary", () => {
  it.each(["queued", "starting", "running"])("uses working copy for %s without exposing internal stages", (status) => {
    render(createElement(RunSummary, { run: { id: "r", status, error: null }, tools: [], onOpenActivity: vi.fn() }));
    expect(screen.getByText("工作中")).toBeVisible();
    expect(screen.queryByText(/任务排队中|正在准备|正在启动/)).toBeNull();
  });
  it("uses working copy during preparation and keeps Stop authoritative", () => {
    const { rerender } = render(createElement(RunSummary, { run: { id: "r", status: "queued", error: null }, taskStatus: "created", executionPhase: "preparing", tools: [], onOpenActivity: vi.fn() }));
    expect(screen.getByText("工作中")).toBeVisible();
    expect(screen.queryByText("任务排队中")).toBeNull();
    rerender(createElement(RunSummary, { run: { id: "r", status: "queued", error: null }, taskStatus: "cancelling", executionPhase: "preparing", tools: [], onOpenActivity: vi.fn() }));
    expect(screen.getByText("正在停止")).toBeVisible();
    expect(screen.queryByText("工作中")).toBeNull();
  });
  it("hides an empty completed summary even with stale streaming state", () => {
    const { container } = render(createElement(RunSummary, { run: { id: "r", status: "running", error: null }, taskStatus: "completed", tools: [], onOpenActivity: vi.fn() }));
    expect(container).toBeEmptyDOMElement();
  });
  it("does not claim to connect a model after failure", () => {
    render(createElement(RunSummary, { run: { id: "r", status: "RUN_ERROR", error: "failure" }, tools: [], onOpenActivity: vi.fn() }));
    fireEvent.click(screen.getByRole("button", { name: /执行过程/ }));
    expect(screen.getByText("本轮执行已结束")).toBeVisible();
  });
  it("keeps the whole task running after its current tools finish", () => {
    render(createElement(RunSummary, { run: { id: "reply-1", status: "running", error: null },
      taskStatus: "running", tools: [tool("finished", "agent_get")], onOpenActivity: vi.fn() }));
    expect(screen.getByText("工作中")).toBeTruthy();
    expect(screen.getByLabelText("仍在运行")).toBeVisible();
  });
  it("leaves image results to the answer instead of the execution process", () => {
    render(createElement(RunSummary, {
      run: { id: "reply-1", status: "RUN_FINISHED", error: null },
      tools: [tool("image-search", "web_search", "success", JSON.stringify({
        category: "images", results: [{ title: "雪山", url: "https://example.com/page",
          image_url: "https://example.com/full.jpg", thumbnail_url: "https://example.com/thumb.jpg" }],
      }))], onOpenActivity: vi.fn(),
    }));
    expect(screen.queryByRole("region", { name: "图片搜索结果" })).toBeNull();
  });
  it("does not invent a running status for an orphan replay record", () => {
    render(createElement(RunSummary, { run: null, tools: [tool("orphan", "Skill", "running")], onOpenActivity: vi.fn() }));
    expect(screen.getByText("执行记录")).toBeTruthy();
    expect(screen.queryByLabelText("仍在运行")).toBeNull();
  });

  it("uses terminal Task status over stale running stream state", () => {
    render(createElement(RunSummary, {
      run: { id: "reply-1", status: "running", error: null },
      tools: [tool("skill", "Skill")], taskStatus: "completed", onOpenActivity: vi.fn(),
    }));
    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.queryByLabelText("仍在运行")).toBeNull();
  });
  it("renders the completed aggregate with category counts", () => {
    const run: RunEntry = { id: "reply-1", status: "RUN_FINISHED", error: null };
    render(createElement(RunSummary, {
      run,
      tools: [tool("t1", "Read"), tool("t2", "Read"), tool("t3", "Bash")],
      artifactCount: 1,
      onOpenActivity: vi.fn(),
      onOpenArtifacts: vi.fn(),
    }));
    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.getByText(/阅读 2 个文件 · 执行 1 个命令/)).toBeTruthy();
    expect(screen.getByText(/生成 1 个成果/)).toBeTruthy();
  });

  it("uses generic working copy instead of the model step name", () => {
    const run: RunEntry = { id: "reply-1", status: "running", error: null };
    render(createElement(RunSummary, {
      run,
      tools: [tool("t1", "Read", "running")],
      steps: [{ id: "s1", name: "调用私有模型", state: "running" }],
      onOpenActivity: vi.fn(),
    }));
    expect(screen.getByText("工作中")).toBeTruthy();
    expect(screen.queryByText(/私有模型/)).toBeNull();
  });

  it("does not highlight an intermediate Bash failure after the task succeeds", () => {
    const run: RunEntry = { id: "reply-1", status: "RUN_FINISHED", error: null };
    render(createElement(RunSummary, {
      run,
      tools: [
        tool("t1", "Read"),
        tool("t2", "Bash", "error", "Error: command not found"),
      ],
      taskStatus: "completed",
      onOpenActivity: vi.fn(),
    }));
    expect(screen.queryByText("调用失败")).toBeNull();
    expect(screen.getByText("已完成")).toBeVisible();
    expect(screen.queryByText("Bash")).toBeNull();
    expect(screen.queryByText("Error: command not found")).toBeNull();
    expect(screen.queryByText("Read")).toBeNull();
  });

  it("shows failed calls when the task ultimately fails", () => {
    const run: RunEntry = { id: "reply-1", status: "RUN_ERROR", error: "failed" };
    render(createElement(RunSummary, {
      run,
      tools: [tool("t2", "Bash", "error", "Error: command not found")],
      taskStatus: "failed",
      onOpenActivity: vi.fn(),
    }));
    expect(screen.getByText("Bash")).toBeTruthy();
    expect(screen.getByText("Error: command not found")).toBeTruthy();
  });

  it("expands the public process inline and opens full activity from the details", () => {
    const onOpenActivity = vi.fn();
    const run: RunEntry = { id: "reply-1", status: "running", error: null };
    const progress: ChatTextMsg = { kind: "text", id: "p1", role: "assistant", text: "我先读取文档并整理结构。", done: true };
    const read = tool("t1", "Read", "running");
    render(createElement(RunSummary, {
      run,
      tools: [read],
      progress: [progress],
      process: [progress, read],
      onOpenActivity,
    }));
    fireEvent.click(screen.getByRole("button", { name: /工作中/ }));
    expect(screen.getByText("我先读取文档并整理结构。")).toBeVisible();
    expect(screen.getByText("读取文件")).toBeVisible();
    expect(onOpenActivity).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "打开完整执行动态" }));
    expect(onOpenActivity).toHaveBeenCalledOnce();
  });

  it("marks an active run as busy and exposes a continuing motion cue", () => {
    const run: RunEntry = { id: "reply-1", status: "running", error: null };
    render(createElement(RunSummary, { run, tools: [], onOpenActivity: vi.fn() }));

    expect(screen.getByRole("status", { name: "仍在运行" })).toBeInTheDocument();
    expect(screen.getByLabelText("仍在运行").closest("section")).toHaveAttribute("aria-busy", "true");
  });
});

it("keeps a manually opened process open as streamed text and tools change", () => {
  const progress: ChatTextMsg = { kind: "text", id: "a:process", role: "assistant", text: "准备文件", done: false };
  const props = { run: { id: "r", status: "running", error: null }, tools: [], progress: [progress], process: [progress], onOpenActivity: vi.fn() };
  const { rerender } = render(createElement(RunSummary, props));
  expect(screen.queryByText("准备文件")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /执行过程/ }));
  const update = { ...progress, text: "准备文件，正在写入" };
  rerender(createElement(RunSummary, { ...props, progress: [update], process: [update, tool("w", "Write", "running")] }));
  expect(screen.getByRole("button", { name: /执行过程/ })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("准备文件，正在写入")).toBeVisible();
  expect(screen.getByRole("heading", { name: "过程说明" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "工具调用" })).toBeVisible();
  expect(screen.queryByLabelText("最新执行进展")).toBeNull();
});

it("keeps uncertain tool results and approval distinct from a working label", () => {
  render(createElement(RunSummary, { run: { id: "r", status: "running", error: null },
    taskStatus: "awaiting_permission", tools: [tool("u", "Write", "unknown")], onOpenActivity: vi.fn() }));
  fireEvent.click(screen.getByRole("button", { name: /等待你的审批/ }));
  expect(screen.getByText("结果未知")).toBeVisible();
  expect(screen.queryByText("工作中")).toBeNull();
});

it("opens and closes process details with the keyboard", async () => {
  const user = userEvent.setup();
  render(createElement(RunSummary, { run: { id: "r", status: "running", error: null },
    tools: [tool("w", "Write", "running")], onOpenActivity: vi.fn() }));
  const toggle = screen.getByRole("button", { name: /执行过程/ });
  toggle.focus();
  await user.keyboard("{Enter}");
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("heading", { name: "工具调用" })).toBeVisible();
  await user.keyboard(" ");
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("heading", { name: "工具调用" })).toBeNull();
});
