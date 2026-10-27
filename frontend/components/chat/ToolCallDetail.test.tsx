import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ToolCallDetail } from "./ToolCallDetail";

describe("ToolCallDetail", () => {
  it("keeps Bash collapsed with a command preview and reveals full output", () => {
    render(<ToolCallDetail item={{
      kind: "tool",
      id: "tool-1",
      runId: "reply-1",
      call: {
        toolCallId: "tool-1",
        toolCallName: "Bash",
        args: JSON.stringify({ command: "rg --files docs" }),
        result: "docs/readme.md",
        state: "success",
      },
    }} />);

    expect(screen.getByText("rg --files docs")).toBeTruthy();
    expect(screen.queryByText("docs/readme.md")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Bash/ }));
    expect(screen.getByText("docs/readme.md")).toBeTruthy();
  });
});

it("bounds Write content until explicitly opened", () => {
  const content = "x".repeat(8000) + "SECRET_TAIL";
  render(<ToolCallDetail defaultOpen item={{ kind: "tool", id: "w", call: { toolCallId: "w", toolCallName: "Write", args: JSON.stringify({ file_path: "/workspace/a.py", content }), result: "ok", state: "success" } }} />);
  expect(screen.getByText("/workspace/a.py")).toBeTruthy();
  expect(document.body.textContent).not.toContain("SECRET_TAIL");
  fireEvent.click(screen.getByRole("button", { name: "查看完整参数" }));
  expect(document.body.textContent).toContain("SECRET_TAIL");
});

it("previews Edit changes and bounds long results independently", () => {
  render(<ToolCallDetail defaultOpen item={{ kind: "tool", id: "edit", call: { toolCallId: "edit", toolCallName: "Edit", args: JSON.stringify({ file_path: "report.md", old_string: "旧内容", new_string: "新内容" }), result: "line\n".repeat(50) + "RESULT_TAIL", state: "success" } }} />);
  expect(screen.getByText(/修改前：/)).toHaveTextContent("旧内容");
  expect(screen.getByText(/修改后：/)).toHaveTextContent("新内容");
  expect(document.body.textContent).not.toContain("RESULT_TAIL");
  fireEvent.click(screen.getByRole("button", { name: "查看完整结果" }));
  expect(document.body.textContent).toContain("RESULT_TAIL");
});

it("bounds partial JSON while arguments are streaming", () => {
  render(<ToolCallDetail defaultOpen item={{ kind: "tool", id: "partial", call: { toolCallId: "partial", toolCallName: "Write", args: '{"content":"' + "a".repeat(5000) + "TAIL", result: "", state: "running" } }} />);
  expect(document.body.textContent).not.toContain("TAIL");
  expect(screen.getByText("(等待结果…)" )).toBeTruthy();
});

it("loads referenced content only on explicit action using authenticated task API", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => "FULL_OBJECT_CONTENT" });
  vi.stubGlobal("fetch", fetchMock);
  localStorage.setItem("workmate_token", "test-token");
  try {
    render(<ToolCallDetail defaultOpen item={{ kind: "tool", id: "object", taskId: "task", content: { arguments: { sha256: "a".repeat(64), sizeBytes: 8000, preview: "bounded preview", incomplete: false } }, call: { toolCallId: "object", toolCallName: "Write", args: "bounded preview", result: "ok", state: "success" } }} />);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "查看完整参数" }));
    expect(await screen.findByText("FULL_OBJECT_CONTENT")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledWith("/api/tasks/task/tools/object/content?kind=arguments", { headers: { Authorization: "Bearer test-token" } });
  } finally { vi.unstubAllGlobals(); localStorage.removeItem("workmate_token"); }
});

it("retains the preview and shows a retryable failure when object access fails", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: false });
  vi.stubGlobal("fetch", fetchMock);
  try {
    render(<ToolCallDetail defaultOpen item={{ kind: "tool", id: "denied", taskId: "task", content: { result: { sha256: "b".repeat(64), sizeBytes: 8000, preview: "safe preview", incomplete: true } }, call: { toolCallId: "denied", toolCallName: "Bash", args: "{}", result: "safe preview", state: "success" } }} />);
    fireEvent.click(screen.getByRole("button", { name: "查看完整结果" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("完整内容暂时无法读取，请重试。");
    expect(screen.getByText("safe preview")).toBeVisible();
    expect(screen.getByText("工具内容不完整")).toBeVisible();
  } finally { vi.unstubAllGlobals(); }
});

it("reads large diff metadata separately without replacing tool results", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, text: async () => '{"diff":"FULL_DIFF"}' }));
  try {
    render(<ToolCallDetail defaultOpen item={{ kind: "tool", id: "diff", taskId: "task", content: { metadata: { sha256: "c".repeat(64), sizeBytes: 8000, preview: "diff preview", incomplete: false } }, call: { toolCallId: "diff", toolCallName: "Write", args: "{}", result: "write successful", state: "success" } }} />);
    expect(screen.getByText("diff preview")).toBeVisible();
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "查看完整修改详情" }));
    expect(await screen.findByText('{"diff":"FULL_DIFF"}')).toBeVisible();
    expect(screen.getByText("write successful")).toBeVisible();
    expect(fetch).toHaveBeenCalledWith("/api/tasks/task/tools/diff/content?kind=metadata", expect.anything());
  } finally { vi.unstubAllGlobals(); }
});

it('shows unknown outcome without pretending a stopped tool succeeded or still runs',()=>{
  render(<ToolCallDetail defaultOpen item={{kind:'tool',id:'uncertain',call:{toolCallId:'uncertain',toolCallName:'Write',args:'{}',result:'',state:'unknown'}}}/>);
  expect(screen.getByText('结果未知')).toBeInTheDocument();
  expect(screen.queryByText('(等待结果…)')).not.toBeInTheDocument();
});
