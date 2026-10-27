import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ResourceToolbar } from "./ResourceToolbar";

it("exposes the selected filter and does not submit an enclosing form", () => {
  const change = vi.fn();
  const submit = vi.fn(event => event.preventDefault());
  const search = vi.fn();
  render(<form onSubmit={submit}><ResourceToolbar label="资源筛选" options={[{ value: "all", label: "全部" }, { value: "mine", label: "我的" }]} value="all" onChange={change} searchLabel="搜索资源" placeholder="搜索" search="" onSearch={search} /></form>);
  expect(screen.getByRole("button", { name: "全部" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(screen.getByRole("button", { name: "我的" }));
  expect(change).toHaveBeenCalledWith("mine");
  expect(submit).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("搜索资源"), { target: { value: "报告" } });
  expect(search).toHaveBeenCalledWith("报告");
});
