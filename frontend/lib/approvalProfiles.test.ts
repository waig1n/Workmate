import assert from "node:assert/strict";
import { test } from "vitest";

import {
  APPROVAL_PROFILES,
  approvalProfileLabel,
  DEFAULT_APPROVAL_PROFILE,
} from "./approvalProfiles.ts";
import type { ApprovalProfile } from "./types.ts";

test("standard is risk-based and full access remains a governed option", () => {
  assert.equal(DEFAULT_APPROVAL_PROFILE, "standard");
  assert.deepEqual(
    APPROVAL_PROFILES.map((profile) => profile.value),
    ["standard", "strict", "read_only", "full_access"],
  );
  assert.equal(approvalProfileLabel("strict"), "修改前询问");
  assert.equal(approvalProfileLabel("full_access"), "自动执行");
  assert.ok(!APPROVAL_PROFILES.some((profile) => ["bypass", "dont_ask"].includes(profile.value)));
});

test("unknown profile values fall back to the raw value", () => {
  assert.equal(
    approvalProfileLabel("legacy_value" as ApprovalProfile),
    "legacy_value",
  );
});
