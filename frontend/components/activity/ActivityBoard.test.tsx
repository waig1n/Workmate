import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Task } from "@/lib/types";
import { ActivityBoard, ActivityLauncher } from "./ActivityBoard";

vi.mock("@/components/activity/RunToolDetails", () => ({ RunToolDetails: () => <div>工具详情</div> }));
vi.mock("@/components/TaskKnowledgeSources", () => ({ TaskKnowledgeSources: () => <div>检索详情</div> }));
vi.mock("@/components/activity/TaskTimeline", () => ({ TaskTimeline: ({ task }: { task: Task | null }) => <div>时间线 {task?.id}</div> }));

function task(id: string): Task {
  return {
    id,
    conversation_id: "conversation-1",
    title: id,
    status: "completed",
    run_id: `reply-${id}`,
    approval_profile: "standard",
    error: null,
    duration_ms: 10,
    steps: 1,
    model_name: "model",
    token_usage: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  };
}

describe("ActivityBoard", () => {
  it("opens from a lightweight floating launcher", () => {
    const onOpen = vi.fn();
    render(<ActivityLauncher task={task("running")} onOpen={onOpen} />);

    fireEvent.click(screen.getByRole("button", { name: "执行活动" }));
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("keeps Product Task navigation in the compact activity board", () => {
    const onSelectTask = vi.fn();
    const tasks = [task("latest"), task("older")];
    render(<ActivityBoard tasks={tasks} task={tasks[1]} toolItems={[]} onSelectTask={onSelectTask} onClose={vi.fn()} />);

    expect(screen.getByRole("region", { name: "执行动态看板" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "选择执行记录" }), { target: { value: "latest" } });
    expect(onSelectTask).toHaveBeenCalledWith("latest");
    fireEvent.click(screen.getByRole("button", { name: "回到最新" }));
    expect(onSelectTask).toHaveBeenLastCalledWith("latest");
  });
  it("keeps usage collapsed and shows the selected task's recorded zero values", () => {
    const selected = { ...task("older"), model_name: "审阅模型", duration_ms: 0, token_usage: { input_tokens: 0, output_tokens: 42 } };
    const { rerender } = render(<ActivityBoard tasks={[task("latest"), selected]} task={selected} toolItems={[]} onSelectTask={vi.fn()} onClose={vi.fn()} />);
    const summary = screen.getByText("用量与执行信息");
    const details = summary.closest("details")!;
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(summary);
    const usage = within(details);
    expect(usage.getByText("审阅模型")).toBeVisible();
    expect(usage.getByText("0")).toBeVisible();
    expect(usage.getByText("42")).toBeVisible();
    expect(usage.getByText("0.0 秒")).toBeVisible();
    const next = { ...task("latest"), model_name: null, duration_ms: null, token_usage: { input_tokens: 11 } };
    rerender(<ActivityBoard tasks={[next, selected]} task={next} toolItems={[]} onSelectTask={vi.fn()} onClose={vi.fn()} />);
    expect(screen.queryByText("审阅模型")).not.toBeInTheDocument();
    expect(screen.getByText("11")).toBeInTheDocument();
    expect(screen.getAllByText("未记录")).toHaveLength(3);
  });

  it("does not invent token counts when no usage was recorded", () => {
    const selected = task("latest");
    render(<ActivityBoard tasks={[selected]} task={selected} toolItems={[]} onSelectTask={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getAllByText("未记录")).toHaveLength(2);
  });

});
