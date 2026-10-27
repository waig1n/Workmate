import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PendingConfirm } from "@/lib/types";
import { TaskSecurityPanel } from "./TaskSecurityPanel";

const pending: PendingConfirm = {
  toolCalls: [{ id: "tool-1", name: "bash", input: '{"command":"npm test"}' }],
  decisionVersion: "decision-1",
  taskId: "task-1",
};

describe("TaskSecurityPanel", () => {
  it("uses an in-product confirmation dialog before enabling full access", () => {
    const onEnable = vi.fn();
    const nativeConfirm = vi.spyOn(window, "confirm");
    const { rerender } = render(<TaskSecurityPanel pending={pending} onEnable={onEnable} />);

    fireEvent.click(screen.getByText("审批设置"));
    fireEvent.click(screen.getByRole("checkbox", { name: "我理解该任务后续普通操作将不再逐项询问" }));
    fireEvent.click(screen.getByRole("button", { name: "为当前任务开启自动执行" }));

    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "确认开启自动执行？" })).toBeVisible();
    expect(onEnable).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确认，仅为当前任务开启" }));
    expect(onEnable).toHaveBeenCalledWith([{ tool_call_id: "tool-1", approved: true }]);
    rerender(<TaskSecurityPanel pending={pending} onEnable={onEnable} error="网络错误" />);
    expect(screen.getByRole("alert")).toHaveTextContent("网络错误");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

it('does not offer full access for a scheduled unattended task',()=>{
 render(<TaskSecurityPanel pending={{decisionVersion:'v',taskId:'t',approvalProfile:'unattended',toolCalls:[]}} onEnable={vi.fn()}/>);
 expect(screen.queryByRole('button',{name:'为当前任务开启自动执行'})).toBeNull();
 expect(screen.getByText('无人值守任务不能开启自动执行。')).toBeVisible();
});
