import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { TaskProgress } from "./TaskProgress";

it("uses an avatar before the first output and distinct waiting and reconnect states", () => {
  const { container, rerender } = render(<TaskProgress activity={{ label: "正在处理…" }} standalone />);
  expect(screen.getByRole("status", { name: "智能体执行状态" })).toHaveTextContent("正在处理…");
  expect(container.querySelector("svg")).not.toBeNull();
  rerender(<TaskProgress activity={{ label: "等待你的审批", animated: false }} />);
  expect(screen.getByRole("status")).toHaveTextContent("等待你的审批");
  expect(container.querySelector(".run-thinking-dots")).toBeNull();
  rerender(<TaskProgress activity={{ label: "连接中断，正在恢复…" }} />);
  expect(screen.getByRole("status")).toHaveTextContent("连接中断，正在恢复…");
  rerender(<TaskProgress activity={{ label: "正在停止…" }} />);
  expect(screen.getByRole("status")).toHaveTextContent("正在停止…");
});
