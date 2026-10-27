import type { Agent } from "./types";
export const bindingIssueLabel = (reason: NonNullable<Agent["binding_issues"]>[number]["reason"]) => ({
  missing: "资源已移除",
  disabled: "资源已停用",
  unshared: "需向智能体所属范围开放使用",
  unready: "资源配置尚未就绪",
  service_disabled: "知识库服务未启用",
})[reason];
