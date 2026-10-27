import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { PermissionDirectory } from "./roles-permissions";
import type { AdminPermission } from "@/lib/types";

test("权限目录用中文显示内置权限并保留英文技术标识", () => {
  const permission = {
    id: "mcp-manage",
    key: "mcp.manage",
    display_name: "mcp.manage",
    description: "",
    is_system: true,
    has_policy_consumer: true,
    enforcement: "rbac",
    applicable_scope: "all",
    created_at: "",
  } as AdminPermission;

  render(
    <QueryClientProvider client={new QueryClient()}>
      <PermissionDirectory permissions={[permission]} canManage={false} onDone={vi.fn()} />
    </QueryClientProvider>,
  );

  expect(screen.getByText("管理工具连接")).toBeInTheDocument();
  expect(screen.getByText("mcp.manage")).toBeInTheDocument();
});
