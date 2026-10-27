import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import SettingsPage from "./page";
import { api } from "@/lib/api";
vi.mock("@/lib/api", () => ({ setAuth: vi.fn(), api: { me: vi.fn(), get: vi.fn(), organization: vi.fn(), renameOrganization: vi.fn(), updateProfile: vi.fn(), changePassword: vi.fn() } }));
beforeEach(() => { vi.resetAllMocks(); vi.mocked(api.me).mockResolvedValue({ id: "u", email: "u@example.com", display_name: "用户", status: "active", must_change_password: false }); vi.mocked(api.organization).mockResolvedValue({ id: "org", name: "原企业", tree_version: 3 }); });
function mount(manager: boolean) {
 vi.mocked(api.get).mockImplementation(async path => path === "/auth/me/permissions" ? { root_scope_id: "org", permissions: [], personal_scope_id: "personal" } : [{ id: "org", kind: "organization", name: "原企业", permissions: manager ? ["scope.manage"] : [] }]);
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 render(<QueryClientProvider client={client}><SettingsPage /></QueryClientProvider>); return client;
}
test("ordinary users do not request organization administration data", async () => {
 mount(false); await screen.findByDisplayValue("用户");
 expect(screen.queryByRole("heading", { name: "组织设置" })).not.toBeInTheDocument();
 expect(api.organization).not.toHaveBeenCalled();
});
test("organization managers rename with version and refresh scope names", async () => {
 vi.mocked(api.renameOrganization).mockResolvedValue({ id: "org", name: "新企业", tree_version: 4 });
 const client = mount(true); const invalidate = vi.spyOn(client, "invalidateQueries");
 fireEvent.change(await screen.findByLabelText("组织名称"), { target: { value: " 新企业 " } });
 fireEvent.click(screen.getByRole("button", { name: "保存组织名称" }));
 await waitFor(() => expect(api.renameOrganization).toHaveBeenCalledWith("新企业", 3));
 expect(await screen.findByText("组织名称已更新")).toBeVisible();
 expect(invalidate).toHaveBeenCalledWith({ queryKey: ["scopes"] });
});
test("version conflict refreshes the version but preserves the draft until an explicit save", async () => {
 vi.mocked(api.renameOrganization).mockRejectedValue(Object.assign(new Error("组织信息已变更，请重新加载"), { status: 409 }));
 mount(true); fireEvent.change(await screen.findByLabelText("组织名称"), { target: { value: "待保存企业" } });
 fireEvent.click(screen.getByRole("button", { name: "保存组织名称" }));
 expect(await screen.findByRole("alert")).toHaveTextContent("组织信息已变更");
 expect(screen.getByLabelText("组织名称")).toHaveValue("待保存企业");
 vi.mocked(api.organization).mockResolvedValue({ id: "org", name: "别人更新", tree_version: 5 });
 fireEvent.click(screen.getByRole("button", { name: "刷新最新版本" }));
 await screen.findByText("当前组织名称：别人更新，草稿已保留，请核对后保存。");
 expect(screen.getByLabelText("组织名称")).toHaveValue("待保存企业");
 expect(api.renameOrganization).toHaveBeenCalledTimes(1);
 vi.mocked(api.renameOrganization).mockResolvedValue({ id: "org", name: "待保存企业", tree_version: 6 });
 fireEvent.click(screen.getByRole("button", { name: "保存组织名称" }));
 await waitFor(() => expect(api.renameOrganization).toHaveBeenLastCalledWith("待保存企业", 5));
});
