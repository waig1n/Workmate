import { resourceResponse } from "./resource-response";
import { expect, test, type Page } from "@playwright/test";

const user = { id: "manager", email: "manager@example.com", display_name: "管理者", status: "active", must_change_password: false };
const scope = { id: "org", name: "总部", kind: "organization", parent_id: null, permissions: ["agent.update", "agent.publish", "knowledge.manage"] };
async function authenticate(page: Page) {
  await page.addInitScript((account) => { localStorage.setItem("workmate_token", "test"); localStorage.setItem("workmate_user", JSON.stringify(account)); }, user);
}

test("knowledge retrieval, original download upgrade and revocation stay separate", async ({ page }) => {
  await authenticate(page);
  let shared = false;
  let allowDownload = false;
  let downloads = 0;
  const previews: boolean[] = [];
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const json = (body: unknown) => route.fulfill({ json: resourceResponse(route.request().url(), body) });
    if (path === "/api/auth/me") return json(user);
    if (path === "/api/auth/me/permissions") return json({ permissions: [], personal_scope_id: "personal", root_scope_id: "org", is_root_admin: true });
    if (path === "/api/scopes/accessible") return json([scope]);
    if (path === "/api/knowledge-bases") return json([{ id: "kb", name: "部门制度", description: "共享资料", scope_id: "org", scope, enabled: true, actions: { use: shared, manage: true, download: allowDownload } }]);
    if (path === "/api/knowledge-bases/kb") return json({ id: "kb", name: "部门制度", description: "共享资料", scope_id: "org", scope, enabled: true, actions: { use: shared, manage: true, download: allowDownload } });
    if (path === "/api/knowledge-bases/kb/documents") return json({ items: [{ id: "doc", filename: "制度.txt", status: "ready", chunk_count: 1 }], total: 1 });
    if (path.endsWith("/content")) { downloads += 1; return route.fulfill({ status: 403, json: { detail: "download revoked" } }); }
    if (path === "/api/sharing/knowledge-bases/kb") return json({ targets: [{ id: "org", name: "全组织可用", kind: "organization" }], shared_scope_ids: shared ? ["org"] : [], grants: shared ? [{ target_scope_id: "org", include_descendants: true, allow_download: allowDownload }] : [] });
    if (path.endsWith("/preview")) {
      const body = route.request().postDataJSON(); previews.push(body.allow_download);
      return json({ resource_id: "kb", name: "部门制度", target_scope_id: "org", target_name: "全组织可用", skills: [], knowledge_bases: [], blockers: [], confirmation: "reviewed" });
    }
    if (path.endsWith("/batch")) {
      const body = route.request().postDataJSON(); shared = true; allowDownload = body.targets[0].allow_download;
      return json({});
    }
    if (path === "/api/sharing/knowledge-bases/kb/org" && method === "DELETE") { shared = false; allowDownload = false; return route.fulfill({ status: 204 }); }
    return json([]);
  });
  await page.goto("/workspace/knowledge-bases");
  await page.getByRole("button", { name: "团队知识库", exact: true }).click();
  await page.getByRole("button", { name: /部门制度/ }).click();
  const detail = page.getByRole("dialog", { name: "部门制度", exact: true });
  await expect(detail.getByText("制度.txt", { exact: true })).toBeVisible();
  await expect(detail.getByRole("button", { name: "下载原文" })).toHaveCount(0);
  await expect(detail.getByLabel("检索问题")).toHaveCount(0);
  await detail.getByRole("button", { name: "共享", exact: true }).click();
  await expect(detail.getByLabel("允许下载原文", { exact: true })).not.toBeChecked();
  await detail.getByLabel("全组织可用", { exact: true }).check();
  await detail.getByRole("button", { name: "共享", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "确认共享", exact: true });
  await expect(confirmation.getByText("仅检索，不允许下载原文")).toBeVisible();
  expect(shared).toBe(false);
  await confirmation.getByRole("button", { name: "确认共享", exact: true }).click();
  await expect(detail.getByLabel("检索问题")).toBeVisible();
  await expect(detail.getByRole("button", { name: "下载原文" })).toHaveCount(0);
  await detail.getByLabel("允许下载原文", { exact: true }).check();
  await detail.getByLabel("全组织可用", { exact: true }).check();
  await detail.getByRole("button", { name: "共享", exact: true }).click();
  await expect(confirmation.getByText("允许检索与下载原文")).toBeVisible();
  await confirmation.getByRole("button", { name: "确认共享", exact: true }).click();
  await detail.getByRole("button", { name: "下载原文" }).click();
  await expect(detail.getByText("原文不存在或当前无权访问")).toBeVisible();
  expect(downloads).toBe(1);
  expect(previews).toEqual([false, true]);
  await detail.getByRole("button", { name: "取消共享", exact: true }).click();
  expect(shared).toBe(true);
  await detail.getByRole("button", { name: "确认取消共享", exact: true }).click();
  await expect(detail.getByRole("button", { name: "下载原文" })).toHaveCount(0);
  await expect(detail.getByLabel("检索问题")).toHaveCount(0);
});

test("sharing a managed draft publishes explicitly and returning private revokes its grants", async ({ page }) => {
  await authenticate(page);
  let visibility = "private";
  let shared = false;
  let saved: Record<string, unknown> | undefined;
  const agent = () => ({ id: "agent", slug: "assistant", display_name: "组织助手", description: "部门办公", scope_id: "org", scope, visibility, include_descendants: true, actions: { use: false, update: true, publish: true, delete: false }, skill_binding_ids: [], mcp_binding_ids: [], unavailable_capability_ids: [], is_default: false, system_prompt: "帮助办公" });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const json = (body: unknown) => route.fulfill({ json: resourceResponse(route.request().url(), body) });
    if (path === "/api/auth/me") return json(user);
    if (path === "/api/auth/me/permissions") return json({ permissions: [], personal_scope_id: "personal", root_scope_id: "org", is_root_admin: true });
    if (path === "/api/scopes/accessible") return json([scope]);
    if (path === "/api/agents") return json([agent()]);
    if (path === "/api/agents/agent" && method === "GET") return json(agent());
    if (path === "/api/agents/agent" && method === "PATCH") { saved = route.request().postDataJSON(); visibility = String(saved!.visibility); if (visibility === "private") shared = false; return json(agent()); }
    if (path === "/api/knowledge-bases") return json([{ id: "kb", name: "研发制度", description: "部门资料", scope_id: "org", scope, enabled: true, actions: { use: false, manage: false, download: false } }]);
    if (path === "/api/sharing/agents/agent") return json({ targets: [{ id: "dept", name: "研发部", kind: "department" }], shared_scope_ids: shared ? ["dept"] : [], grants: [] });
    if (path.endsWith("/preview")) return json({ resource_id: "agent", name: "组织助手", target_scope_id: "dept", target_name: "研发部", skills: [], mcps: [{ id: "mcp", name: "报表连接", scope_id: "org" }], knowledge_bases: [{ id: "kb", name: "研发制度", scope_id: "org" }], blockers: [], confirmation: "reviewed" });
    if (path.endsWith("/batch")) { shared = true; visibility = "public"; return json({}); }
    return json([]);
  });
  await page.goto("/workspace/agents");
  await page.getByRole("button", { name: /组织助手/ }).click();
  await expect(page.getByRole("button", { name: "使用此智能体" })).toHaveCount(0);
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  const knowledge = page.getByLabel(/研发制度/);
  await expect(knowledge).toBeEnabled();
  await knowledge.check();
  await knowledge.uncheck();
  await page.getByRole("button", { name: "发布并设置使用范围", exact: true }).click();
  await page.getByLabel("研发部", { exact: true }).check();
  await page.getByRole("button", { name: "发布并设置使用范围", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "确认共享", exact: true });
  await expect(confirmation.getByText(/确认共享将同时发布此私有草稿/)).toBeVisible();
  await expect(confirmation.getByText("同时共享知识库：研发制度")).toBeVisible();
  await expect(confirmation.getByText("同时共享 MCP 连接：报表连接")).toBeVisible();
  await expect(confirmation.getByText(/复用来源维护的受管连接/)).toBeVisible();
  expect(visibility).toBe("private");
  await confirmation.getByRole("button", { name: "确认共享", exact: true }).click();
  await expect(page.getByText(/已发布，按显式共享授权使用/)).toBeVisible();
  await page.getByText("高级配置：指令、技能与权限", { exact: true }).click();
  await expect(page.getByLabel("包含下级部门", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("radio")).toHaveCount(0);
  await page.getByRole("button", { name: "撤回为草稿", exact: true }).click();
  await expect(page.getByText(/保存为私有草稿将撤销全部智能体共享/)).toBeVisible();
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect.poll(() => saved?.visibility).toBe("private");
  expect(saved).not.toHaveProperty("include_descendants");
  expect(shared).toBe(false);
});


test("MCP sharing stays with managers while members see their own access source", async ({ page }) => {
  await authenticate(page);
  let manageable = true;
  let shared = false;
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown) => route.fulfill({ json: resourceResponse(route.request().url(), body) });
    if (path === "/api/auth/me") return json(user);
    if (path === "/api/auth/me/permissions") return json({ permissions: [], personal_scope_id: "personal", root_scope_id: "org" });
    if (path === "/api/scopes/accessible") return json([scope]);
    if (path === "/api/mcps") return json([{ id: "mcp", name: "reports", display_name: "报表连接", scope_id: "org", scope_kind: "organization", manageable, usable: shared, enabled: true, metadata: { description: "受管报表查询" }, config: { secret: "must-not-render" } }]);
    if (path === "/api/sharing/mcps/mcp/access") return json({ can_use: shared, allow_download: false, sources: shared ? [{ target_scope_id: "dept", target_name: "研发部", target_kind: "department", include_descendants: true, inherited: true, allow_download: false }] : [] });
    if (path === "/api/sharing/mcps/mcp") return json({ targets: [{ id: "dept", name: "研发部", kind: "department" }], shared_scope_ids: shared ? ["dept"] : [], grants: shared ? [{ target_scope_id: "dept", include_descendants: true }] : [] });
    if (path.endsWith("/preview")) return json({ resource_id: "mcp", name: "报表连接", target_scope_id: "dept", target_name: "研发部", include_descendants: true, skills: [], mcps: [], blockers: [], confirmation: "reviewed" });
    if (path.endsWith("/batch")) { expect(route.request().postDataJSON().targets).toEqual([{ target_scope_id: "dept", include_descendants: true, confirmation: "reviewed" }]); shared = true; return json({}); }
    return json([]);
  });
  await page.goto("/workspace/mcps");
  await page.getByRole("button", { name: /报表连接.*查看详情/ }).click();
  const detail = page.getByRole("dialog", { name: "报表连接" });
  await detail.getByRole("button", { name: "共享", exact: true }).click();
  await detail.getByLabel("研发部", { exact: true }).check();
  await detail.getByLabel("研发部：同时包含下级部门", { exact: true }).check();
  await detail.getByRole("button", { name: "共享", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "确认共享" });
  expect(shared).toBe(false);
  await confirmation.getByRole("button", { name: "确认共享", exact: true }).click();
  await expect(detail.getByText(/研发部：来自上级部门的共享/)).toBeVisible();
  manageable = false;
  await page.reload();
  await page.getByRole("button", { name: /报表连接.*查看详情/ }).click();
  await expect(detail.getByText(/研发部：来自上级部门的共享/)).toBeVisible();
  await expect(detail.getByRole("button", { name: "共享", exact: true })).toHaveCount(0);
  await expect(page.getByText("must-not-render")).toHaveCount(0);
});
