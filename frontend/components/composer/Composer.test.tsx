import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";

describe("Composer", () => {
  it("does not submit Enter while an IME composition is active", () => {
    const submit = vi.fn();
    render(createElement(Composer, {
      input: "中文输入",
      onInputChange: vi.fn(),
      attachments: [],
      onAttachmentsChange: vi.fn(),
      approvalProfile: "standard",
      onApprovalProfileChange: vi.fn(),
      runningTaskProfile: null,
      disabled: false,
      busy: false,
      sending: false,
      error: null,
      placeholder: "输入任务",
      onSubmit: submit,
    }));
    const textarea = screen.getByRole("textbox", { name: "任务描述" });
    fireEvent.keyDown(textarea, { key: "Enter", isComposing: true });
    expect(submit).not.toHaveBeenCalled();
    fireEvent.keyDown(textarea, { key: "Enter", isComposing: false });
    expect(submit).toHaveBeenCalledOnce();
  });

  it("keeps the next-task selector available while showing the frozen profile", () => {
    const change = vi.fn();
    render(createElement(Composer, {
      input: "",
      onInputChange: vi.fn(),
      attachments: [],
      onAttachmentsChange: vi.fn(),
      approvalProfile: "strict",
      onApprovalProfileChange: change,
      runningTaskProfile: "standard",
      disabled: false,
      busy: true,
      sending: false,
      error: null,
      placeholder: "输入任务",
      onSubmit: vi.fn(),
    }));
    expect(screen.getByText(/当前任务：重要操作询问/)).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "下一任务审批等级" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "任务描述" })).not.toBeDisabled();
  });

  it("offers the next-task selector when no task is running", () => {
    render(createElement(Composer, {
      input: "",
      onInputChange: vi.fn(),
      attachments: [],
      onAttachmentsChange: vi.fn(),
      approvalProfile: "strict",
      onApprovalProfileChange: vi.fn(),
      runningTaskProfile: null,
      disabled: false,
      busy: false,
      sending: false,
      error: null,
      placeholder: "输入任务",
      onSubmit: vi.fn(),
    }));
    expect(screen.getByRole("combobox", { name: "下一任务审批等级" })).toBeTruthy();
    expect(screen.queryByText(/当前任务：/)).toBeNull();
  });
  it("explains execution choices with keyboard and touch accessible help", async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    render(createElement(Composer, {
      input: "", onInputChange: vi.fn(), attachments: [], onAttachmentsChange: vi.fn(),
      approvalProfile: "standard", onApprovalProfileChange: change,
      runningTaskProfile: null, disabled: false, busy: false, sending: false,
      error: null, placeholder: "输入任务", onSubmit: vi.fn(),
    }));
    const help = screen.getByRole("button", { name: "执行方式说明" });
    expect(help).toHaveAttribute("aria-expanded", "false");
    help.focus();
    await user.keyboard("{Enter}");
    expect(help).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("仅影响下一次发送，不会更改正在执行的任务。")).toBeVisible();
    expect(screen.getByText(/自动执行仍须在隔离沙箱内运行/)).toBeVisible();
    await user.click(help);
    expect(help).toHaveAttribute("aria-expanded", "false");
    await user.selectOptions(screen.getByRole("combobox"), "strict");
    expect(change).toHaveBeenCalledWith("strict");
    expect(screen.getByRole("option", { name: "仅查看分析" })).toHaveValue("read_only");
    expect(screen.getByRole("option", { name: "自动执行" })).toHaveValue("full_access");
  });

});

it("switches the primary action between stop and queued send without stopping on Enter", () => {
  const onStop = vi.fn();
  const onSubmit = vi.fn();
  const props = { input: "", onInputChange: vi.fn(), attachments: [], onAttachmentsChange: vi.fn(),
    approvalProfile: "standard" as const, onApprovalProfileChange: vi.fn(), runningTaskProfile: null,
    disabled: false, busy: true, sending: false, error: null, placeholder: "输入任务", onSubmit, onStop };
  const { rerender } = render(createElement(Composer, props));
  expect(screen.queryByText("正在处理…")).toBeNull();
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  expect(onStop).not.toHaveBeenCalled();
  expect(onSubmit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "停止执行" }));
  expect(onStop).toHaveBeenCalledOnce();
  rerender(createElement(Composer, { ...props, input: "下一项工作" }));
  fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
  expect(onSubmit).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "停止执行" })).toBeVisible();
  rerender(createElement(Composer, { ...props, attachments: [new File(["data"], "input.txt")] }));
  expect(screen.getByRole("button", { name: "发送任务" })).not.toBeDisabled();
  rerender(createElement(Composer, { ...props, stopping: true }));
  expect(screen.getByRole("button", { name: "正在停止" })).toBeDisabled();
});

it("keeps stop errors actionable without a separate activity indicator", () => {
  const props = { input: "", onInputChange: vi.fn(), attachments: [], onAttachmentsChange: vi.fn(),
    approvalProfile: "standard" as const, onApprovalProfileChange: vi.fn(), runningTaskProfile: null,
    disabled: false, busy: true, sending: false, error: null, placeholder: "输入任务", onSubmit: vi.fn(), onStop: vi.fn() };
  const { rerender } = render(createElement(Composer, { ...props }));
  expect(screen.queryByRole("status")).toBeNull();
  rerender(createElement(Composer, { ...props, stopError: "网络错误" }));
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent("停止失败");
  expect(screen.getByRole("button", { name: "停止执行" })).not.toBeDisabled();
  rerender(createElement(Composer, { ...props, busy: false }));
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.getByRole("button", { name: "发送任务" })).toBeDisabled();
});
