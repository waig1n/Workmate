"use client";

import { DepartmentTransfer } from "./department-transfer";
import Link from "next/link";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Dialog } from "@/components/ui/Dialog";
import { buttonClass, fieldClass, FormError, primaryClass } from "./people-access";
import { api } from "@/lib/api";
import { scopeCan } from "@/lib/permissions";
import type { AdminRole, AdminScopeNode, Permissions } from "@/lib/types";

type Member = { id: string; display_name: string; email: string; status: string; access_version: number; membership_scope_ids: string[] };
type Binding = { user_id: string; display_name: string; email: string; access_version: number; role_key: string; role_name: string; include_descendants: boolean };
type Page<T> = { items: T[]; next_cursor: string | null; counts?: { direct: number; total: number; limited: boolean } };

export function DepartmentPeople({ scope, scopes, onSelectScope }: { scope: AdminScopeNode; scopes: AdminScopeNode[]; onSelectScope?: (id: string) => void }) {
  const isOrganization = scope.kind === "organization";
  const cache = useQueryClient(); const [tab, setTab] = useState("members");
  const [q, setQ] = useState(""); const [descendants, setDescendants] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<Member | null>(null);
  const [showAdd, setShowAdd] = useState(false); const [showBind, setShowBind] = useState(false);
  const [bindingTarget, setBindingTarget] = useState<Binding | null>(null);
  const permissions = useQuery({ queryKey: ["auth", "permissions"], queryFn: () => api.get<Permissions>("/auth/me/permissions") });
  const canReadUsers = permissions.data?.root_permissions?.includes("user.read") ?? false;
  const canMembers = scopeCan(scope, "membership.read"); const canManage = scopeCan(scope, "membership.manage"); const canBind = scopeCan(scope, "role.bind");
  const members = useInfiniteQuery({ queryKey: ["admin-scope-members", scope.id, q, descendants], initialPageParam: "", enabled: canMembers,
    queryFn: ({ pageParam }) => api.get<Page<Member>>(`/admin/scopes/${scope.id}/members?${new URLSearchParams({ q, include_descendants: String(descendants), ...(pageParam ? { cursor: pageParam } : {}) })}`), getNextPageParam: (page) => page.next_cursor || undefined });
  const bindings = useInfiniteQuery({ queryKey: ["admin-scope-bindings", scope.id], initialPageParam: "", enabled: canBind,
    queryFn: ({ pageParam }) => api.get<Page<Binding>>(`/admin/scopes/${scope.id}/role-bindings${pageParam ? `?cursor=${encodeURIComponent(pageParam)}` : ""}`), getNextPageParam: (page) => page.next_cursor || undefined });
  const refresh = async () => { await Promise.all([cache.invalidateQueries({ queryKey: ["admin-scope-members"] }), cache.invalidateQueries({ queryKey: ["admin-scope-bindings"] }), cache.invalidateQueries({ queryKey: ["admin-effective-access"] }), cache.invalidateQueries({ queryKey: ["admin-member-candidates"] }), cache.invalidateQueries({ queryKey: ["admin-users"] }), cache.invalidateQueries({ queryKey: ["admin-user"] }), cache.invalidateQueries({ queryKey: ["admin-effective-access"] }), cache.invalidateQueries({ queryKey: ["auth", "permissions"] }), cache.invalidateQueries({ queryKey: ["admin-scopes"] })]); };
  const unbind = useMutation({ mutationFn: () => api.delete(`/admin/scopes/${scope.id}/role-bindings/${bindingTarget!.user_id}/${encodeURIComponent(bindingTarget!.role_key)}?expected_version=${bindingTarget!.access_version}`), onSuccess: async () => { setBindingTarget(null); await refresh(); } });
  const memberItems = members.data?.pages.flatMap((page) => page.items) ?? [];
  const counts = members.data?.pages[0]?.counts;
  const bindingItems = bindings.data?.pages.flatMap((page) => page.items) ?? [];
  return <div className="mt-7 border-t border-[var(--line)] pt-5"><nav className="flex gap-2"><button className={`${buttonClass} ${tab === "members" ? "font-semibold bg-[var(--surface-subtle)]" : ""}`} onClick={() => setTab("members")}>{isOrganization ? "企业成员" : "部门成员"}</button><button className={`${buttonClass} ${tab === "bindings" ? "font-semibold bg-[var(--surface-subtle)]" : ""}`} onClick={() => setTab("bindings")}>管理职责</button></nav>
    {tab === "members" && <section className="mt-5">{!canMembers ? <p className="text-sm text-[var(--ink-soft)]">你没有此部门的成员查看权限。</p> : <><div className="flex flex-wrap items-center gap-3"><input aria-label="搜索部门成员" className={`${fieldClass} sm:max-w-64`} placeholder="搜索成员" value={q} onChange={(event) => setQ(event.target.value)} /><div role="group" aria-label="成员范围" className="flex flex-wrap gap-2"><button className={`${buttonClass} ${!descendants ? "font-semibold bg-[var(--surface-subtle)]" : ""}`} aria-pressed={!descendants} onClick={() => setDescendants(false)}>{isOrganization ? "未分配部门" : "直属成员"}</button><button className={`${buttonClass} ${descendants ? "font-semibold bg-[var(--surface-subtle)]" : ""}`} aria-pressed={descendants} onClick={() => setDescendants(true)}>{isOrganization ? "全部成员" : "全部成员（含下级）"}</button></div>{canManage && !isOrganization && <button className={primaryClass} onClick={() => setShowAdd(true)}>添加成员</button>}</div><p className="mt-3 text-sm text-[var(--ink-soft)]">{counts ? `${isOrganization ? "未分配部门" : "直属"} ${counts.direct} 人 · ${isOrganization ? "全部" : "含下级"} ${counts.total} 人（按用户去重）` : members.isError ? "人数统计暂不可用" : "正在统计成员…"}{counts?.limited && " · 仅统计有权查看的部门"}</p><details className="mt-2 text-xs text-[var(--ink-soft)]"><summary className="cursor-pointer">统计说明</summary><p className="mt-2 leading-5">{isOrganization && "组织成员身份不代表直属部门；未分配部门仅展示没有任何部门归属的组织成员。"}人数按用户去重，不随搜索条件变化。汇总展示不改变部门归属或管理职责。</p></details><FormError error={members.error} retry={() => void members.refetch()} />{members.isLoading && <p role="status" className="py-5 text-sm">正在加载成员…</p>}{members.isSuccess && memberItems.length === 0 && <p className="py-8 text-center text-sm text-[var(--ink-soft)]">此范围内没有符合条件的成员。</p>}<div className="mt-4 divide-y divide-[var(--line)]">{memberItems.map((member) => <div key={member.id} className="flex flex-wrap items-center justify-between gap-3 py-4"><div><p className="text-sm font-semibold">{member.display_name}<span className="ml-2 font-normal text-[var(--ink-soft)]">{member.status === "disabled" ? "已禁用" : "正常"}</span></p><p className="mt-1 text-sm text-[var(--ink-soft)]">{member.email}</p><p className="mt-1 text-sm text-[var(--ink-soft)]">归属：{member.membership_scope_ids.map((id) => scopes.find((item) => item.id === id)?.name ?? "归属信息不可见").join("、")}</p></div><div className="flex gap-3 text-sm">{member.membership_scope_ids.filter((id) => id !== scope.id).map((id) => { const target = scopes.find((item) => item.id === id); return target && scopeCan(target, "membership.read") && onSelectScope ? <button key={id} className="underline" onClick={() => onSelectScope(id)}>{scopeCan(target, "membership.manage") ? "进入所属部门管理" : "查看所属部门"} · {target.name}</button> : null; })}{canReadUsers && <Link href={`/workspace/users?user=${encodeURIComponent(member.id)}`} className="underline">用户详情</Link>}{canManage && !isOrganization && member.membership_scope_ids.includes(scope.id) && <button className="text-red-600" onClick={() => { setRemoveTarget(member); }}>移出部门</button>}</div></div>)}</div>{members.hasNextPage && <button className={buttonClass} disabled={members.isFetchingNextPage} onClick={() => void members.fetchNextPage()}>加载更多成员</button>}</>}</section>}
    {tab === "bindings" && <section className="mt-5">{!canBind ? <p className="text-sm text-[var(--ink-soft)]">你没有此部门的管理职责管理权限。</p> : <><div className="flex flex-wrap items-center justify-between gap-3"><span className="text-sm text-[var(--ink-soft)]" title="继承授权请到上级来源修改">本部门直接授权</span><button className={primaryClass} onClick={() => setShowBind(true)}>分配管理职责</button></div><FormError error={bindings.error} retry={() => void bindings.refetch()} />{bindings.isLoading && <p role="status" className="py-5 text-sm">正在加载授权…</p>}{bindings.isSuccess && bindingItems.length === 0 && <p className="py-8 text-center text-sm text-[var(--ink-soft)]">此部门还没有直接管理职责。</p>}<div className="mt-4 divide-y divide-[var(--line)]">{bindingItems.map((binding) => <div key={`${binding.user_id}:${binding.role_key}`} className="flex flex-wrap items-center justify-between gap-3 py-4 text-sm"><div><p className="font-semibold">{binding.display_name} · {binding.role_name}</p><p className="mt-1 text-[var(--ink-soft)]">{binding.include_descendants ? "本部门及全部下级部门" : "仅本部门"}</p></div><button className="text-red-600" onClick={() => { unbind.reset(); setBindingTarget(binding); }}>撤销授权</button></div>)}</div>{bindings.hasNextPage && <button className={buttonClass} disabled={bindings.isFetchingNextPage} onClick={() => void bindings.fetchNextPage()}>加载更多授权</button>}</>}</section>}
    {showAdd && <AddMember scope={scope} onClose={() => setShowAdd(false)} onDone={async () => { setShowAdd(false); await refresh(); }} />}
    {showBind && <AssignRole scope={scope} onClose={() => setShowBind(false)} onDone={async () => { setShowBind(false); await refresh(); }} />}
    {removeTarget && <RemoveMember scope={scope} member={removeTarget} canBind={canBind} onClose={() => setRemoveTarget(null)} onDone={async () => { setRemoveTarget(null); await refresh(); }} />}
    <Dialog open={Boolean(bindingTarget)} title="撤销管理职责" onClose={() => { if (!unbind.isPending) setBindingTarget(null); }}><p className="text-sm">撤销 {bindingTarget?.display_name} 在此部门的 {bindingTarget?.role_name} 授权。其他直接或继承角色仍可能提供相同权限。</p><FormError error={unbind.error} /><button className={`${primaryClass} mt-5`} disabled={unbind.isPending} onClick={() => unbind.mutate()}>{unbind.isPending ? "处理中…" : "确认撤销"}</button></Dialog>
  </div>;
}

function AddMember({ scope, onClose, onDone }: { scope: AdminScopeNode; onClose: () => void; onDone: () => Promise<void> }) {
  const [selected, setSelected] = useState<Member | null>(null);
  const add = useMutation({ mutationFn: () => api.put(`/admin/scopes/${scope.id}/members/${selected!.id}`, { expected_version: selected!.access_version }), onSuccess: onDone });
  return <Dialog open title={`添加成员 · ${scope.name}`} onClose={() => { if (!add.isPending) onClose(); }}><p className="mb-3 text-xs text-[var(--ink-soft)]">仅显示可管理的人员</p><MemberPicker scopeId={scope.id} selected={selected} onSelect={setSelected} disabled={add.isPending} /><FormError error={add.error} /><button className={`${primaryClass} mt-5`} disabled={!selected || add.isPending} onClick={() => add.mutate()}>{add.isPending ? "添加中…" : "添加到部门"}</button></Dialog>;
}

function AssignRole({ scope, onClose, onDone }: { scope: AdminScopeNode; onClose: () => void; onDone: () => Promise<void> }) {
  const [member, setMember] = useState<Member | null>(null); const [roleKey, setRoleKey] = useState(""); const [descendants, setDescendants] = useState(false);
  const roles = useQuery({ queryKey: ["admin-assignable-roles", scope.id], queryFn: () => api.get<AdminRole[]>(`/admin/scopes/${scope.id}/assignable-roles`) });
  const assign = useMutation({ mutationFn: () => api.put(`/admin/scopes/${scope.id}/role-bindings/${member!.id}/${encodeURIComponent(roleKey)}`, { include_descendants: descendants, expected_version: member!.access_version }), onSuccess: onDone });
  return <Dialog open title={`分配管理职责 · ${scope.name}`} onClose={() => { if (!assign.isPending) onClose(); }}><form className="max-h-[65vh] space-y-4 overflow-y-auto" onSubmit={(event) => { event.preventDefault(); assign.mutate(); }}><MemberPicker scopeId={scope.id} selected={member} onSelect={setMember} disabled={assign.isPending} /><label className="block text-sm">可分配管理职责<select required className={fieldClass} value={roleKey} onChange={(event) => setRoleKey(event.target.value)}><option value="">选择角色</option>{roles.data?.map((item) => <option key={item.id} value={item.key}>{item.display_name}</option>)}</select></label><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={descendants} onChange={(event) => setDescendants(event.target.checked)} />包含下级部门</label><p className="text-sm text-[var(--ink-soft)]">可授予范围和权限上限由服务端校验。默认仅在本部门生效。</p><FormError error={roles.error} retry={() => void roles.refetch()} /><FormError error={assign.error} /><button className={primaryClass} disabled={!member || !roleKey || !roles.isSuccess || assign.isPending}>{assign.isPending ? "分配中…" : "确认分配"}</button></form></Dialog>;
}

function MemberPicker({ scopeId, selected, onSelect, disabled }: { scopeId: string; selected: Member | null; onSelect: (member: Member | null) => void; disabled: boolean }) {
  const [q, setQ] = useState("");
  const candidates = useInfiniteQuery({ queryKey: ["admin-member-candidates", scopeId, q], initialPageParam: "", queryFn: ({ pageParam }) => api.get<Page<Member>>(`/admin/scopes/${scopeId}/member-candidates?${new URLSearchParams({ q, ...(pageParam ? { cursor: pageParam } : {}) })}`), getNextPageParam: (page) => page.next_cursor || undefined });
  const users = candidates.data?.pages.flatMap((page) => page.items) ?? [];
  return <fieldset disabled={disabled}><label className="text-sm">查找用户<input className={fieldClass} placeholder="输入姓名或邮箱" value={q} onChange={(event) => { setQ(event.target.value); onSelect(null); }} /></label><FormError error={candidates.error} retry={() => void candidates.refetch()} /><div className="mt-3 max-h-48 overflow-y-auto">{users.map((user) => <label key={user.id} className="flex items-center gap-3 rounded-lg p-3 text-sm hover:bg-[var(--surface-subtle)]"><input type="radio" name="member" checked={selected?.id === user.id} onChange={() => onSelect(user)} /><span>{user.display_name}<span className="ml-2 text-[var(--ink-soft)]">{user.email}</span></span></label>)}{candidates.isLoading && <p className="p-3 text-sm">正在搜索…</p>}{candidates.isSuccess && !users.length && <p className="p-3 text-sm">没有可管理的匹配用户。</p>}</div>{candidates.hasNextPage && <button type="button" className={buttonClass} disabled={candidates.isFetchingNextPage} onClick={() => void candidates.fetchNextPage()}>加载更多用户</button>}</fieldset>;
}


type AccessSource = { role_key: string; role_name: string; scope_id: string | null; scope_name: string; inherited: boolean; include_descendants: boolean };

function RemoveMember({ scope, member, canBind, onClose, onDone }: { scope: AdminScopeNode; member: Member; canBind: boolean; onClose: () => void; onDone: () => Promise<void> }) {
  const [retain, setRetain] = useState(false);
  const access = useQuery({ queryKey: ["admin-effective-access", member.id, scope.id], queryFn: () => api.get<{ sources: AccessSource[] }>(`/admin/users/${member.id}/effective-access?scope_id=${encodeURIComponent(scope.id)}`) });
  const direct = access.data?.sources.filter((source) => !source.inherited && source.scope_id === scope.id) ?? [];
  const inherited = access.data?.sources.filter((source) => source.inherited) ?? [];
  const remove = useMutation({ mutationFn: () => api.delete(`/admin/scopes/${scope.id}/members/${member.id}?expected_version=${member.access_version}&retain_departure_roles=${retain}`), onSuccess: onDone });
  return <Dialog open title="移出部门" onClose={() => { if (!remove.isPending) onClose(); }}>
    <p className="text-sm">将 {member.display_name} 移出 {scope.name}</p>
    <FormError error={access.error} retry={() => void access.refetch()} />
    {access.isLoading && <p role="status" className="mt-3 text-sm">正在检查部门职责…</p>}
    {access.isSuccess && <DepartmentTransfer scopes={[scope]} before={[scope.id]} after={[]} bindings={direct.map((source) => ({ scope_id: scope.id, role_key: source.role_key, include_descendants: source.include_descendants }))} roles={direct.map((source) => ({ key: source.role_key, display_name: source.role_name }) as AdminRole)} canBind={canBind} retain={retain} onRetain={setRetain} disabled={remove.isPending} />}
    {inherited.length > 0 && <p className="mt-3 text-sm">继续生效的上级职责：{inherited.map((source) => `${source.role_name} · ${source.scope_name}`).join("、")}</p>}
    <FormError error={remove.error} />
    <button className={`${primaryClass} mt-5`} disabled={remove.isPending || !access.isSuccess || (!canBind && direct.length > 0)} onClick={() => remove.mutate()}>{remove.isPending ? "处理中…" : "确认移出"}</button>
  </Dialog>;
}
