import { afterEach, expect, test, vi } from "vitest";
import { api, type ApiError } from "./api";

afterEach(() => { vi.unstubAllGlobals(); });

test("邮箱校验错误转中文字段提示，不保留服务端input和ctx", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 422, statusText: "Unprocessable Entity", json: async () => ({ detail: [{ type: "value_error", loc: ["body", "email"], msg: "Value error for private@example.test", input: "private@example.test", ctx: { reason: "private-context" } }] }) }));
  const error = await api.post("/auth/register", { email: "private@example.test" }).catch((reason: ApiError) => reason) as ApiError;
  expect(error.code).toBe("validation_error");
  expect(error.status).toBe(422);
  expect(error.message).toContain("邮箱");
  expect(error.detail).toEqual({ fields: [{ field: "email", message: "邮箱格式不正确，请检查后重试。" }] });
  expect(`${error.message}${JSON.stringify(error.detail)}`).not.toMatch(/private@example|private-context|input|ctx/);
});

test("密码校验错误不泄露input、msg或ctx中的密码", async () => {
  const password = "sensitive-secret-password";
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 422, statusText: "Unprocessable Entity", json: async () => ({ detail: [{ type: "string_too_short", loc: ["body", "initial_password"], msg: `Rejected ${password}`, input: password, ctx: { min_length: 8, password } }] }) }));
  const error = await api.post("/admin/users", { initial_password: password }).catch((reason: ApiError) => reason) as ApiError;
  expect(error.code).toBe("validation_error");
  expect(error.message).toContain("初始密码长度不符合要求");
  expect(error.detail).toEqual({ fields: [{ field: "initial_password", message: "初始密码长度不符合要求，请检查后重试。" }] });
  expect(`${error.message}${JSON.stringify(error.detail)}`).not.toContain(password);
  expect(error.detail).not.toHaveProperty("input");
  expect(error.detail).not.toHaveProperty("ctx");
});

test("first-time registration sends organization name while ordinary registration omits it", async () => {
 const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ access_token: "test", user: { id: "u" }, permissions: [] }) });
 vi.stubGlobal("fetch", fetchMock);
 await api.register("admin@example.com", "password123", "管理员", "示例企业");
 expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ email: "admin@example.com", password: "password123", display_name: "管理员", organization_name: "示例企业" });
 fetchMock.mockClear();
 await api.register("member@example.com", "password123", "成员");
 expect(JSON.parse(fetchMock.mock.calls[0][1].body)).not.toHaveProperty("organization_name");
});

test("organization rename sends the displayed tree version", async () => {
 const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: "org", name: "新企业", tree_version: 4 }) });
 vi.stubGlobal("fetch", fetchMock);
 await api.renameOrganization("新企业", 3);
 expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/admin/organization"), expect.objectContaining({ method: "PATCH", body: JSON.stringify({ name: "新企业", expected_tree_version: 3 }) }));
});
