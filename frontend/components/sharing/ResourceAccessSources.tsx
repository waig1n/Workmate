"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

type Access = {
  can_use: boolean;
  allow_download: boolean;
  sources: { target_scope_id: string; target_name: string; target_kind?: "organization" | "department"; target_parent_id?: string | null; include_descendants: boolean; allow_download: boolean; member_scope_ids?: string[]; inherited: boolean }[];
};

/** The access endpoint returns only grants matching the current user, never the recipient directory. */
export function ResourceAccessSources({ kind, resourceId }: { kind: "agents" | "skills" | "mcps" | "knowledge-bases"; resourceId: string }) {
  const state = useQuery({ queryKey: ["sharing", kind, resourceId, "access"], queryFn: () => api.get<Access>(`/sharing/${kind}/${resourceId}/access`) });
  return <section className="rounded-xl border border-[var(--line)] p-4 text-sm" aria-label="我的使用权限">
    <h3 className="font-medium">我的使用权限</h3>
    {state.isLoading ? <p>正在读取使用权限来源…</p> : state.isError || !Array.isArray(state.data?.sources) ? <p>使用权限来源暂时无法读取<button className="ml-2 underline" onClick={() => void state.refetch()}>重试</button></p> : <>
      <p className="mt-2">{state.data.can_use ? kind === "knowledge-bases" ? state.data.allow_download ? "可检索与下载原文" : "可检索，不可下载原文" : "已获得使用授权" : "尚未获得使用授权"}</p>
      {state.data.sources.map((source) => <p className="mt-2 text-xs text-[var(--ink-soft)]" key={source.target_scope_id}>{source.target_name}：{source.inherited ? "来自上级部门的共享" : "所在部门或组织的共享"}{source.include_descendants ? "（含全部下级）" : source.target_kind === "organization" || source.target_parent_id === null ? "（仅组织成员）" : "（仅本部门）"}{kind === "knowledge-bases" && <span> · {source.allow_download ? "此授权允许下载原文" : "此授权仅可检索"}</span>}</p>)}
    </>}
  </section>;
}
