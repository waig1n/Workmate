"use client";

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { RoleEditor, roleButtonClass, roleInputClass, type RoleTemplate } from "@/components/admin/roles-editor";
import { PermissionDirectory } from "@/components/admin/roles-permissions";
import { Dialog } from "@/components/ui/Dialog";
import { api } from "@/lib/api";
import { adminErrorMessage } from "@/lib/errors";
import { canRoot } from "@/lib/permissions";
import { usePermissions } from "@/lib/usePermissions";
import type { AdminRole } from "@/lib/types";

export default function RolesPage() {
  const cache = useQueryClient();
  const auth = usePermissions();
  const canRead = canRoot(auth.data, "role.read");
  const canManage = canRoot(auth.data, "role.manage");
  const roles = useQuery({ queryKey: ["admin-roles"], queryFn: api.adminRoles, enabled: canRead });
  const permissions = useQuery({ queryKey: ["admin-permissions"], queryFn: api.adminPermissions, enabled: canRead });
  const templates = useQuery({ queryKey: ["admin-role-templates"], queryFn: () => api.get<RoleTemplate[]>("/admin/role-templates"), enabled: canManage });
  const [query, setQuery] = useState("");
  const [editor, setEditor] = useState<{ role: AdminRole | null; copy?: boolean } | null>(null);
  const [deleting, setDeleting] = useState<AdminRole | null>(null);
  const [references, setReferences] = useState<AdminRole | null>(null);
  const [notice, setNotice] = useState("");
  const refresh = async () => { await Promise.all(["admin-roles", "admin-permissions", "admin-users", "admin-role-bindings", "admin-scopes", "auth"].map((key) => cache.invalidateQueries({ queryKey: [key] }))); };
  const remove = useMutation({ mutationFn: (id: string) => api.delete(`/admin/roles/${id}`), onSuccess: async () => { setDeleting(null); setNotice("角色已删除"); await refresh(); } });
  const filtered = (roles.data ?? []).filter((role) => `${role.display_name} ${role.key} ${role.description}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <main className="h-full overflow-y-auto bg-[var(--surface)] p-5 pb-28 sm:p-8 lg:p-10"><div className="mx-auto max-w-[1180px]">
    <header className="flex flex-wrap items-start justify-between gap-4"><div><h1 className="mt-2 text-3xl font-semibold">角色与权限</h1><p className="mt-3 max-w-2xl text-sm leading-6 text-[var(--ink-soft)]">配置角色权限，在用户或部门中分配。</p></div>{canManage && <button className={`${roleButtonClass} bg-[var(--ink)] text-[var(--surface)]`} disabled={!permissions.isSuccess} onClick={() => setEditor({ role: null })}>创建角色</button>}</header>
    {notice && <p role="status" className="mt-4 rounded-xl bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</p>}
    {auth.isPending ? <p className="mt-8" role="status">正在检查管理权限…</p> : auth.isError ? <LoadError error={auth.error} retry={() => void auth.refetch()} /> : !canRead ? <p role="alert" className="mt-8">你没有组织角色目录的查看权限。</p> : <>
      <section className="mt-8"><label className="block max-w-md text-sm">搜索角色<input className={roleInputClass} placeholder="名称、标识或用途" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        {roles.isError ? <LoadError error={roles.error} retry={() => void roles.refetch()} /> : roles.isPending ? <p role="status" className="mt-5">正在加载角色…</p> : !filtered.length ? <p className="mt-5 rounded-xl border border-[var(--line)] p-6 text-sm">{query ? "没有匹配的角色，请调整搜索条件。" : "尚无角色。创建角色后即可为用户分配权限。"}</p> : <div className="mt-5 grid gap-4 md:grid-cols-2">{filtered.map((role) => <article key={role.id} className="rounded-2xl border border-[var(--line)] bg-[var(--surface-elevated)] p-5">
          <div className="flex items-start justify-between gap-3"><h2 className="text-lg font-semibold">{role.display_name}</h2><span className="rounded-full bg-[var(--surface-subtle)] px-3 py-1 text-xs">{role.is_system ? "系统角色" : role.delegatable ? "允许部门分配" : "自定义角色"}</span></div>
          <p className="mt-2 line-clamp-1 text-sm leading-5 text-[var(--ink-soft)]">{role.description}</p><p className="mt-3 text-sm">{role.permission_keys.length} 项权限 · {role.user_count ?? "—"} 位用户</p>
          <div className="mt-5 flex flex-wrap gap-2"><button className={roleButtonClass} disabled={!permissions.isSuccess} onClick={() => setEditor({ role })}>{canManage && !role.is_system ? "编辑权限" : "查看权限"}</button><button className={roleButtonClass} onClick={() => setReferences(role)}>授权范围</button>{canManage && <><button className={roleButtonClass} disabled={!permissions.isSuccess} onClick={() => setEditor({ role, copy: true })}>复制角色</button>{!role.is_system && <button className={`${roleButtonClass} text-red-600`} onClick={() => { remove.reset(); setDeleting(role); }}>删除</button>}</>}</div>
        </article>)}</div>}
      </section>
      {permissions.isError ? <LoadError error={permissions.error} retry={() => void permissions.refetch()} /> : permissions.isPending ? <p className="mt-6" role="status">正在加载权限目录…</p> : <PermissionDirectory permissions={permissions.data} canManage={canManage} onDone={refresh} />}
      {templates.isError && <div className="mt-4 text-sm"><p>常用角色模板暂不可用，仍可手动创建角色。</p><LoadError error={templates.error} retry={() => void templates.refetch()} /></div>}
    </>}
  </div>
    {editor && permissions.isSuccess && <RoleEditor key={`${editor.role?.id ?? "new"}:${Boolean(editor.copy)}`} role={editor.role} copy={editor.copy} forceReadOnly={!canManage} templates={templates.data} permissions={permissions.data} onClose={() => setEditor(null)} onDone={async () => { setEditor(null); setNotice("角色已保存"); await refresh(); }} />}
    {deleting && <Dialog open onClose={() => { if (!remove.isPending) setDeleting(null); }} title={`删除 ${deleting.display_name}`}><p className="text-sm leading-6">该角色有 {deleting.user_count ?? "—"} 位授权用户、{deleting.binding_count} 条授权。仍被引用时无法删除，请先在用户或部门中撤销授权。</p>{remove.error && <p role="alert" className="mt-3 text-sm text-red-600">{adminErrorMessage(remove.error)}</p>}<div className="mt-5 flex justify-end gap-3"><button className={roleButtonClass} disabled={remove.isPending} onClick={() => setDeleting(null)}>取消</button><button className={`${roleButtonClass} text-red-600`} disabled={remove.isPending || deleting.binding_count > 0} onClick={() => remove.mutate(deleting.id)}>{remove.isPending ? "删除中…" : "确认删除"}</button></div></Dialog>}
    {references && <RoleReferences key={references.id} role={references} onClose={() => setReferences(null)} />}
  </main>;
}

function LoadError({ error, retry }: { error: unknown; retry: () => void }) {
  return <div role="alert" className="mt-5 rounded-xl border border-red-200 p-4 text-sm"><p>{adminErrorMessage(error)}</p><button className={`${roleButtonClass} mt-3`} onClick={retry}>重试加载</button></div>;
}

function RoleReferences({ role, onClose }: { role: AdminRole; onClose: () => void }) {
  const references = useInfiniteQuery({ queryKey: ["admin-role-bindings", role.id], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => api.adminRoleBindings(role.id, pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  return <Dialog open title={`${role.display_name} 的授权范围`} onClose={onClose} width="max-w-[760px]" description="角色定义变更将影响以下所有授权；具体动作还取决于其他角色与资源访问规则。">
    <div className="max-h-[60vh] space-y-3 overflow-y-auto">{references.isPending ? <p role="status">正在加载授权…</p> : <>{references.data?.pages.flatMap((page) => page.items).map((binding, index) => <article key={`${binding.user_id}:${binding.scope_id}:${index}`} className="rounded-xl border border-[var(--line)] p-3 text-sm"><p className="font-medium">{binding.display_name}</p><p className="mt-1 text-[var(--ink-soft)]">{binding.email}</p><p className="mt-2">{binding.scope_name} · {binding.include_descendants ? "包含下级部门" : "仅当前范围"}</p></article>)}{references.isSuccess && !references.data.pages[0].items.length && <p className="text-sm">此角色尚未分配给用户。</p>}</>}
      {references.isError && <LoadError error={references.error} retry={() => void (references.data ? references.fetchNextPage() : references.refetch())} />}
      {references.hasNextPage && <button className={roleButtonClass} disabled={references.isFetchingNextPage} onClick={() => void references.fetchNextPage()}>{references.isFetchingNextPage ? "加载中…" : "加载更多授权"}</button>}
    </div>
  </Dialog>;
}
