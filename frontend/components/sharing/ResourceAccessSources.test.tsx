import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "@/lib/api";
import { ResourceAccessSources } from "./ResourceAccessSources";
vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }));
beforeEach(() => vi.resetAllMocks());
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ResourceAccessSources kind="knowledge-bases" resourceId="kb" /></QueryClientProvider>); }
test("shows only returned personal access sources and separates download permission", async () => {
 vi.mocked(api.get).mockResolvedValue({ can_use: true, allow_download: false, sources: [{ target_scope_id: "finance", target_name: "财务部", include_descendants: true, inherited: true, allow_download: false }] });
 mount();
 expect(await screen.findByText(/财务部：来自上级部门的共享（含全部下级）/)).toBeVisible();
 expect(screen.getByText("可检索，不可下载原文")).toBeVisible();
 expect(api.get).toHaveBeenCalledWith("/sharing/knowledge-bases/kb/access");
});
test("does not turn a source load failure into a permission denial", async () => {
 vi.mocked(api.get).mockRejectedValue(new Error("网络错误")); mount();
 expect(await screen.findByText("使用权限来源暂时无法读取")).toBeVisible();
 expect(screen.queryByText("尚未获得使用授权")).not.toBeInTheDocument();
});
test("reports no usable grant without disclosing other recipients", async () => {
 vi.mocked(api.get).mockResolvedValue({ can_use: false, allow_download: false, sources: [] }); mount();
 expect(await screen.findByText("尚未获得使用授权")).toBeVisible();
});

test("identifies organization direct grants and each download source", async () => {
 vi.mocked(api.get).mockResolvedValue({ can_use: true, allow_download: true, sources: [{ target_scope_id: "org", target_name: "全组织", target_kind: "organization", target_parent_id: null, include_descendants: false, inherited: false, allow_download: true }] });
 mount();
 expect(await screen.findByText(/仅组织成员/)).toBeVisible();
 expect(screen.getByText(/此授权允许下载原文/)).toBeVisible();
});
