import assert from "node:assert/strict";
import { test } from "vitest";

import { adminErrorMessage } from "./errors.ts";

test("adminErrorMessage maps stable codes to Chinese messages", () => {
  assert.equal(
    adminErrorMessage({
      code: "last_root_admin",
      message: "Operation would remove the last active root administrator.",
    }),
    "该操作会移除最后一个活跃的根管理员，请先创建或恢复另一个管理员。",
  );
  assert.equal(
    adminErrorMessage({ code: "email_already_registered", message: "x" }),
    "该邮箱已被注册。",
  );
  assert.equal(
    adminErrorMessage({ code: "scope_cycle", message: "x" }),
    "不能将部门移动到自身或下级部门之下。",
  );
});

test("concurrent administration conflicts explain recovery", () => {
  assert.match(adminErrorMessage({ code: "version_conflict" }), /其他管理员/);
  assert.match(adminErrorMessage({ code: "permission_required" }), /权限/);
});

test("adminErrorMessage falls back to the raw message", () => {
  assert.equal(
    adminErrorMessage({ code: "unknown_code", message: "raw detail" }),
    "raw detail",
  );
  assert.equal(adminErrorMessage(new Error("network down")), "network down");
  assert.equal(adminErrorMessage("plain string"), "plain string");
});
