"use client";

import type { AdminRole, AdminRoleBinding, AdminScopeNode } from "@/lib/types";

export function departingDepartments(scopes: AdminScopeNode[], before: string[], after: string[]) {
  return scopes.filter((scope) => scope.kind === "department" && before.includes(scope.id) && !after.includes(scope.id));
}

export function DepartmentTransfer({ scopes, before, after, bindings, roles, canBind, retain, onRetain, disabled = false }: {
  scopes: AdminScopeNode[]; before: string[]; after: string[];
  bindings: AdminRoleBinding[]; roles: Pick<AdminRole, "key" | "display_name">[]; canBind: boolean;
  retain: boolean; onRetain: (retain: boolean) => void; disabled?: boolean;
}) {
  const removed = departingDepartments(scopes, before, after);
  const added = scopes.filter((scope) => scope.kind === "department" && after.includes(scope.id) && !before.includes(scope.id));
  if (!removed.length && !added.length) return null;
  const departingRoles = bindings.filter((binding) => removed.some((scope) => scope.id === binding.scope_id));
  return <section aria-label="调岗影响" className="mt-4 space-y-3 rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
    <h3 className="font-semibold">部门调整 · 保存后生效</h3>
    {removed.length > 0 && <p>移出：{removed.map((scope) => scope.name).join("、")}</p>}
    {added.length > 0 && <p>加入：{added.map((scope) => scope.name).join("、")}</p>}
    <p>{removed.length > 0 && "将失去原部门提供的资源使用授权。"}{added.length > 0 && "将获得新部门开放的资源。"}全组织授权及其他所属部门提供的授权继续有效，历史会话与已交付成果保留。</p>
    {departingRoles.length > 0 && <>
      <p className="font-medium">{retain ? "将保留以下跨部门管理职责：" : "将撤销以下原部门管理职责："}</p>
      <ul className="list-inside list-disc">{departingRoles.map((binding) => <li key={`${binding.scope_id}:${binding.role_key}`}>{roles.find((role) => role.key === binding.role_key)?.display_name ?? binding.role_key} · {scopes.find((scope) => scope.id === binding.scope_id)?.name}{binding.include_descendants && "（含下级部门）"}</li>)}</ul>
      {canBind ? <label className="flex items-center gap-2"><input type="checkbox" checked={retain} disabled={disabled} onChange={(event) => onRetain(event.target.checked)} />保留上述职责，继续跨部门负责</label> : <p role="alert">需要有职责授权权限的管理员完成此次调岗，当前修改不会部分保存。</p>}
    </>}
    {removed.length > 0 && <p>组织、其他部门及上级继承的管理职责继续保留，可在“当前权限”查看来源。</p>}
  </section>;
}
