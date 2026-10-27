"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Icon } from "@/components/ui/Icons";
import { Dialog } from "@/components/ui/Dialog";
import { api } from "@/lib/api";

export type SharePlan = {
  resource_id: string;
  name: string;
  target_scope_id: string;
  target_name: string;
  target_kind?: "organization" | "department";
  mcps?: { id: string; name: string; scope_id: string }[];
  skills: { id: string; name: string; scope_id: string }[];
  knowledge_bases?: { id: string; name: string; scope_id: string }[];
  include_descendants?: boolean;
  allow_download?: boolean;
  blockers: string[];
  confirmation: string;
};

type Sharing = { targets: { id: string; name: string; kind?: "organization" | "department"; parent_id?: string | null }[]; shared_scope_ids: string[]; grants?: { target_scope_id: string; include_descendants: boolean; allow_download?: boolean }[] };

export function ShareSummary({ plans }: { plans: SharePlan[] }) {
  return <div className="space-y-3 text-sm">
    {plans.map((plan) => <div key={plan.target_scope_id}>
      <p className="font-medium">共享给：{plan.target_name}</p>
      <p className="text-xs text-neutral-500">{plan.target_kind === "organization" ? plan.include_descendants ? "全组织（含全部层级及未来新增部门）" : "组织直属成员（历史授权，不扩大范围）" : plan.include_descendants ? "包含本部门及全部下级部门（含未来新增）" : "仅本部门"}</p>
      {plan.skills.length > 0 && <p>同时共享技能：{plan.skills.map((skill) => skill.name).join("、")}</p>}
      {Boolean(plan.mcps?.length) && <p>同时共享 MCP 连接：{plan.mcps?.map((mcp) => mcp.name).join("、")}</p>}
      {Boolean(plan.knowledge_bases?.length) && <p>同时共享知识库：{plan.knowledge_bases?.map((library) => library.name).join("、")}</p>}
      {plan.blockers.map((blocker) => <p key={blocker} className="text-red-600">{blocker}</p>)}
    </div>)}
    {plans.some((plan) => Boolean(plan.mcps?.length)) && <p className="text-xs text-neutral-500">复用来源维护的受管连接，不复制或转移凭据，也不授予连接管理权限。</p>}
    {plans.some((plan) => Boolean(plan.knowledge_bases?.length)) && <p className="text-xs text-neutral-500">关联知识库只补充检索授权，不自动授予原文下载权限。</p>}
    <p className="text-xs leading-5 text-neutral-500">资源仍由来源管理者维护，更新后对后续运行生效。接收部门有权编辑智能体的成员也能将共享技能和 MCP 连接绑定到自己的智能体；不能修改源资源或再次转授。</p>
  </div>;
}

export function ResourceSharing({ kind, resourceId, disabled = false, publishOnShare = false, initiallyOpen = false }: { kind: "agents" | "skills" | "mcps" | "knowledge-bases"; resourceId: string; disabled?: boolean; publishOnShare?: boolean; initiallyOpen?: boolean }) {
  const [allowDownload, setAllowDownload] = useState(false);
  const [downloadDefault, setDownloadDefault] = useState<boolean | undefined>();
  const [downloads, setDownloads] = useState<Record<string, boolean>>({});
  const [open, setOpen] = useState(initiallyOpen);
  const [subtrees, setSubtrees] = useState<Record<string, boolean>>({});
  const [targets, setTargets] = useState<string[]>([]);
  const [plans, setPlans] = useState<SharePlan[]>([]);
  const [revokeTarget, setRevokeTarget] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const client = useQueryClient();
  const path = `/sharing/${kind}/${resourceId}`;
  const state = useQuery({ queryKey: ["sharing", kind, resourceId], queryFn: () => api.get<Sharing>(path), enabled: Boolean(resourceId) });
  const descendantsOf = (id: string) => subtrees[id] ?? state.data?.grants?.find((grant) => grant.target_scope_id === id)?.include_descendants ?? (state.data?.targets.find((target) => target.id === id)?.kind === "organization");
  const downloadOf = (id: string) => downloads[id] ?? downloadDefault ?? state.data?.grants?.find((grant) => grant.target_scope_id === id)?.allow_download ?? false;
  const targetPath = (id: string): string => {
    const parts: string[] = [];
    const seen = new Set<string>();
    let target = state.data?.targets.find((item) => item.id === id);
    while (target && !seen.has(target.id)) {
      seen.add(target.id); parts.unshift(target.name);
      target = state.data?.targets.find((item) => item.id === target?.parent_id && item.kind !== "organization");
    }
    return parts.join(" / ") || id;
  };
  const preview = useMutation({
    mutationFn: () => Promise.all(targets.map((target) => api.post<SharePlan>(`${path}/preview`, { target_scope_id: target, include_descendants: descendantsOf(target), ...(kind === "knowledge-bases" ? { allow_download: downloadOf(target) } : {}) }))),
    onSuccess: (result) => { setPlans(result.map((plan) => ({ ...plan, target_kind: state.data?.targets.find((target) => target.id === plan.target_scope_id)?.kind, target_name: targetPath(plan.target_scope_id), include_descendants: plan.include_descendants ?? descendantsOf(plan.target_scope_id), allow_download: downloadOf(plan.target_scope_id) }))); setMessage(null); },
    onError: (error) => setMessage(error.message),
  });
  const write = useMutation({
    mutationFn: async (revoke: boolean) => {
      if (revoke && revokeTarget) return api.delete(`${path}/${revokeTarget}`);
      if (!plans.length) throw new Error("请重新发起共享并确认");
      return api.post(`${path}/batch`, { targets: plans.map((plan) => ({ target_scope_id: plan.target_scope_id, confirmation: plan.confirmation, include_descendants: plan.include_descendants ?? descendantsOf(plan.target_scope_id), ...(kind === "knowledge-bases" ? { allow_download: plan.allow_download } : {}) })) });
    },
    onSuccess: async () => {
      setPlans([]); setRevokeTarget(null); setTargets([]); setSubtrees({}); setDownloads({}); setDownloadDefault(undefined); setAllowDownload(false); setMessage("共享设置已更新");
      await Promise.all(["sharing", "agents", "skills", "installed-skills", "mcps", "agent-capability-candidates", "knowledge-bases"].map((key) => client.invalidateQueries({ queryKey: [key] })));
    },
    onError: (error) => { setMessage(error.message); setPlans([]); },
  });
  const busy = preview.isPending || write.isPending;
  const legacyOrganizationGrant = (id: string) => state.data?.targets.some((target) => target.id === id && target.kind === "organization")
    && state.data?.grants?.some((grant) => grant.target_scope_id === id && !grant.include_descendants);
  return <section className="mt-3 overflow-hidden rounded-xl border border-[var(--line)]">
    <p className="px-4 pt-3 text-sm">使用范围：{state.data?.shared_scope_ids?.length ? state.data.shared_scope_ids.map((id) => legacyOrganizationGrant(id) ? "组织成员（历史授权）" : `${targetPath(id)}${state.data?.grants?.find((grant) => grant.target_scope_id === id)?.include_descendants ? "（含全部下级）" : "（仅本部门）"}`).join("、") : state.isLoading ? "加载中…" : state.error ? "读取失败" : publishOnShare ? "尚未发布，仅创建者可试用" : "尚未开放使用"}</p>
    <button type="button" className="relative flex min-h-12 w-full items-center justify-center gap-2 px-12 py-3 text-sm font-medium transition-colors hover:bg-[var(--surface-subtle)] focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--ink-soft)] disabled:cursor-not-allowed disabled:opacity-50" disabled={disabled} aria-expanded={open && !disabled} onClick={() => setOpen(!open)}>
      <Icon name="users" className="h-4 w-4 text-[var(--ink-soft)]" aria-hidden="true" />
      <span>{open ? "收起共享设置" : publishOnShare ? "发布并设置使用范围" : "共享"}</span>
      <Icon name="chevron" aria-hidden="true" className={`absolute right-4 h-4 w-4 text-[var(--ink-faint)] transition-transform ${open && !disabled ? "-rotate-90" : "rotate-90"}`} />
    </button>
    {disabled && <p className="px-4 pb-3 text-center text-xs text-neutral-500">请先保存当前配置，再共享已保存的智能体。</p>}
    {open && !disabled && <div className="space-y-3 border-t border-[var(--line)] p-4">
      {state.isLoading && <p className="text-sm">正在读取共享范围…</p>}
      {state.error && <p role="alert">{state.error.message}<button onClick={() => void state.refetch()}>重试</button></p>}
      {state.data && <>
        <p className="text-xs text-neutral-500">默认仅授权所选部门，不包含下级部门；可为每个部门单独选择包含本部门全部下级及未来新增部门。</p>
        <p className="text-xs text-neutral-500">选择「全组织可用」将向整个组织开放，包括未来新增部门。共享不会自动绑定智能体，也不会授予编辑或转共享权限。</p>
        {state.data.shared_scope_ids.map((id) => <div key={id} role="group" aria-label={`已有授权：${targetPath(id)}`} className="flex items-center justify-between gap-2 text-sm">
          <span>{legacyOrganizationGrant(id) ? "组织成员（历史授权）" : targetPath(id)}{kind === "knowledge-bases" && <span className="ml-2 text-xs text-neutral-500">{state.data.grants?.find((grant) => grant.target_scope_id === id)?.allow_download ? "可检索与下载" : "仅可检索"}</span>}</span>
          <button disabled={busy} className="text-red-600" onClick={() => { setRevokeTarget(id); setPlans([]); }}>取消共享</button>
        </div>)}
        {state.data.shared_scope_ids.some((id) => legacyOrganizationGrant(id)) && <p className="text-xs text-neutral-500">历史授权保留原有组织成员使用权；部门智能体仍需对应部门授权。</p>}
        {kind === "knowledge-bases" && <label className="flex items-start gap-2 text-sm"><input type="checkbox" aria-label="允许下载原文" checked={allowDownload} disabled={busy} onChange={(event) => { setAllowDownload(event.target.checked); setDownloadDefault(event.target.checked); setDownloads({}); setPlans([]); }} /><span>允许下载原文<span className="block text-xs text-neutral-500">批量设置所选范围的下载权限，也可在各部门单独调整。已有授权默认保留原设置。</span></span></label>}
        <fieldset disabled={busy} className="space-y-2">
          <legend className="mb-2 text-sm font-medium">{"选择使用范围"} <span className="font-normal text-[var(--ink-soft)]">可多选 · 已选 {targets.length} 个</span></legend>
          <div className="max-h-56 overflow-y-auto rounded-xl border border-[var(--line)] p-2">
            {state.data.targets.map((scope) => <div key={scope.id} className="rounded-lg p-3 text-sm hover:bg-[var(--surface-subtle)]">
              <label className="flex cursor-pointer items-center gap-3">
                <input type="checkbox" checked={targets.includes(scope.id)} onChange={() => { setTargets((current) => current.includes(scope.id) ? current.filter((id) => id !== scope.id) : [...current, scope.id]); setPlans([]); setRevokeTarget(null); }} />{targetPath(scope.id)}
              </label>
              {targets.includes(scope.id) && kind === "knowledge-bases" && <label className="ml-6 mt-2 flex items-center gap-2"><input type="checkbox" aria-label={`${targetPath(scope.id)}：允许下载原文`} checked={downloadOf(scope.id)} onChange={(event) => { setDownloads((current) => ({ ...current, [scope.id]: event.target.checked })); setPlans([]); }} />{targetPath(scope.id)}：允许下载原文</label>}
              {targets.includes(scope.id) && (scope.kind === "organization" && !legacyOrganizationGrant(scope.id) ? <p className="ml-6 mt-2 text-xs text-neutral-500">全组织可用，始终包含所有层级及未来新增部门。</p> : <label className="ml-6 mt-2 flex items-start gap-2">
                <input type="checkbox" aria-label={scope.kind === "organization" ? "明确升级为全组织可用" : `${targetPath(scope.id)}：同时包含下级部门`} checked={descendantsOf(scope.id)} onChange={(event) => { setSubtrees((current) => ({ ...current, [scope.id]: event.target.checked })); setPlans([]); }} />
                <span>{scope.kind === "organization" ? "明确升级为全组织可用" : "同时包含下级部门"}<span className="block text-xs text-neutral-500">包含本部门所有层级及未来新增部门；不授予管理权限。</span></span>
              </label>)}
            </div>)}
            {!state.data.targets.length && <p className="p-3 text-sm text-[var(--ink-soft)]">没有可共享的范围。</p>}
          </div>
        </fieldset>
        <button disabled={!targets.length || busy} className="product-control px-3 py-2 text-sm disabled:opacity-50" onClick={() => { setPlans([]); preview.mutate(); }}>{preview.isPending ? "检查中…" : publishOnShare ? "发布并设置使用范围" : "共享"}</button>
      </>}
      <Dialog open={plans.length > 0} onClose={() => { if (!write.isPending) setPlans([]); }} title="确认共享" description="请确认使用范围和共享内容。确认后，授权范围内的成员即可使用。" footer={<>
        <button type="button" disabled={write.isPending} className="product-control px-4 py-2 text-sm" onClick={() => setPlans([])}>返回修改</button>
        <button type="button" disabled={busy || plans.some((plan) => plan.blockers.length > 0)} className="rounded-xl bg-[var(--ink)] px-4 py-2 text-sm text-[var(--surface)] disabled:opacity-50" onClick={() => write.mutate(false)}>{write.isPending ? "共享中…" : "确认共享"}</button>
      </>}>
        <div className="max-h-[50dvh] overflow-y-auto"><ShareSummary plans={plans} />{plans.some((plan) => state.data?.grants?.some((grant) => grant.target_scope_id === plan.target_scope_id && grant.include_descendants) && !(plan.include_descendants ?? descendantsOf(plan.target_scope_id))) && <p className="mt-3 text-sm text-amber-700">确认后将所选授权收窄为仅本部门。下级部门的独立授权和来自祖先部门的授权不会撤销。</p>}{kind === "agents" && publishOnShare && <p className="mt-3 text-sm font-medium">确认共享将同时发布此私有草稿，并向所选范围授权使用。</p>}{kind === "knowledge-bases" && plans.map((plan) => <p key={plan.target_scope_id} className="mt-3 text-sm font-medium">{plan.target_name}：<span>{plan.allow_download ? "允许检索与下载原文" : "仅检索，不允许下载原文"}</span></p>)}</div>
      </Dialog>
      {revokeTarget && <div className="space-y-2 text-sm">
        <p>{kind === "agents" ? "取消后不能再通过此授权运行智能体；已共享的技能保持共享，MCP 连接和知识库授权也保留，不影响它们被其他智能体使用。" : kind === "knowledge-bases" ? "取消后不能再通过此授权检索或下载原文；其他有效授权仍然生效，已有智能体绑定保留。" : kind === "mcps" ? "取消后，依赖此连接授权的智能体将无法为目标部门成员执行；其他有效授权保留，来源连接和凭据不删除。" : "取消后，依赖此技能授权的智能体将无法为目标部门成员执行。已有绑定保留；若没有其他有效授权，将显示不可用。"}历史会话和已交付成果保留。</p>
        <button disabled={busy} className="text-red-600" onClick={() => write.mutate(true)}>确认取消共享</button>
        <button disabled={busy} className="ml-3" onClick={() => setRevokeTarget(null)}>保留共享</button>
      </div>}
      {message && <p role="status" className="text-sm">{message}</p>}
    </div>}
  </section>;
}
