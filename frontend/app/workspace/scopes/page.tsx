"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Dialog } from "@/components/ui/Dialog";
import { buttonClass, fieldClass, flattenScopes, FormError, primaryClass } from "@/components/admin/people-access";
import { DepartmentPeople } from "@/components/admin/people-department";
import { api } from "@/lib/api";
import { scopeCan } from "@/lib/permissions";
import type { AdminScopeNode } from "@/lib/types";

type ScopeAction = { kind: "create" | "rename" | "move" | "delete"; scope: AdminScopeNode };
type MovePreview = { tree_version: number; descendant_count: number; member_count: number; agent_count: number; removed_inherited_bindings: number; added_inherited_bindings: number; capability_blockers: number; shared_resource_changes?: Record<"agents" | "skills" | "mcps" | "knowledge_bases" | "knowledge_downloads", { gained: number; lost: number }> };

export default function ScopesPage() {
  const cache = useQueryClient();
  const tree = useQuery({ queryKey: ["admin-scopes"], queryFn: api.adminScopeTree });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [action, setAction] = useState<ScopeAction | null>(null);
  const [notice, setNotice] = useState("");
  const nodes = flattenScopes(tree.data ?? []);
  const selected = nodes.find((node) => node.id === selectedId) ?? null;
  const refresh = async () => { await Promise.all([cache.invalidateQueries({ queryKey: ["admin-scopes"] }), cache.invalidateQueries({ queryKey: ["scopes"] }), cache.invalidateQueries({ queryKey: ["auth", "permissions"] }), cache.invalidateQueries({ queryKey: ["admin-scope-members"] }), cache.invalidateQueries({ queryKey: ["admin-scope-bindings"] })]); };
  return <div className="h-full overflow-y-auto bg-[var(--surface)] p-5 pb-28 sm:p-8 lg:p-10"><div className="mx-auto max-w-[1280px]"><header><h1 className="mt-2 text-3xl font-semibold">部门管理</h1></header>
    {notice && <p role="status" className="mt-4 text-sm text-emerald-700">{notice}</p>}
    <FormError error={tree.error} retry={() => void tree.refetch()} />
    <div className="mt-7 grid items-start gap-6 lg:grid-cols-[320px_minmax(0,1fr)]"><section className="rounded-2xl border border-[var(--line)] p-4"><label className="text-sm font-medium">组织架构<input aria-label="搜索部门" className={fieldClass} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索部门名称" /></label><div className="mt-4">{tree.isLoading && <p role="status" className="py-5 text-sm">正在加载组织架构…</p>}{search ? nodes.filter((node) => node.name.toLowerCase().includes(search.toLowerCase())).map((node) => <button key={node.id} className="block w-full rounded-xl p-3 text-left text-sm hover:bg-[var(--surface-subtle)]" onClick={() => setSelectedId(node.id)}>{node.name}</button>) : tree.data?.map((node) => <ScopeBranch key={node.id} node={node} depth={0} selectedId={selectedId} onSelect={setSelectedId} />)}{tree.isSuccess && nodes.length === 0 && <p className="py-5 text-sm text-[var(--ink-soft)]">没有可管理的部门。</p>}</div></section>
      <section className="min-w-0 rounded-2xl border border-[var(--line)] p-5 sm:p-6">{selected ? <><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="mt-1 text-xl font-semibold">{selected.name}</h2><p className="mt-2 text-sm text-[var(--ink-soft)]">{selected.children.length} 个下级部门</p></div>{scopeCan(selected, "scope.manage") && <button className={primaryClass} onClick={() => setAction({ kind: "create", scope: selected })}>新建下级部门</button>}</div>
        {selected.kind === "department" && scopeCan(selected, "scope.manage") && <div className="mt-5 flex flex-wrap gap-2"><button className={buttonClass} onClick={() => setAction({ kind: "rename", scope: selected })}>重命名</button><button className={buttonClass} onClick={() => setAction({ kind: "move", scope: selected })}>移动部门</button><button className={`${buttonClass} text-red-600`} onClick={() => setAction({ kind: "delete", scope: selected })}>删除部门</button></div>}
        <DepartmentPeople key={selected.id} scope={selected} scopes={nodes} onSelectScope={setSelectedId} />
      </> : <div className="py-20 text-center"><h2 className="font-semibold">选择一个部门</h2><p className="mt-2 text-sm text-[var(--ink-soft)]">从左侧选择即可查看成员和角色。</p></div>}</section>
    </div></div>{action && <ScopeActionDialog action={action} nodes={nodes} onClose={() => setAction(null)} onDone={async () => { if (action.kind === "delete") setSelectedId(null); setAction(null); setNotice("部门信息已更新。"); await refresh(); }} />}</div>;
}

function ScopeBranch({ node, depth, selectedId, onSelect }: { node: AdminScopeNode; depth: number; selectedId: string | null; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(true);
  return <div><div className={`flex items-center gap-1 rounded-xl py-2 ${selectedId === node.id ? "bg-[var(--surface-subtle)]" : "hover:bg-[var(--surface-subtle)]"}`} style={{ paddingLeft: `${Math.min(depth, 8) * 12}px` }}><button className="h-8 w-8 shrink-0 text-sm" aria-label={`${open ? "收起" : "展开"}${node.name}`} aria-expanded={open} disabled={!node.children.length} onClick={() => setOpen(!open)}>{node.children.length ? open ? "▾" : "▸" : "·"}</button><button className="min-w-0 flex-1 truncate pr-2 text-left text-sm" title={node.name} onClick={() => onSelect(node.id)}>{node.name}</button></div>{open && node.children.map((child) => <ScopeBranch key={child.id} node={child} depth={depth + 1} selectedId={selectedId} onSelect={onSelect} />)}</div>;
}

function ScopeActionDialog({ action, nodes, onClose, onDone }: { action: ScopeAction; nodes: AdminScopeNode[]; onClose: () => void; onDone: () => Promise<void> }) {
  const [name, setName] = useState(action.kind === "rename" ? action.scope.name.split(" / ").at(-1) ?? "" : "");
  const [parentId, setParentId] = useState("");
  const descendants = new Set(flattenScopes([action.scope]).map((node) => node.id));
  const candidates = nodes.filter((node) => !descendants.has(node.id) && node.id !== action.scope.parent_id && scopeCan(node, "scope.manage"));
  const preview = useQuery({ queryKey: ["scope-move-preview", action.scope.id, parentId], queryFn: () => api.post<MovePreview>(`/admin/scopes/${action.scope.id}/move-preview`, { new_parent_id: parentId }), enabled: action.kind === "move" && Boolean(parentId), retry: false });
  const mutation = useMutation({ mutationFn: () => {
    const path = `/admin/scopes/${action.scope.id}`;
    if (action.kind === "create") return api.post("/admin/scopes", { parent_id: action.scope.id, name });
    if (action.kind === "rename") return api.patch(path, { name, expected_tree_version: action.scope.tree_version });
    if (action.kind === "move") return api.post(`${path}/move`, { new_parent_id: parentId, expected_tree_version: preview.data?.tree_version });
    return api.delete(`${path}?expected_tree_version=${action.scope.tree_version}`);
  }, onSuccess: onDone });
  const title = { create: "新建下级部门", rename: "重命名部门", move: "移动部门", delete: "删除部门" }[action.kind];
  return <Dialog open title={title} description={action.scope.name} onClose={() => { if (!mutation.isPending) onClose(); }}><form onSubmit={(event) => { event.preventDefault(); mutation.mutate(); }}>
    {(action.kind === "create" || action.kind === "rename") && <label className="text-sm">部门名称<input required maxLength={100} className={fieldClass} value={name} onChange={(event) => setName(event.target.value)} /></label>}
    {action.kind === "move" && <><p className="mb-4 text-sm text-[var(--ink-soft)]">此部门和全部下级将进入新的上级路径，继承权限和资源共享范围可能改变。服务端将重新检查约束。</p><label className="text-sm">新的上级部门<select required className={fieldClass} value={parentId} onChange={(event) => setParentId(event.target.value)}><option value="">选择上级部门</option>{candidates.map((node) => <option key={node.id} value={node.id}>{node.name}</option>)}</select></label></>}
    {action.kind === "delete" && <p className="text-sm text-[var(--ink-soft)]">仅可删除没有下级、成员、角色授权和资源的部门。若仍有关联数据，系统会阻止删除；不会级联清理资源。</p>}
    {action.kind === "move" && parentId && <div className="mt-4 rounded-xl bg-[var(--surface-subtle)] p-4 text-sm">{preview.isFetching ? <p role="status">正在检查移动影响…</p> : preview.data ? <><p>影响 {preview.data.descendant_count} 个部门（含当前）、{preview.data.member_count} 位成员、{preview.data.agent_count} 个 Agent。</p><p className="mt-2">将移除 {preview.data.removed_inherited_bindings} 条祖先授权来源，新增 {preview.data.added_inherited_bindings} 条。实际权限按角色并集计算。</p>{preview.data.shared_resource_changes && <div className="mt-3 border-t border-[var(--line)] pt-3"><p className="font-medium">共享使用范围变化（按部门统计）</p>{([['agents', 'Agent'], ['skills', '技能'], ['mcps', 'MCP 连接'], ['knowledge_bases', '知识库'], ['knowledge_downloads', '原文下载']] as const).map(([key, label]) => <p key={key}>{label}：新增 {preview.data.shared_resource_changes![key]?.gained ?? 0}，失去 {preview.data.shared_resource_changes![key]?.lost ?? 0}</p>)}<p className="mt-2 text-[var(--muted)]">包含下级的共享随部门移动自动变化；独立共享继续保留。成员通过其他部门获得的使用权不受影响。</p></div>}{preview.data.capability_blockers > 0 && <p role="alert" className="mt-2 text-red-700">有 {preview.data.capability_blockers} 个能力绑定会失效（含知识库或已共享 Agent 的依赖），请先调整配置或共享范围。</p>}</> : null}<FormError error={preview.error} retry={() => void preview.refetch()} /></div>}
    <FormError error={mutation.error} /><div className="mt-5 flex justify-end gap-2"><button type="button" className={buttonClass} disabled={mutation.isPending} onClick={onClose}>取消</button><button className={primaryClass} disabled={mutation.isPending || (action.kind === "move" && (!parentId || !preview.data || preview.isFetching || preview.isError || preview.data.capability_blockers > 0))}>{mutation.isPending ? "处理中…" : "确认"}</button></div>
  </form></Dialog>;
}
