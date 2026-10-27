"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { defaultUsageDates, formatCost, formatTokens, usageCsv, type UsageOption, type UsageOptions, type UsageReport } from "@/lib/usage";

const control = "mt-1 block h-10 w-full rounded-xl border border-[var(--line)] bg-[var(--surface)] px-3 text-sm text-[var(--ink)]";
const panel = "rounded-2xl border border-[var(--line)] bg-[var(--surface)] p-5";
const groupLabels = { user: "成员", model: "模型", agent: "智能体" };
function Filter({ label, value, onChange, options, all = "全部", disabled = false }: { label: string; value: string; onChange: (value: string) => void; options: UsageOption[]; all?: string; disabled?: boolean }) {
  return <label className="text-sm text-[var(--ink-soft)]">{label}<select aria-label={label} className={`${control} !flex items-center justify-between`} value={value} onChange={event => onChange(event.target.value)} disabled={disabled}><option value="">{all}</option>{options.map(option => <option key={option.id} value={option.id}>{option.name}</option>)}</select></label>;
}
function Tokens({ value }: { value: number }) { return <span title={`${value.toLocaleString("en-US")} Token`}>{formatTokens(value)}</span>; }

export default function UsageDashboard() {
  const [dates, setDates] = useState(defaultUsageDates);
  const [scope, setScope] = useState("");
  const [user, setUser] = useState("");
  const [model, setModel] = useState("");
  const [agent, setAgent] = useState("");
  const [group, setGroup] = useState<keyof typeof groupLabels>("model");
  const [offset, setOffset] = useState(0);
  const dayCount = (Date.parse(dates.until) - Date.parse(dates.since)) / 86400000 + 1;
  const validDates = Number.isFinite(dayCount) && dayCount > 0 && dayCount <= 366;
  const base = new URLSearchParams(dates);
  if (scope) base.set("scope_id", scope);
  const query = new URLSearchParams(base);
  if (scope && user) query.set("user_id", user);
  if (model) query.set("model_profile", model);
  if (agent) query.set("agent_id", agent);
  query.set("group_by", group); query.set("limit", "50"); query.set("offset", String(offset));
  // Never reuse the previous scope's data while another scope is loading.
  const scopes = useQuery({ queryKey: ["usage-scopes"], queryFn: () => api.get<(UsageOption & { kind: string })[]>("/usage/scopes"), retry: false });
  const managedScopes = scopes.data ?? [];
  const scopeOptions = scope && !managedScopes.some(item => item.id === scope)
    ? [...managedScopes, { id: scope, name: "当前范围（不可用）", kind: "unknown" }] : managedScopes;
  const canChooseScope = managedScopes.length > 0 || Boolean(scope);
  const scopeReady = !scope || (scopes.isSuccess && !scopes.isFetching && managedScopes.some(item => item.id === scope));
  const options = useQuery({ queryKey: ["usage-options", base.toString()], queryFn: () => api.get<UsageOptions>(`/usage/options?${base}`), enabled: validDates && scopeReady, retry: false });
  const report = useQuery({ queryKey: ["usage", query.toString()], queryFn: () => api.get<UsageReport>(`/usage?${query}`), enabled: validDates && scopeReady, retry: false });
  const data = validDates && scopeReady && !report.isFetching && !report.isError ? report.data : undefined;
  const visibleOptions = scopeReady && !options.isFetching && !options.isError ? options.data : undefined;
  function refreshUsage() { void Promise.all([report.refetch(), options.refetch(), scopes.refetch()]); }
  function changeBase(update: () => void) { update(); setUser(""); setModel(""); setAgent(""); setOffset(0); }
  function exportPage() {
    if (!data) return;
    const url = URL.createObjectURL(new Blob([usageCsv(data.groups, groupLabels[group])], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `usage-${dates.since}-${dates.until}-${group}-page-${offset / 50 + 1}.csv`; anchor.click(); URL.revokeObjectURL(url);
  }
  return <main className="h-full overflow-y-auto bg-[var(--canvas)] p-4 pb-24 text-[var(--ink)] sm:p-8 sm:pb-24 lg:pb-8"><div className="mx-auto max-w-7xl space-y-6">
    <header><div className="flex flex-wrap items-center justify-between gap-3"><div><h1 className="text-2xl font-semibold">{canChooseScope ? "用量与成本" : "我的用量"}</h1><p className="mt-2 text-sm text-[var(--ink-soft)]">{scope ? managedScopes.find(item => item.id === scope)?.name ?? "所选范围" : "个人用量"} · 费用仅为估算</p></div><Button variant="secondary" size="sm" onClick={refreshUsage} disabled={!validDates || report.isFetching || options.isFetching || scopes.isFetching}>刷新</Button></div></header>
    <section aria-label="用量筛选" className={panel}><div className={`grid gap-4 sm:grid-cols-2 ${scope ? "xl:grid-cols-3" : canChooseScope ? "xl:grid-cols-5" : "xl:grid-cols-4"}`}>
      {canChooseScope && <Filter label="统计范围" value={scope} onChange={value => { changeBase(() => setScope(value)); setGroup(value ? "user" : "model"); }} options={scopeOptions} all="我的用量" disabled={scopes.isFetching} />}
      <label className="text-sm text-[var(--ink-soft)]">开始日期<input aria-label="开始日期" type="date" className={control} value={dates.since} onChange={event => changeBase(() => setDates({ ...dates, since: event.target.value }))} /></label>
      <label className="text-sm text-[var(--ink-soft)]">结束日期<input aria-label="结束日期" type="date" className={control} value={dates.until} onChange={event => changeBase(() => setDates({ ...dates, until: event.target.value }))} /></label>
      {scope && <Filter label="成员" value={user} onChange={value => { setUser(value); setOffset(0); }} options={visibleOptions?.users ?? []} disabled={!visibleOptions} />}
      <Filter label="模型" value={model} onChange={value => { setModel(value); setOffset(0); }} options={visibleOptions?.models ?? []} disabled={!visibleOptions} />
      <Filter label="智能体" value={agent} onChange={value => { setAgent(value); setOffset(0); }} options={visibleOptions?.agents ?? []} disabled={!visibleOptions} />
    </div>{!validDates && <p role="alert" className="mt-3 text-sm text-red-600">请选择有效日期，最多查询 366 天。</p>}
    {scopes.isError && <p role="alert" className="mt-3 text-sm">范围加载失败。<Button variant="ghost" size="sm" onClick={() => void scopes.refetch()}>重试范围</Button></p>}
    {options.isError && <p role="alert" className="mt-3 text-sm">筛选选项加载失败。<Button variant="ghost" size="sm" onClick={() => void options.refetch()}>重试筛选</Button></p>}</section>
    {scope && !scopes.isFetching && !scopeReady && <div role="alert" className={panel}>所选范围暂不可用。<Button variant="ghost" size="sm" onClick={() => { changeBase(() => setScope("")); setGroup("model"); }}>查看我的用量</Button></div>}
    {validDates && (report.isFetching || (scope && scopes.isFetching)) && <p role="status" className={panel}>正在加载用量…</p>}
    {validDates && report.isError && <div role="alert" className={panel}><p>用量加载失败</p><Button className="mt-3" variant="secondary" onClick={() => void report.refetch()}>重试加载用量</Button></div>}
    {data && <>
      <section aria-label="用量总览" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="任务数" value={data.summary.task_count.toLocaleString()} detail={`${data.summary.recorded_tasks} 项记录完整`} />
        <Metric label="输入 Token" value={<Tokens value={data.summary.input_tokens} />} detail="1 K = 1,000 Token" />
        <Metric label="输出 Token" value={<Tokens value={data.summary.output_tokens} />} detail="1 K = 1,000 Token" />
        <Metric label="估算成本" value={formatCost(data.summary)} detail={data.summary.unpriced_tasks ? `${data.summary.unpriced_tasks} 项未计价${data.summary.costs.length ? " · 仅含已计价部分" : ""}` : "按币种统计"} />
      </section>
      {data.summary.task_count > data.summary.recorded_tasks && <p className="text-sm text-[var(--ink-soft)]">{data.summary.task_count - data.summary.recorded_tasks} 项用量不完整，合计仅含已记录部分。</p>}
      {!data.summary.task_count ? <p className={panel}>暂无用量</p> : <DailyUsage daily={data.daily} />}
      <section className={panel}><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">用量明细</h2><div className="flex flex-wrap items-center gap-3"><label className="text-sm">分组方式<select aria-label="分组方式" className={`${control} !mt-0 !inline-flex items-center justify-between !w-auto ml-2`} value={group} onChange={event => { setGroup(event.target.value as keyof typeof groupLabels); setOffset(0); }}>{Object.entries(groupLabels).filter(([key]) => scope || key !== "user").map(([key, label]) => <option key={key} value={key}>按{label}</option>)}</select></label><Button variant="secondary" size="sm" onClick={exportPage} disabled={!data.groups.length}>导出本页</Button></div></div>
        <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[700px] text-left text-sm"><thead className="border-b border-[var(--line)] text-[var(--ink-soft)]"><tr>{[groupLabels[group], "任务数", "输入 Token", "输出 Token", "估算成本", "未计价"].map(label => <th key={label} className="px-3 py-3 font-medium">{label}</th>)}</tr></thead><tbody>{data.groups.map(row => <tr key={row.id} className="border-b border-[var(--line)]"><td className="max-w-64 break-words px-3 py-4">{row.name}</td><td className="px-3 py-4">{row.task_count}</td><td className="px-3 py-4"><Tokens value={row.input_tokens} /></td><td className="px-3 py-4"><Tokens value={row.output_tokens} /></td><td className="px-3 py-4">{formatCost(row)}{row.costs.length > 0 && row.unpriced_tasks > 0 && <span className="block text-xs text-[var(--ink-muted)]">仅含已计价部分</span>}</td><td className="px-3 py-4">{row.unpriced_tasks}</td></tr>)}</tbody></table></div>
        {!data.groups.length && <p className="py-5 text-sm text-[var(--ink-muted)]">没有匹配的明细</p>}
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm"><span>共 {data.total_groups} 项 · 当前第 {Math.floor(offset / 50) + 1} 页</span><div className="flex gap-2"><Button variant="secondary" size="sm" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button><Button variant="secondary" size="sm" disabled={offset + 50 >= data.total_groups} onClick={() => setOffset(offset + 50)}>下一页</Button></div></div>
      </section>
    </>}
    <details className="text-xs leading-6 text-[var(--ink-muted)]"><summary className="w-fit cursor-pointer">统计说明</summary><div className="mt-2 space-y-1">
      <p>按任务创建日（UTC）统计，包含失败、取消任务的已记录用量。缺失用量不代表零消耗。</p>
      <p>费用按模型单价估算，未配置价格或用量不完整的任务不计价；多币种分别统计，不等同于账单。</p>
      {scope && <p>组织与部门仅统计已记录归属的任务；部门数据可能重叠，不可直接相加。</p>}
      <p>已删除会话不计入统计。导出仅包含当前页。</p>
    </div></details>
  </div></main>;
}
function Metric({ label, value, detail }: { label: string; value: React.ReactNode; detail: string }) {
  return <div className={panel}><h2 className="text-sm text-[var(--ink-soft)]">{label}</h2><div className="mt-3 break-words text-2xl font-semibold tabular-nums">{value}</div><p className="mt-2 text-xs leading-5 text-[var(--ink-muted)]">{detail}</p></div>;
}
function DailyUsage({ daily }: { daily: UsageReport["daily"] }) {
  const max = Math.max(1, ...daily.map(day => day.input_tokens + day.output_tokens));
  return <section className={panel}><h2 className="font-semibold">每日用量</h2><p className="mt-1 text-xs text-[var(--ink-muted)]">输入与输出 Token 合计</p><div className="mt-5 max-h-80 space-y-3 overflow-y-auto" aria-label="每日 Token 用量">{daily.map(day => <div key={day.date} className="flex items-center gap-3 text-xs sm:text-sm"><span className="w-24 shrink-0">{day.date}</span><div className="h-3 flex-1 rounded bg-[var(--surface-subtle)]"><div className="h-full rounded bg-[var(--accent)]" style={{ width: `${(day.input_tokens + day.output_tokens) / max * 100}%` }} /></div><span className="w-24 shrink-0 text-right tabular-nums"><Tokens value={day.input_tokens + day.output_tokens} /></span></div>)}</div></section>;
}
