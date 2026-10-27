import { expect, test } from "@playwright/test";
import { resourceResponse } from "./resource-response";

test("first administrator names the workspace and can rename it in organization settings", async ({ page }) => {
  let initialized = false;
  let name = "";
  let version = 1;
  let registrations = 0;
  let scopeReads = 0;
  let renameAttempts = 0;
  const user = { id: "admin", email: "admin@example.com", display_name: "张三", status: "active", must_change_password: false };
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown) => route.fulfill({ json: resourceResponse(route.request().url(), body) });
    if (path === "/api/auth/registration-status") return json({ registration_available: !initialized, bootstrap_required: !initialized });
    if (path === "/api/auth/register") {
      const body = route.request().postDataJSON();
      expect(body).toEqual({ email: "admin@example.com", password: "password123", display_name: "张三", organization_name: "示例企业" });
      name = body.organization_name; initialized = true; registrations += 1;
      return json({ access_token: "admin-token", user });
    }
    if (path === "/api/auth/me") return json(user);
    if (path === "/api/auth/me/permissions") return json({ permissions: [], root_scope_id: "org", personal_scope_id: "personal", is_root_admin: true });
    if (path === "/api/scopes/accessible") { scopeReads += 1; return json([{ id: "org", kind: "organization", name, parent_id: null, permissions: ["scope.manage"] }]); }
    if (path === "/api/admin/organization") {
      if (route.request().method() === "PATCH") {
        renameAttempts += 1;
        if (renameAttempts === 1) { name = "另一位管理员的名称"; version = 2; return route.fulfill({ status: 409, json: { detail: { code: "version_conflict", message: "stale tree version" } } }); }
        expect(route.request().postDataJSON()).toEqual({ name: "更名后的企业", expected_tree_version: 2 }); name = "更名后的企业"; version += 1;
      }
      return json({ id: "org", name, tree_version: version });
    }
    return json([]);
  });
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "创建企业工作空间" })).toBeVisible();
  await page.getByLabel("组织名称", { exact: true }).fill("示例企业");
  await page.getByLabel("管理员姓名", { exact: true }).fill("张三");
  await page.getByLabel("邮箱", { exact: true }).fill("admin@example.com");
  await page.getByLabel("密码", { exact: true }).fill("password123");
  await page.getByRole("button", { name: "创建工作空间", exact: true }).click();
  await expect(page).toHaveURL(/\/workspace$/);
  expect(registrations).toBe(1);
  await page.goto("/workspace/settings");
  await expect(page.getByLabel("组织名称", { exact: true })).toHaveValue("示例企业");
  await page.getByLabel("组织名称", { exact: true }).fill("更名后的企业");
  const readsBeforeSave = scopeReads;
  await page.getByRole("button", { name: "保存组织名称", exact: true }).click();
  await expect(page.getByText("其他管理员已修改此对象。请重新加载最新内容，对照差异后再保存。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "刷新最新版本", exact: true }).click();
  await expect(page.getByText("当前组织名称：另一位管理员的名称，草稿已保留，请核对后保存。", { exact: true })).toBeVisible();
  await expect(page.getByLabel("组织名称", { exact: true })).toHaveValue("更名后的企业");
  expect(renameAttempts).toBe(1);
  await page.getByRole("button", { name: "保存组织名称", exact: true }).click();
  await expect(page.getByText("组织名称已更新", { exact: true })).toBeVisible();
  expect(name).toBe("更名后的企业");
  await expect.poll(() => scopeReads).toBeGreaterThan(readsBeforeSave);
});

test("open registration joins the existing workspace without asking for an organization name", async ({ page }) => {
  await page.route("**/api/auth/registration-status", route => route.fulfill({ json: { registration_available: true, bootstrap_required: false } }));
  await page.goto("/login");
  await page.getByRole("button", { name: "没有账号？注册", exact: true }).click();
  await expect(page.getByRole("heading", { name: "创建企业账号" })).toBeVisible();
  await expect(page.getByLabel("组织名称", { exact: true })).toHaveCount(0);
});
