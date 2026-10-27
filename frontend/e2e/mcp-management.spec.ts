import { expect, test, type Page } from "@playwright/test";
import { resourceResponse } from "./resource-response";

async function setup(page: Page, manageable = true) {
  const user = { id: "u", email: "test@example.com", display_name: "测试", status: "active", must_change_password: false };
  const connection = { id: "mcp", name: "reports", display_name: "企业报表", scope_id: "org", scope_kind: "organization", enabled: true, manageable, usable: true, metadata: { description: "报表查询" } };
  const calls: { created?: Record<string, unknown>; patched?: Record<string, unknown>; probes: string[]; configurationReads: number } = { probes: [], configurationReads: 0 };
  await page.addInitScript((u) => {
    localStorage.setItem("workmate_token", "test"); localStorage.setItem("workmate_user", JSON.stringify(u));
  }, user);
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown) => route.fulfill({ json: resourceResponse(route.request().url(), body) });
    if (path === "/api/auth/me") return json(user);
    if (path === "/api/auth/me/permissions") return json({ permissions: manageable ? ["mcp.manage"] : [], root_scope_id: "org", personal_scope_id: "personal" });
    if (path === "/api/scopes/accessible") return json([{ id: "org", name: "企业", kind: "organization", permissions: manageable ? ["mcp.manage"] : [] }]);
    if (path === "/api/mcps") return json([connection]);
    if (path === "/api/mcps/custom") { calls.created = route.request().postDataJSON(); return json(connection); }
    if (path === "/api/mcps/mcp/custom") { calls.patched = route.request().postDataJSON(); return json(connection); }
    if (path.endsWith("/configuration")) {
      calls.configurationReads += 1;
      return json({ source: "custom", name: "reports", display_name: "企业报表", description: "报表查询", url: "https://example.com/mcp", timeout: 30, is_stateful: false, header_names: ["Authorization"], configured_fields: [] });
    }
    if (path.endsWith("/probe")) { calls.probes.push(path); return json({ ok: true, tools: [{ name: "search_reports" }] }); }
    if (path.endsWith("/access")) return json({ can_use: true, sources: [] });
    return json([]);
  });
  return calls;
}

test("organization manager creates and tests HTTP MCP then edits without credential disclosure", async ({ page }) => {
  const calls = await setup(page); await page.goto("/workspace/mcps");
  await page.getByRole("button", { name: "添加 MCP" }).click();
  await page.getByRole("button", { name: /自定义连接/ }).click();

  await page.getByLabel("服务地址").fill("https://example.com/mcp");
  await page.getByLabel("认证方式").selectOption("bearer");
  await page.getByLabel("Bearer Token", { exact: true }).fill("test-secret");
  await expect(page.getByLabel("Bearer Token", { exact: true })).toHaveAttribute("type", "password");
  await page.getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(page.getByText("连接成功，发现 1 个工具。")).toBeVisible();
  await expect(page.getByText("search_reports", { exact: true })).toBeVisible();
  await page.getByLabel("连接名称", { exact: true }).fill("reports");
  await page.getByRole("button", { name: "保存连接" }).click();
  await expect.poll(() => calls.created).toMatchObject({ scope_id: "org", name: "reports", headers: { Authorization: "Bearer test-secret" } });
  await expect(page.getByText(/连接已保存/)).toBeVisible();
  await page.getByRole("button", { name: "编辑配置与测试连接" }).click();
  await expect(page.getByText(/已配置请求头：Authorization/)).toBeVisible();
  await expect(page.locator('input[value="Bearer test-secret"]')).toHaveCount(0);
  await page.getByRole("button", { name: "测试已保存配置" }).click();
  await expect(page.getByText("连接成功，发现 1 个工具。")).toBeVisible();
  await page.getByText("高级选项", { exact: true }).click();
  await page.getByLabel("显示名称", { exact: true }).fill("新报表");
  await page.getByRole("button", { name: "保存连接" }).click();
  await expect.poll(() => calls.patched?.display_name).toBe("新报表");
  expect(calls.patched).not.toHaveProperty("headers");
  expect(calls.probes).toEqual(["/api/mcps/probe", "/api/mcps/mcp/probe"]);
});

test("ordinary member sees shared connection without create, edit, or credential access", async ({ page }) => {
  const calls = await setup(page, false); await page.goto("/workspace/mcps");
  await page.getByRole("button", { name: /企业报表/ }).click();
  await expect(page.getByText(/你没有连接管理权限/)).toBeVisible();
  await expect(page.getByRole("button", { name: "编辑配置与测试连接" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "添加 MCP" })).toHaveCount(0);
  expect(calls.configurationReads).toBe(0);
});
