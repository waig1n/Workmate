"use client";

import { useState } from "react";
import type { AdminRole, AdminRoleBinding, AdminScopeNode } from "@/lib/types";
import { adminErrorMessage } from "@/lib/errors";

export const fieldClass = "mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--surface)] px-3 py-2.5 text-sm";
export const buttonClass = "rounded-xl border border-[var(--line)] px-4 py-2 text-sm disabled:opacity-50";
export const primaryClass = `${buttonClass} bg-[var(--ink)] text-[var(--surface)]`;
export const flattenScopes = (nodes: AdminScopeNode[], prefix = ""): AdminScopeNode[] => nodes.flatMap((node) => {
  const name = prefix ? `${prefix} / ${node.name}` : node.name;
  return [{ ...node, name }, ...flattenScopes(node.children, name)];
});

export function FormError({ error, retry }: { error: unknown; retry?: () => void }) {
  if (!error) return null;
  return <div role="alert" className="my-3 rounded-xl bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">{adminErrorMessage(error)}{retry && <button className="ml-3 underline" onClick={retry} type="button">重试</button>}</div>;
}

export function AccessFields({ scopes, roles, memberships, bindings, onMemberships, onBindings, disabled = false, creation = false, canManageMemberships = true, canBindRoles = true, roleNames = {} }: {
  scopes: AdminScopeNode[]; roles: AdminRole[]; memberships: string[]; bindings: AdminRoleBinding[];
  onMemberships: (value: string[]) => void; onBindings: (value: AdminRoleBinding[]) => void; disabled?: boolean; creation?: boolean; canManageMemberships?: boolean; canBindRoles?: boolean; roleNames?: Record<string, string>;
}) {
  const [departmentSearch, setDepartmentSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [departmentLimit, setDepartmentLimit] = useState(20);
  const [selectedScope, setScope] = useState("");
  const [selectedRole, setRole] = useState("");
  const [descendants, setDescendants] = useState(false);
  const chosenDepartments = scopes.filter((scope) => scope.kind === "department" && memberships.includes(scope.id));
  const scopeId = selectedScope || (chosenDepartments.length === 1 ? chosenDepartments[0].id : "");
  const rootOnlyPermissions = ["user.read", "user.manage", "role.read", "role.manage"];
  const isDepartment = scopes.find((scope) => scope.id === scopeId)?.kind === "department";
  const managementRoles = roles.filter((role) => role.key !== "member");
  const candidates = isDepartment
    ? managementRoles.filter((role) => role.key !== "admin" && !role.permission_keys?.some((key) => rootOnlyPermissions.includes(key)))
    : managementRoles;
  const roleKey = candidates.some((role) => role.key === selectedRole) ? selectedRole : candidates[0]?.key || "";
  const duplicate = bindings.some((binding) => binding.scope_id === scopeId && binding.role_key === roleKey);
  const departments = scopes.filter((scope) => scope.kind === "department");
  const matches = departments.filter((scope) => scope.name.toLocaleLowerCase().includes(departmentSearch.trim().toLocaleLowerCase()));
  const selectedDepartments = memberships.filter((id) => !scopes.some((scope) => scope.id === id && scope.kind !== "department"));
  const membershipCheckbox = (scope: AdminScopeNode) => <label key={scope.id} className="flex items-start gap-3 rounded-lg p-2 text-sm hover:bg-[var(--surface-subtle)]"><input className="mt-1" type="checkbox" checked={memberships.includes(scope.id)} onChange={(event) => onMemberships(event.target.checked ? [...memberships, scope.id] : memberships.filter((id) => id !== scope.id))} />{scope.name}</label>;
  return <fieldset disabled={disabled} className="space-y-5 disabled:opacity-60">
    <p className="text-sm text-[var(--ink-soft)]">企业成员 · 可使用全组织及所属部门开放的资源</p>
    <fieldset disabled={!canManageMemberships} className="rounded-xl border border-[var(--line)] p-4"><legend className="font-semibold">所属部门</legend>
      <div className="mt-3 rounded-lg bg-[var(--surface-subtle)] p-3"><p className="text-sm font-medium">已选部门（{selectedDepartments.length}）</p><div className="mt-2 flex flex-wrap gap-2">{selectedDepartments.length ? selectedDepartments.map((id) => <span key={id} className="rounded-lg border border-[var(--line)] px-2 py-1 text-sm">{scopes.find((scope) => scope.id === id)?.name ?? "未加载的部门"}<button type="button" aria-label={`取消部门 ${scopes.find((scope) => scope.id === id)?.name ?? id}`} className="ml-2 text-[var(--ink-soft)]" onClick={() => onMemberships(memberships.filter((item) => item !== id))}>×</button></span>) : <p className="text-sm text-[var(--ink-soft)]">未选择部门</p>}</div></div>
      <label className="mt-3 block text-sm">搜索部门<input className={fieldClass} placeholder="输入部门名称或路径" value={departmentSearch} onChange={(event) => { setDepartmentSearch(event.target.value); setDepartmentLimit(20); }} /></label>
      <div className="mt-2 max-h-48 overflow-y-auto">{matches.slice(0, departmentLimit).map(membershipCheckbox)}{!matches.length && <p className="p-3 text-sm text-[var(--ink-soft)]">没有匹配的部门</p>}</div>
      {matches.length > departmentLimit && <button type="button" className={`${buttonClass} mt-2`} onClick={() => setDepartmentLimit((limit) => limit + 20)}>显示更多部门（还有 {matches.length - departmentLimit} 个）</button>}
    </fieldset>
    {creation && <p role="status" className="text-sm text-[var(--ink-soft)]">{chosenDepartments.length ? `加入 ${chosenDepartments.map((scope) => scope.name).join("、")}` : "企业成员，暂不分配部门"}；{bindings.length ? `另有 ${bindings.length} 项管理职责，请核对生效范围。` : "作为普通成员，不授予管理权限。"}</p>}
    <fieldset disabled={!canBindRoles}><details open={creation ? undefined : true} className="rounded-xl border border-[var(--line)] p-4"><summary className="cursor-pointer font-semibold">{creation ? `管理职责（可选）${bindings.length ? ` · ${bindings.length} 项` : ""}` : "管理职责（可选）"}</summary>
      <div className="mt-3 space-y-2">{bindings.length === 0 && <p className="rounded-lg bg-[var(--surface-subtle)] p-3 text-sm text-[var(--ink-soft)]">普通成员，无额外管理职责</p>}{bindings.map((binding, index) => <div key={`${binding.scope_id}:${binding.role_key}`} className="space-y-2 rounded-xl bg-[var(--surface-subtle)] p-3 text-sm">
        <div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="font-medium">{roles.find((role) => role.key === binding.role_key)?.display_name ?? roleNames[binding.role_key] ?? binding.role_key}</p><p className="mt-1 text-[var(--ink-soft)]">生效范围：{scopes.find((scope) => scope.id === binding.scope_id)?.name ?? "未加载的范围"}</p></div><button type="button" className="shrink-0 text-red-600" onClick={() => onBindings(bindings.filter((_, at) => at !== index))}>移除授权</button></div>
        <label className="flex items-center gap-2"><input type="checkbox" checked={binding.include_descendants} onChange={(event) => onBindings(bindings.map((item, at) => at === index ? { ...item, include_descendants: event.target.checked } : item))} />管理范围包含下级部门</label>
        <p className="text-xs text-[var(--ink-soft)]">此人员的上述管理职责同时适用于全部下级部门，包括未来新增部门。</p>
      </div>)}</div>
      {canBindRoles && (!adding ? <button type="button" className={`${buttonClass} mt-3`} disabled={!scopes.length || !roles.length} onClick={() => { setScope(""); setRole(""); setDescendants(false); setAdding(true); }}>新增管理职责</button> : <div className="mt-4 space-y-3 rounded-xl bg-[var(--surface-subtle)] p-4"><h4 className="text-sm font-semibold">新增授权 · 尚未保存</h4>
        <div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">生效范围<select aria-label="生效范围" className={fieldClass} value={scopeId} onChange={(event) => setScope(event.target.value)}><option value="">请选择生效范围</option>{scopes.map((scope) => <option key={scope.id} value={scope.id}>{scope.name}</option>)}</select></label><label className="text-sm">角色<select aria-label="角色" className={fieldClass} value={roleKey} onChange={(event) => setRole(event.target.value)}>{candidates.map((role) => <option key={role.id} value={role.key}>{role.display_name}</option>)}</select></label></div>
        {!candidates.length && <p className="text-sm text-[var(--ink-soft)]">此范围没有可分配的角色。</p>}
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={descendants} onChange={(event) => setDescendants(event.target.checked)} />管理范围包含下级部门</label><p className="text-xs text-[var(--ink-soft)]">此人员的上述管理职责同时适用于全部下级部门，包括未来新增部门。</p><p className="text-sm text-[var(--ink-soft)]">加入列表后统一保存。</p>
        <div className="flex flex-wrap justify-end gap-2"><button type="button" className={buttonClass} onClick={() => { setAdding(false); setDescendants(false); }}>取消添加</button><button type="button" className={buttonClass} disabled={!scopeId || !roleKey || duplicate} onClick={() => { onBindings([...bindings, { scope_id: scopeId, role_key: roleKey, include_descendants: descendants }]); setAdding(false); setDescendants(false); }}>{duplicate ? "列表中已有该授权" : "加入待保存列表"}</button></div>
      </div>)}
    </details></fieldset>
  </fieldset>;
}
