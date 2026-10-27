import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ApprovalCard } from "./ApprovalCard";

describe("resource submission approval", () => {
  it("shows skill scope and explicit confirmation even in full access", () => {
    const onConfirm = vi.fn();
    render(<ApprovalCard pending={{
      decisionVersion: "request-1",
      approvalProfile: "full_access",
      toolCalls: [{ id: "install-1", name: "skill_install", input: JSON.stringify({
        display_name: "Report skill", scope_id: "department-1", expected_sha256: "abc",
      }) }],
    }} onConfirm={onConfirm} />);
    expect(screen.getByText("确认安装技能")).toBeInTheDocument();
    expect(screen.getByText("所属范围：department-1")).toBeInTheDocument();
    expect(screen.getByText("仅安装技能，不绑定任何智能体")).toBeInTheDocument();
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "允许本次" }));
    expect(onConfirm).toHaveBeenCalledWith([{ tool_call_id: "install-1", approved: true }]);
  });
});

it("keeps explicit per-operation choices while submitting and shows retry errors", () => {
  const onConfirm = vi.fn();
  const pending = { decisionVersion: "batch", toolCalls: [
    { id: "one", name: "delete", input: '{"path":"/important/report.md"}' },
    { id: "two", name: "send_email", input: '{"to":"client@example.com"}' },
  ] };
  const { rerender } = render(<ApprovalCard pending={pending} onConfirm={onConfirm} />);
  expect(screen.getByRole("button", { name: "提交选择" })).toBeDisabled();
  fireEvent.click(screen.getAllByRole("button", { name: "允许" })[0]);
  fireEvent.click(screen.getAllByRole("button", { name: "拒绝" })[1]);
  rerender(<ApprovalCard pending={pending} onConfirm={onConfirm} submitting />);
  expect(screen.getAllByRole("button", { name: "允许" })[0]).toBeDisabled();
  rerender(<ApprovalCard pending={pending} onConfirm={onConfirm} error="网络错误" />);
  expect(screen.getByRole("alert")).toHaveTextContent("网络错误");
  fireEvent.click(screen.getByRole("button", { name: "提交选择" }));
  expect(onConfirm).toHaveBeenCalledWith([{ tool_call_id: "one", approved: true }, { tool_call_id: "two", approved: false }]);
  expect(screen.getByText("目标：/important/report.md")).toBeVisible();
});

it("exposes sensitive agent changes without needing to expand raw parameters", () => {
  render(<ApprovalCard pending={{ decisionVersion: "update", toolCalls: [{ id: "update", name: "agent_update", input: JSON.stringify({
    agent_id: "agent-1", description: "", visibility: "public", skill_binding_ids: [], mcp_binding_ids: ["connector"], system_prompt: "new instructions", is_default: true, include_descendants: true,
  }) }] }} onConfirm={vi.fn()} />);
  expect(screen.getByText("简介说明：清空")).toBeVisible();
  expect(screen.getByText("可见性：公开")).toBeVisible();
  expect(screen.getByText("技能绑定：无（清空）")).toBeVisible();
  expect(screen.getByText("包含提示词变更，可展开详情核对全文")).toBeVisible();
});

it.each([["delete", "确认删除"], ["send_email", "确认发送"]])("names the %s confirmation explicitly and permits refusal", (name, label) => {
  const onConfirm = vi.fn();
  render(<ApprovalCard pending={{ decisionVersion: name, toolCalls: [{ id: "call", name, input: "invalid json" }] }} onConfirm={onConfirm} />);
  expect(screen.getByRole("button", { name: label })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "拒绝本次" }));
  expect(onConfirm).toHaveBeenCalledWith([{ tool_call_id: "call", approved: false }]);
});

it("does not offer approval actions without a pending operation", () => {
  render(<ApprovalCard pending={{ decisionVersion: "empty", toolCalls: [] }} onConfirm={vi.fn()} />);
  expect(screen.getByRole("button", { name: "提交选择" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "拒绝当前全部" })).toBeDisabled();
});
