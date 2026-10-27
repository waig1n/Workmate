import type { ChatToolMsg } from "./model";

/**
 * Tool-call categories for the aggregated run summary line. The main
 * conversation stream only shows counts per category; full call details
 * live in the Activity side panel.
 */

export type ToolCategory = "read" | "command" | "write" | "skill" | "mcp" | "other";

const COMMAND_TOOLS = new Set([
  "bash", "shell", "terminal", "execute", "execute_command",
  "run_command", "command",
]);

const READ_TOOLS = new Set([
  "read", "read_file", "readfile", "view", "view_file", "glob", "grep",
  "search", "search_files", "list_files", "list_directory", "ls", "find",
  "cat", "head", "tail",
]);

const WRITE_TOOLS = new Set([
  "write", "write_file", "writefile", "edit", "edit_file", "apply_patch",
  "create_file", "delete", "delete_file", "move", "move_file", "rename",
]);

export function classifyTool(name: string): ToolCategory {
  const normalized = name.trim().toLowerCase();
  if (!normalized) return "other";
  if (normalized.startsWith("mcp__")) return "mcp";
  if (COMMAND_TOOLS.has(normalized)) return "command";
  if (READ_TOOLS.has(normalized)) return "read";
  if (WRITE_TOOLS.has(normalized)) return "write";
  if (normalized.startsWith("skill") || normalized.includes("skill")) {
    return "skill";
  }
  return "other";
}

const CATEGORY_LABELS: Record<ToolCategory, (count: number) => string> = {
  read: (n) => `阅读 ${n} 个文件`,
  command: (n) => `执行 ${n} 个命令`,
  write: (n) => `修改 ${n} 个文件`,
  skill: (n) => `调用 ${n} 个 Skill`,
  mcp: (n) => `调用 ${n} 个连接器`,
  other: (n) => `${n} 个其他操作`,
};

/** e.g. ["阅读 4 个文件", "执行 2 个命令"] — nonzero categories only. */
export function summarizeTools(tools: ChatToolMsg[]): string[] {
  const counts = new Map<ToolCategory, number>();
  for (const item of tools) {
    const category = classifyTool(item.call.toolCallName);
    counts.set(category, (counts.get(category) ?? 0) + 1);
  }
  const order: ToolCategory[] = ["read", "command", "write", "skill", "mcp", "other"];
  return order
    .filter((category) => counts.has(category))
    .map((category) => CATEGORY_LABELS[category](counts.get(category)!));
}

/** Failed calls stay visible as single lines below the summary. */
export function failedTools(tools: ChatToolMsg[]): ChatToolMsg[] {
  return tools.filter((item) => item.call.state === "error");
}

/** Short human snippet of a failed call's output. */
export function failureSummary(item: ChatToolMsg): string {
  const raw = item.call.result?.trim();
  if (!raw) return "执行失败";
  const single = raw.split("\n").find((line) => line.trim());
  const line = single ? single.trim() : raw;
  return line.length > 160 ? `${line.slice(0, 160)}…` : line;
}
