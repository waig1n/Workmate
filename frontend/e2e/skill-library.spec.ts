import { resourceResponse } from "./resource-response";
import { expect, test } from "@playwright/test";

test("upload enters library once, then explicit sharing and revocation control use", async ({ page }) => {
  const user = { id: "u", email: "admin@example.com", display_name: "管理员", status: "active", must_change_password: false };
  const skill = { id: "uploaded-skill", name: "reports", display_name: "上传报告", scope_id: "org", scope_kind: "organization", manageable: true, usable: false, enabled: true, metadata: { hub_id: "uploaded", creation_channel: "upload", description: "整理报告" } };
  const builtin = { ...skill, id: "builtin", name: "builtin", display_name: "内置工作方法", metadata: { hub_id: "workmate-builtin" } };
  const imported = { ...skill, id: "third-party", name: "meeting-notes", display_name: "第三方纪要", metadata: { hub_id: "skillhub-cn" } };
  const targets = [{ id: "org", name: "全组织可用", kind: "organization" }, { id: "dept", name: "研发部", kind: "department" }];
  const calls = { uploads: 0, installs: 0, previews: [] as string[], grants: [] as string[], revokes: [] as string[], catalogs: [] as string[] };
  await page.addInitScript((account) => {
    localStorage.setItem("workmate_token", "test");
    localStorage.setItem("workmate_user", JSON.stringify(account));
  }, user);
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    const json = (body: unknown) => route.fulfill({ json: resourceResponse(route.request().url(), body) });
    if (path === "/api/auth/me") return json(user);
    if (path === "/api/auth/me/permissions") return json({ permissions: ["skill.manage"], personal_scope_id: "personal", root_scope_id: "org", is_root_admin: true });
    if (path === "/api/scopes/accessible") return json([{ id: "org", name: "总部", kind: "organization", parent_id: null, permissions: ["skill.manage"] }]);
    if (path === "/api/skills/upload") {
      expect(method).toBe("POST");
      expect(route.request().postDataBuffer()?.toString()).not.toContain('name="scope_id"');
      calls.uploads += 1;
      return json(skill);
    }
    if (path === "/api/skills") {
      if (method === "POST") {
        expect(route.request().postDataJSON()).toEqual({ hub_id: "skillhub-cn", card_id: "meeting-notes", confirmed: true });
        calls.installs += 1;
        return json(imported);
      }
      return json([builtin, ...(calls.uploads ? [skill] : []), ...(calls.installs ? [imported] : [])]);
    }
    if (path === "/api/skills/hubs") return json([{ hub_id: "uploaded", display_name: "Uploaded Hub" }, { hub_id: "workmate-builtin", display_name: "平台内置" }, { hub_id: "skillhub-cn", display_name: "第三方来源" }]);
    if (path === "/api/skills/catalog") {
      calls.catalogs.push(url.searchParams.get("hub_id") ?? "");
      return json({ cards: [{ id: "meeting-notes", name: "meeting-notes", display_name: "第三方纪要", hub_id: "skillhub-cn", description: "生成纪要", installed: calls.installs > 0 }] });
    }
    if (path === "/api/sharing/skills/uploaded-skill") return json({ targets, shared_scope_ids: calls.grants, grants: calls.grants.map((id) => ({ target_scope_id: id, include_descendants: id === "org" })) });
    if (path === "/api/sharing/skills/uploaded-skill/preview") {
      const { target_scope_id: id } = route.request().postDataJSON();
      calls.previews.push(id);
      return json({ resource_id: skill.id, name: skill.display_name, target_scope_id: id, target_name: targets.find((target) => target.id === id)?.name, skills: [], blockers: [], confirmation: `confirmed-${id}` });
    }
    if (path === "/api/sharing/skills/uploaded-skill/batch") {
      const body = route.request().postDataJSON();
      expect(body.targets).toEqual([{ target_scope_id: "org", include_descendants: true, confirmation: "confirmed-org" }, { target_scope_id: "dept", include_descendants: false, confirmation: "confirmed-dept" }]);
      calls.grants = body.targets.map((target: { target_scope_id: string }) => target.target_scope_id);
      return json({});
    }
    if (path.startsWith("/api/sharing/skills/uploaded-skill/") && method === "DELETE") {
      const id = path.split("/").at(-1)!;
      calls.revokes.push(id);
      calls.grants = calls.grants.filter((grant) => grant !== id);
      return route.fulfill({ status: 204 });
    }
    return json([]);
  });
  await page.goto("/workspace/skills");
  await expect(page.getByRole("button", { name: /内置工作方法.*平台内置/ })).toBeVisible();
  await expect(page.getByLabel("筛选技能来源").getByRole("option", { name: "自建技能" })).toHaveCount(1);
  await page.getByRole("button", { name: "添加技能" }).click();
  await expect(page.getByLabel("管理归属", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("筛选管理归属", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("option", { name: "Uploaded Hub" })).toHaveCount(0);
  await page.getByLabel("上传 ZIP").setInputFiles({ name: "reports.zip", mimeType: "application/zip", buffer: Buffer.from("mocked skill archive") });
  const detail = page.getByRole("dialog", { name: "上传报告", exact: true });
  await expect(detail).toBeVisible();
  await expect(page.getByRole("dialog", { name: "添加技能", exact: true })).toHaveCount(0);
  expect(calls.uploads).toBe(1);
  expect(calls.installs).toBe(0);
  expect(calls.catalogs).not.toContain("uploaded");
  expect(calls.catalogs).not.toContain("workmate-builtin");
  expect(calls.grants).toEqual([]);
  await detail.getByRole("button", { name: "共享", exact: true }).click();
  await expect(detail.getByText(/包括未来新增部门/)).toBeVisible();
  await detail.getByLabel("全组织可用", { exact: true }).check();
  await detail.getByLabel("研发部", { exact: true }).check();
  await detail.getByRole("button", { name: "共享", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "确认共享", exact: true });
  await expect(confirmation.getByText("共享给：全组织可用")).toBeVisible();
  await expect(confirmation.getByText("共享给：研发部")).toBeVisible();
  expect(calls.grants).toEqual([]);
  await confirmation.getByRole("button", { name: "确认共享", exact: true }).click();
  await expect(confirmation).toHaveCount(0);
  await expect.poll(() => calls.grants).toEqual(["org", "dept"]);
  await detail.getByRole("group", { name: "已有授权：全组织可用", exact: true }).getByRole("button", { name: "取消共享", exact: true }).click();
  expect(calls.revokes).toEqual([]);
  await detail.getByRole("button", { name: "确认取消共享", exact: true }).click();
  await expect.poll(() => calls.revokes).toEqual(["org"]);
  expect(calls.grants).toEqual(["dept"]);
  await expect(detail.getByLabel("全组织可用", { exact: true })).not.toBeChecked();
  expect(calls.installs).toBe(0);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "添加技能", exact: true }).click();
  await page.getByRole("button", { name: "查看并入库", exact: true }).click();
  await page.getByRole("button", { name: "确认入库", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "第三方纪要", exact: true })).toBeVisible();
  expect(calls.installs).toBe(1);
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("筛选技能来源")).toHaveValue("third-party");
  await expect(page.getByRole("button", { name: /第三方纪要/ })).toBeVisible();
  await page.getByRole("button", { name: "添加技能", exact: true }).click();
  await expect(page.getByRole("button", { name: "已入库", exact: true })).toBeDisabled();
  expect(calls.installs).toBe(1);
});
