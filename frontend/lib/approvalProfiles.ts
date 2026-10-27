import type { ApprovalProfile, TaskApprovalProfile } from "./types";

export const DEFAULT_APPROVAL_PROFILE: ApprovalProfile = "standard";

export const APPROVAL_PROFILES: readonly {
  value: ApprovalProfile;
  label: string;
  description: string;
}[] = [
  { value: "standard", label: "重要操作询问", description: "沙箱内安全操作自动执行，危险命令和外部影响操作询问" },
  { value: "strict", label: "修改前询问", description: "修改和外部操作均询问" },
  { value: "read_only", label: "仅查看分析", description: "只分析，不执行修改" },
  { value: "full_access", label: "自动执行", description: "在隔离沙箱中自动执行普通操作；禁止规则和必须确认的操作仍有效" },
];

export function approvalProfileLabel(profile: TaskApprovalProfile): string {
  if (profile === "unattended") return "无人值守";
  return APPROVAL_PROFILES.find((item) => item.value === profile)?.label
    ?? profile;
}

/**
 * Product-side explanation of why a tool call needed confirmation under
 * the task's frozen profile. AgentScope's RequireUserConfirmEvent does
 * not carry the exact triggering rule, so this must be phrased as a
 * common cause rather than an authoritative policy diagnosis.
 */
export function approvalRequestExplanation(
  profile: TaskApprovalProfile,
  toolNames: string[],
): string {
  const names = toolNames.map((name) => name.trim().toLowerCase());
  if (names.some((name) => ["agent_create", "agent_update", "skill_install"].includes(name))) {
    return "创建或修改智能体、安装技能必须确认当前提交内容；自动执行不会免除此确认。";
  }
  const isCommand = names.some((name) =>
    ["bash", "shell", "terminal", "execute", "execute_command", "command"]
      .includes(name),
  );
  const isExternal = names.some((name) => name.startsWith("mcp__"));
  switch (profile) {
    case "unattended": return "无人值守任务会自动拒绝需要确认的操作。";
    case "standard":
      if (isCommand) return "该命令被安全检查识别为危险或无法可靠静态分析，因此需要确认。";
      if (isExternal) {
        return "该连接器调用未被“重要操作询问”模式自动放行；外部服务访问通常需要确认。";
      }
      return "该操作具有外部影响或无法安全分类，因此需要确认。";
    case "strict":
      return "“修改前询问”模式下，修改、外部动作和未明确允许的操作都需要确认。";
    case "read_only":
      return "该操作未被“仅查看分析”模式自动放行。“仅查看分析”模式会拒绝变更；显式 ASK 规则仍可能要求确认。";
    case "full_access":
      return "该操作命中了仍需确认的显式规则；自动执行也不能覆盖 Agent 或企业拒绝。";
  }
}
