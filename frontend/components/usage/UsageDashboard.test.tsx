import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "@/lib/api";
import UsageDashboard from "./UsageDashboard";
import { formatTokens, usageCsv } from "@/lib/usage";
vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }));
const summary = { task_count: 2, recorded_tasks: 1, input_tokens: 1250, output_tokens: 10, unpriced_tasks: 2, costs: [] };
const report = { since: "2026-09-01", until: "2026-09-14", summary, daily: [{ date: "2026-09-14", ...summary }], groups: [{ id: "u1", name: "用户甲", ...summary }], total_groups: 1, limit: 50, offset: 0 };
function mount() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><UsageDashboard /></QueryClientProvider>); }
beforeEach(() => { vi.mocked(api.get).mockReset(); vi.mocked(api.get).mockImplementation(async (path) => path === "/usage/scopes" ? [{ id: "org", name: "研发组织", kind: "organization" }] : path.startsWith("/usage/options") ? { users: [], models: [], agents: [] } : report); });
test("defaults to personal usage, shows K tokens and unpriced rather than zero cost", async () => {
  mount(); await screen.findByText("用户甲");
  expect(screen.getByLabelText("统计范围")).toHaveValue("");
  expect(screen.getAllByText("1.25 K").length).toBeGreaterThan(0);
  expect(screen.getAllByText("未计价").length).toBeGreaterThan(0);
  expect(screen.getByText(/1 项用量不完整/)).toBeVisible();
});
test("changing scope hides old data immediately and ignores delayed old responses", async () => {
  mount(); await screen.findByText("用户甲");
  vi.mocked(api.get).mockImplementation(() => new Promise(() => {}));
  fireEvent.change(screen.getByLabelText("统计范围"), { target: { value: "org" } });
  expect(screen.queryByText("用户甲")).not.toBeInTheDocument();
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(expect.stringContaining("scope_id=org")));
});
test("query errors offer retry and empty results are explicit", async () => {
  vi.mocked(api.get).mockRejectedValue(new Error("服务暂不可用")); mount();
  await screen.findByText("用量加载失败");
  vi.mocked(api.get).mockImplementation(async (path) => path === "/usage/scopes" ? [] : path.startsWith("/usage/options") ? { users: [], models: [], agents: [] } : { ...report, summary: { ...summary, task_count: 0 }, groups: [], daily: [] });
  fireEvent.click(screen.getByRole("button", { name: "重试加载用量" }));
  await screen.findByText("暂无用量");
});
test("multiple currencies remain separate and partial pricing is explicit", async () => {
  vi.mocked(api.get).mockImplementation(async (path) => path === "/usage/scopes" ? [] : path.startsWith("/usage/options") ? { users: [], models: [], agents: [] } : { ...report, summary: { ...summary, unpriced_tasks: 1, costs: [{ currency: "CNY", amount: "0.123456" }, { currency: "USD", amount: "0.000001" }] } });
  mount(); await screen.findByText(/CNY 0.123456/); expect(screen.getByText(/USD 0.000001/)).toBeVisible(); expect(screen.getByText(/仅含已计价部分/)).toBeVisible();
});
test("K preserves small usage and CSV neutralizes spreadsheet formulas", () => {
  expect(formatTokens(1)).toBe("0.001 K"); expect(formatTokens(0)).toBe("0 K");
  const csv = usageCsv([{ id: "u", name: '=HYPERLINK("bad")', ...summary }], "用户");
  expect(csv).toContain("'=" ); expect(csv).toContain("1250"); expect(csv).toContain("未计价");
});
test("filters, grouping and pagination request server subsets and reset pages", async () => {
  vi.mocked(api.get).mockImplementation(async (path) => path === "/usage/scopes" ? [{ id: "org", name: "研发组织", kind: "organization" }] : path.startsWith("/usage/options") ? { users: [{ id: "u1", name: "成员一" }], models: [{ id: "m1", name: "模型一" }], agents: [{ id: "a1", name: "助手一" }] } : { ...report, total_groups: 51 });
  mount(); await screen.findByText("用户甲");
  fireEvent.change(screen.getByLabelText("统计范围"), { target: { value: "org" } }); await screen.findByText("用户甲");
  fireEvent.change(screen.getByLabelText("成员", { selector: "select" }), { target: { value: "u1" } }); await screen.findByText("用户甲");
  fireEvent.change(screen.getByLabelText("模型", { selector: "select" }), { target: { value: "m1" } }); await screen.findByText("用户甲");
  fireEvent.change(screen.getByLabelText("智能体", { selector: "select" }), { target: { value: "a1" } }); await screen.findByText("用户甲");
  fireEvent.change(screen.getByLabelText("分组方式"), { target: { value: "model" } }); await screen.findByText("用户甲");
  fireEvent.click(screen.getByText("下一页")); await screen.findByText("用户甲");
  expect(api.get).toHaveBeenCalledWith(expect.stringMatching(/scope_id=org&user_id=u1&model_profile=m1&agent_id=a1&group_by=model&limit=50&offset=50/));
  fireEvent.click(screen.getByText("上一页")); await screen.findByText("用户甲");
  fireEvent.change(screen.getByLabelText("开始日期"), { target: { value: "2020-01-01" } });
  expect(screen.getByText(/最多查询 366 天/)).toBeVisible(); expect(screen.getByRole("button", { name: "刷新" })).toBeDisabled(); expect(screen.queryByText("用户甲")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("结束日期"), { target: { value: "2020-01-30" } }); await screen.findByText("用户甲");
  expect(screen.getByLabelText("成员", { selector: "select" })).toHaveValue("");
});
test("export downloads only visible rows and revokes the object URL", async () => {
  const create = vi.fn(() => "blob:usage"); const revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  mount(); await screen.findByText("用户甲"); fireEvent.click(screen.getByText("导出本页"));
  expect(create).toHaveBeenCalledOnce(); expect(revoke).toHaveBeenCalledWith("blob:usage"); expect(click).toHaveBeenCalledOnce();
  click.mockRestore(); vi.unstubAllGlobals();
});

test("manual refresh reloads data and authorization options, and is disabled while busy or dates invalid", async () => {
  mount(); await screen.findByText("用户甲");
  expect(screen.queryByText(/部门数据可能重叠/)).not.toBeInTheDocument();
  expect(screen.getByText(/已删除会话不计入统计/)).toBeInTheDocument();
  vi.mocked(api.get).mockClear();
  vi.mocked(api.get).mockImplementation(() => new Promise(() => {}));
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(3));
  expect(api.get).toHaveBeenCalledWith("/usage/scopes");
  expect(api.get).toHaveBeenCalledWith(expect.stringContaining("/usage/options?"));
  expect(api.get).toHaveBeenCalledWith(expect.stringContaining("/usage?"));
  expect(screen.getByRole("button", { name: "刷新" })).toBeDisabled();
});

test("ordinary users see only personal controls and model grouping", async () => {
  vi.mocked(api.get).mockImplementation(async (path) => path === "/usage/scopes" ? [] : path.startsWith("/usage/options") ? { users: [], models: [], agents: [] } : report);
  mount(); await screen.findByText("用户甲");
  expect(screen.getByRole("heading", { name: "我的用量" })).toBeVisible();
  expect(screen.queryByLabelText("统计范围")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("成员", { selector: "select" })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "按成员" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("分组方式")).toHaveValue("model");
  expect(api.get).toHaveBeenCalledWith(expect.stringContaining("group_by=model"));
  expect(api.get).not.toHaveBeenCalledWith(expect.stringMatching(/scope_id=|user_id=/));
});

test("authorized viewers get member controls only in a managed scope", async () => {
  mount(); await screen.findByText("用户甲");
  expect(screen.queryByLabelText("成员", { selector: "select" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("统计范围"), { target: { value: "org" } });
  await screen.findByText("用户甲");
  expect(screen.getByLabelText("成员", { selector: "select" })).toBeVisible();
  expect(screen.getByLabelText("分组方式")).toHaveValue("user");
  fireEvent.change(screen.getByLabelText("统计范围"), { target: { value: "" } });
  await screen.findByText("用户甲");
  expect(screen.getByLabelText("分组方式")).toHaveValue("model");
  expect(screen.queryByRole("option", { name: "按成员" })).not.toBeInTheDocument();
});

test.each(["revoked", "failed"])("hides stale managed data when scope refresh is %s", async (state) => {
  mount(); await screen.findByText("用户甲");
  fireEvent.change(screen.getByLabelText("统计范围"), { target: { value: "org" } });
  await screen.findByText("用户甲");
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path === "/usage/scopes") {
      if (state === "failed") throw new Error("offline");
      return [];
    }
    return path.startsWith("/usage/options") ? { users: [], models: [], agents: [] } : report;
  });
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByText("所选范围暂不可用。");
  expect(screen.queryByText("用户甲")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "导出本页" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("统计范围")).toHaveValue("org");
  fireEvent.click(screen.getByRole("button", { name: "查看我的用量" }));
  await screen.findByText("用户甲");
  expect(screen.getByLabelText("分组方式")).toHaveValue("model");
});

test("ignores a late managed response after returning to personal usage", async () => {
  mount(); await screen.findByText("用户甲");
  let resolve!: (data: typeof report) => void;
  vi.mocked(api.get).mockImplementation(async (path) => path.startsWith("/usage/options")
    ? { users: [], models: [], agents: [] }
    : path.includes("scope_id=org") ? new Promise<typeof report>(done => { resolve = done; }) : report);
  fireEvent.change(screen.getByLabelText("统计范围"), { target: { value: "org" } });
  await waitFor(() => expect(resolve).toBeDefined());
  fireEvent.change(screen.getByLabelText("统计范围"), { target: { value: "" } });
  resolve({ ...report, groups: [{ ...report.groups[0], name: "旧管理数据" }] });
  await screen.findByText("用户甲");
  expect(screen.queryByText("旧管理数据")).not.toBeInTheDocument();
});
