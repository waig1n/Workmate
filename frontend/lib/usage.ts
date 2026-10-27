export type UsageSummary = { task_count: number; recorded_tasks: number; input_tokens: number; output_tokens: number; unpriced_tasks: number; costs: { currency: string; amount: string }[] };
export type UsageGroup = UsageSummary & { id: string; name: string };
export type UsageReport = { since: string; until: string; summary: UsageSummary; daily: (UsageSummary & { date: string })[]; groups: UsageGroup[]; total_groups: number; limit: number; offset: number };
export type UsageOption = { id: string; name: string };
export type UsageOptions = { users: UsageOption[]; models: UsageOption[]; agents: UsageOption[] };
export function formatTokens(value: number): string {
  return `${(value / 1000).toLocaleString("en-US", { maximumFractionDigits: 3 })} K`;
}
export function formatCost(summary: UsageSummary): string {
  return summary.costs.length ? summary.costs.map(({ currency, amount }) => `${currency} ${amount}`).join(" · ") : summary.task_count ? "未计价" : "—";
}
function csvCell(value: string | number) {
  const text = String(value);
  return `"${(/^[\s]*[=+@-]/.test(text) ? `'${text}` : text).replaceAll('"', '""')}"`;
}
export function usageCsv(groups: UsageGroup[], label: string): string {
  const rows = [[label, "任务数", "已记录用量任务", "输入 Token", "输出 Token", "估算成本（已计价部分）", "未计价任务"], ...groups.map(row => [row.name, row.task_count, row.recorded_tasks, row.input_tokens, row.output_tokens, formatCost(row), row.unpriced_tasks])];
  return `\uFEFF${rows.map(row => row.map(csvCell).join(",")).join("\r\n")}`;
}
export function defaultUsageDates() {
  const until = new Date();
  const since = new Date(until); since.setUTCDate(since.getUTCDate() - 29);
  return { since: since.toISOString().slice(0, 10), until: until.toISOString().slice(0, 10) };
}
