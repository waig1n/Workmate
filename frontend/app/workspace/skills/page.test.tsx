import { resourcePageMock } from "@/test/resourcePages";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, test, vi } from "vitest";
import SkillsPage from "./page";
import { api } from "@/lib/api";
vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn(), upload: vi.fn() } }));
vi.mock("@/lib/usePermissions", () => ({ usePermissions: () => ({ isLoading: false, isError: false }) }));
test("skills show purpose first and keep sharing and removal inside details", async () => {
  vi.mocked(api.get).mockImplementation(resourcePageMock(async (path) => {
    if (path === "/skills") return [{ id: "s1", name: "reports", display_name: "报告整理", scope_id: "a", scope_kind: "department", manageable: true, enabled: true, metadata: { description: "整理会议记录与行动事项" } }];
    return [];
  }));
  render(<QueryClientProvider client={new QueryClient()}><SkillsPage /></QueryClientProvider>);
  await screen.findByText("报告整理");
  expect(screen.getByText("整理会议记录与行动事项")).toBeVisible();
  expect(screen.queryByText("共享")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("搜索技能库"), { target: { value: "找不到" } });
  expect(await screen.findByText("没有匹配的技能")).toBeVisible();
  fireEvent.change(screen.getByLabelText("搜索技能库"), { target: { value: "行动事项" } });
  fireEvent.click(await screen.findByRole("button", { name: /报告整理/ }));
  expect(screen.getByRole("dialog")).toBeVisible();
  expect(screen.getByText("共享")).toBeVisible();
  fireEvent.click(screen.getByText("移除技能"));
  expect(api.delete).not.toHaveBeenCalled();
  expect(screen.getByText("确认移除")).toBeVisible();
});

const uploaded = { id: "u1", name: "uploaded-report", display_name: "上传报告", scope_id: "org", scope_kind: "organization", manageable: true, usable: false, enabled: true, metadata: { hub_id: "uploaded", creation_channel: "upload" } };
beforeEach(() => vi.resetAllMocks());
function mockLibrary() {
  vi.mocked(api.get).mockImplementation(resourcePageMock(async (path) => {
    if (path === "/skills") return [uploaded, { ...uploaded, id: "b1", name: "builtin", display_name: "内置技能", metadata: { hub_id: "workmate-builtin" } }];
    if (path === "/scopes/accessible") return [{ id: "org", name: "总部", kind: "organization", permissions: ["skill.manage"] }];
    if (path === "/skills/hubs") return [{ hub_id: "uploaded", display_name: "Uploaded" }, { hub_id: "workmate-builtin", display_name: "Builtin" }];
    if (path.startsWith("/skills/catalog")) return { cards: [] };
    return [];
  }));
  render(<QueryClientProvider client={new QueryClient()}><SkillsPage /></QueryClientProvider>);
}
test("custom skills are filtered in the library and excluded from import sources", async () => {
  mockLibrary();
  await screen.findByText("上传报告");
  fireEvent.change(screen.getByLabelText("筛选技能来源"), { target: { value: "uploaded" } });
  expect(await screen.findByText("上传报告")).toBeVisible();
  expect(screen.getByRole("option", { name: "自建技能" })).toBeVisible();
  expect(screen.getByRole("button", { name: /上传报告.*自建技能.*文件上传/ })).toBeVisible();
  expect(screen.queryByText("内置技能")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "添加技能" }));
  expect(screen.queryByLabelText("管理归属")).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Builtin" })).not.toBeInTheDocument();
  expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining("hub_id=workmate-builtin"));
  expect(screen.queryByRole("option", { name: "Uploaded" })).not.toBeInTheDocument();
  expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining("hub_id=uploaded"));
});
test("upload returns to the library and opens sharing without a second install", async () => {
  mockLibrary();
  vi.mocked(api.upload).mockResolvedValue(uploaded);
  fireEvent.click(await screen.findByRole("button", { name: "添加技能" }));
  fireEvent.change(screen.getByLabelText("上传 ZIP"), { target: { files: [new File(["zip"], "reports.zip", { type: "application/zip" })] } });
  await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(1));
  await screen.findByRole("dialog", { name: "上传报告" });
  expect(screen.queryByRole("dialog", { name: "添加技能" })).not.toBeInTheDocument();
  expect(screen.getByText("共享")).toBeVisible();
  expect(screen.getByText(/入库不自动开放使用/)).toBeVisible();
  expect(api.post).not.toHaveBeenCalled();
  expect((vi.mocked(api.upload).mock.calls[0][1] as FormData).has("scope_id")).toBe(false);
});

test("catalog import requires confirmation and resets library filters to the new source", async () => {
  const imported = { ...uploaded, id: "b2", name: "meeting-notes", display_name: "会议纪要", metadata: { hub_id: "skillhub-cn" } };
  let complete = false;
  vi.mocked(api.get).mockImplementation(resourcePageMock(async (path) => {
    if (path === "/skills") return complete ? [uploaded, imported] : [uploaded];
    if (path === "/scopes/accessible") return [{ id: "org", name: "总部", kind: "organization", permissions: ["skill.manage"] }];
    if (path === "/skills/hubs") return [{ hub_id: "skillhub-cn", display_name: "第三方来源" }];
    if (path.startsWith("/skills/catalog")) return { cards: [{ id: "meeting-notes", name: "meeting-notes", display_name: "会议纪要", hub_id: "skillhub-cn", description: "整理会议决策" }] };
    return [];
  }));
  vi.mocked(api.post).mockImplementation(async () => { complete = true; return imported; });
  render(<QueryClientProvider client={new QueryClient()}><SkillsPage /></QueryClientProvider>);
  await screen.findByText("上传报告");
  fireEvent.change(screen.getByLabelText("筛选技能来源"), { target: { value: "uploaded" } });
  fireEvent.change(screen.getByLabelText("搜索技能库"), { target: { value: "上传报告" } });
  fireEvent.click(screen.getByRole("button", { name: "添加技能" }));
  fireEvent.click(await screen.findByRole("button", { name: "查看并入库" }));
  expect(screen.getByRole("dialog", { name: "确认入库 会议纪要" })).toBeVisible();
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认入库" }));
  await screen.findByRole("dialog", { name: "会议纪要" });
  expect(api.post).toHaveBeenCalledExactlyOnceWith("/skills", { hub_id: "skillhub-cn", card_id: "meeting-notes", confirmed: true });
  expect(screen.getByText(/入库不自动开放使用/)).toBeVisible();
  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByLabelText("筛选技能来源")).toHaveValue("third-party");
  expect(screen.queryByLabelText("筛选管理归属")).not.toBeInTheDocument();
  expect(screen.getByLabelText("搜索技能库")).toHaveValue("");
  expect(await screen.findByRole("button", { name: /会议纪要/ })).toBeVisible();
});

test("department-only managers cannot add to the organization skill library", async () => {
  vi.mocked(api.get).mockImplementation(resourcePageMock(async path => path === "/skills" ? [uploaded] : path === "/scopes/accessible" ? [{ id: "org", kind: "organization", permissions: [] }, { id: "dept", kind: "department", permissions: ["skill.manage"] }] : []));
  render(<QueryClientProvider client={new QueryClient()}><SkillsPage /></QueryClientProvider>);
  await screen.findByText("上传报告");
  expect(screen.queryByRole("button", { name: "添加技能" })).not.toBeInTheDocument();
  expect(api.get).not.toHaveBeenCalledWith("/skills/hubs");
});
test("builtins are already in the library with source labels and no import action", async () => {
  mockLibrary();
  await screen.findByText("内置技能");
  fireEvent.change(screen.getByLabelText("筛选技能来源"), { target: { value: "builtin" } });
  expect(await screen.findByRole("button", { name: /内置技能.*平台内置/ })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /内置技能/ }));
  expect(screen.queryByText(/第三方技能可能/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /入库|安装/ })).not.toBeInTheDocument();
  expect(api.post).not.toHaveBeenCalled();
});

test("builtins can be disabled and enabled but cannot be removed", async () => {
  mockLibrary();
  vi.mocked(api.patch).mockImplementation(async (_path, body) => ({ ...uploaded, id: "b1", name: "builtin", display_name: "内置技能", enabled: (body as { enabled: boolean }).enabled, metadata: { hub_id: "workmate-builtin" } }));
  fireEvent.click(await screen.findByRole("button", { name: /内置技能/ }));
  expect(screen.queryByRole("button", { name: "移除技能" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "停用技能" }));
  await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/skills/b1", { enabled: false }));
  fireEvent.click(await screen.findByRole("button", { name: "启用技能" }));
  await waitFor(() => expect(api.patch).toHaveBeenLastCalledWith("/skills/b1", { enabled: true }));
  expect(api.delete).not.toHaveBeenCalled();
});

test("builtin disable errors retain the enabled state and explain bound Agent dependencies", async () => {
  mockLibrary();
  vi.mocked(api.patch).mockRejectedValue(new Error("技能仍被智能体绑定，请先解除绑定"));
  fireEvent.click(await screen.findByRole("button", { name: /内置技能/ }));
  fireEvent.click(screen.getByRole("button", { name: "停用技能" }));
  expect(await screen.findAllByText("技能仍被智能体绑定，请先解除绑定")).toHaveLength(2);
  expect(screen.getByRole("button", { name: "停用技能" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "启用技能" })).not.toBeInTheDocument();
});

test("migrated skill names retain their department provenance without ownership controls", async () => {
  vi.mocked(api.get).mockImplementation(resourcePageMock(async path => path === "/skills" ? [{ ...uploaded, metadata: { hub_id: "uploaded", central_library_origin: { scope_id: "old-dept", scope_name: "原研发部" } } }] : []));
  render(<QueryClientProvider client={new QueryClient()}><SkillsPage /></QueryClientProvider>);
  const card = await screen.findByRole("button", { name: /上传报告.*历史来源：原研发部/ });
  fireEvent.click(card);
  expect(screen.getAllByText(/历史来源：原研发部/)).toHaveLength(2);
  expect(screen.queryByLabelText("管理归属")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("筛选管理归属")).not.toBeInTheDocument();
});

test("ordinary Skill users see only the summary, never cached full instructions or a request for them", async () => {
 const readable = { ...uploaded, manageable: false, usable: true, metadata: { description: "公开的技能简介" } };
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => path === "/skills" ? [readable] : []));
 const client = new QueryClient();
 client.setQueryData(["skill-description", uploaded.id], { description: "旧内部描述", markdown: "机密技能实现" });
 render(<QueryClientProvider client={client}><SkillsPage /></QueryClientProvider>);
 fireEvent.click(await screen.findByRole("button", { name: /上传报告/ }));
 expect(screen.getAllByText("公开的技能简介").length).toBeGreaterThan(0);
 expect(screen.queryByText("技能说明")).not.toBeInTheDocument();
 expect(screen.queryByText("机密技能实现")).not.toBeInTheDocument();
 expect(api.get).not.toHaveBeenCalledWith(`/skills/${uploaded.id}/description`);
});
test("Skill managers can expand full instructions", async () => {
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => path === "/skills" ? [uploaded] : path.endsWith("/description") ? { description: "摘要", markdown: "管理员可读全文" } : []));
 const view = render(<QueryClientProvider client={new QueryClient()}><SkillsPage /></QueryClientProvider>);
 fireEvent.click(await screen.findByRole("button", { name: /上传报告/ }));
 const disclosure = view.baseElement.querySelector("details")!;
 disclosure.open = true; fireEvent(disclosure, new Event("toggle"));
 expect(await screen.findByText("管理员可读全文")).toBeVisible();
});

test("revoking management while Skill details are open removes the full-text view", async () => {
 let manageable = true;
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => path === "/skills" ? [{ ...uploaded, manageable, usable: true }] : path.endsWith("/description") ? { description: "内部摘要", markdown: "撤权前全文" } : []));
 const client = new QueryClient();
 const view = render(<QueryClientProvider client={client}><SkillsPage /></QueryClientProvider>);
 fireEvent.click(await screen.findByRole("button", { name: /上传报告/ }));
 const disclosure = view.baseElement.querySelector("details")!;
 disclosure.open = true; fireEvent(disclosure, new Event("toggle"));
 await screen.findByText("撤权前全文");
 manageable = false;
 await act(async () => { await client.invalidateQueries({ queryKey: ["skills", "page"] }); });
 await waitFor(() => expect(screen.queryByText("技能说明")).not.toBeInTheDocument());
 expect(screen.queryByText("撤权前全文")).not.toBeInTheDocument();
 expect(screen.getByRole("dialog", { name: "上传报告" })).toBeVisible();
});
