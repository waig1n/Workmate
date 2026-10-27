import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { DepartmentTransfer } from "./department-transfer";
import type { AdminScopeNode } from "@/lib/types";
const scopes = [
  { id: "finance", name: "财务部", kind: "department", children: [], permissions: [] },
  { id: "sales", name: "销售部", kind: "department", children: [], permissions: [] },
] as unknown as AdminScopeNode[];
test("调岗显示失去和获得的部门使用范围，职责默认撤销且可明确保留", () => {
  const onRetain = vi.fn();
  render(<DepartmentTransfer scopes={scopes} before={["root", "finance"]} after={["root", "sales"]} bindings={[{ scope_id: "finance", role_key: "editor", include_descendants: false }]} roles={[]} canBind retain={false} onRetain={onRetain} />);
  expect(screen.getByText("移出：财务部")).toBeVisible();
  expect(screen.getByText("加入：销售部")).toBeVisible();
  expect(screen.getByText(/将撤销以下原部门管理职责/)).toBeVisible();
  expect(screen.getByLabelText("保留上述职责，继续跨部门负责")).not.toBeChecked();
  fireEvent.click(screen.getByLabelText("保留上述职责，继续跨部门负责"));
  expect(onRetain).toHaveBeenCalledWith(true);
});
test("成员管理员遇到原部门职责时显示需要授权管理员处理", () => {
  render(<DepartmentTransfer scopes={scopes} before={["finance"]} after={[]} bindings={[{ scope_id: "finance", role_key: "editor", include_descendants: false }]} roles={[]} canBind={false} retain={false} onRetain={vi.fn()} />);
  expect(screen.getByRole("alert")).toHaveTextContent("需要有职责授权权限的管理员完成此次调岗");
  expect(screen.queryByLabelText("保留上述职责，继续跨部门负责")).not.toBeInTheDocument();
});
