import type { AgentDeniedToolGroup } from "./types";

export const AGENT_DENIED_TOOL_GROUPS: readonly {
  value: AgentDeniedToolGroup;
  label: string;
  description: string;
}[] = [
  { value: "shell", label: "命令执行", description: "禁止 Bash、PowerShell 和本地程序执行" },
  { value: "workspace_write", label: "工作区写入", description: "禁止 Write、Edit 等文件修改工具" },
  { value: "external_connectors", label: "外部连接器", description: "禁止所有 MCP 外部服务调用" },
  { value: "artifact_delivery", label: "成果交付", description: "禁止将工作区文件发布为正式成果" },
  { value: "agent_management", label: "Agent 管理", description: "禁止查询、创建和编辑 Agent" },
  { value: "skill_management", label: "Skill 管理", description: "禁止校验和安装 Skill" },
  { value: "web_access", label: "Web 访问", description: "禁止网页搜索与读取" },
];
