"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Dialog } from "@/components/ui/Dialog";
import { api } from "@/lib/api";
import { adminErrorMessage } from "@/lib/errors";
import { permissionDisplayName } from "@/lib/permissionLabels";
import type { AdminPermission, AdminRole } from "@/lib/types";

export const roleInputClass = "mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--surface)] px-3 py-2 text-sm disabled:opacity-60";
export const roleButtonClass = "rounded-xl border border-[var(--line)] px-4 py-2 text-sm disabled:opacity-50";
const groups: [string, string[]][] = [
  ["用户账号", ["user"]], ["部门与成员", ["scope", "membership"]],
  ["智能体", ["agent"]], ["技能与连接", ["skill", "mcp"]],
  ["访问治理", ["role"]], ["审计", ["audit"]], ["用量与成本", ["usage"]],
];
export type RoleTemplate = { key: string; display_name: string; permission_keys: string[]; delegatable: boolean };

export function PermissionMatrix({ permissions, selected, readOnly, onChange }: {
  permissions: AdminPermission[]; selected: string[]; readOnly: boolean; onChange: (keys: string[]) => void;
}) {
  const [showDetails, setShowDetails] = useState(false);
  const known = groups.flatMap(([, prefixes]) => prefixes);
  const sections = [...groups.map(([name, prefixes]) => ({ name, items: permissions.filter((p) => prefixes.includes(p.key.split(".")[0])) })),
    { name: "扩展权限", items: permissions.filter((p) => !known.includes(p.key.split(".")[0])) }];
  return <div className="space-y-3"><div className="flex items-center justify-between gap-3"><span className="text-xs text-[var(--ink-soft)]">已选 {selected.length} 项</span><button type="button" aria-expanded={showDetails} onClick={() => setShowDetails(!showDetails)} className="text-xs text-[var(--ink-soft)] underline underline-offset-4">{showDetails ? "收起权限说明" : "显示权限说明"}</button></div>{sections.filter((g) => g.items.length).map((group) => <fieldset key={group.name} className="rounded-xl border border-[var(--line)] p-4">
    <legend className="px-1 text-sm font-semibold">{group.name}</legend><div className="grid gap-2 sm:grid-cols-2">{group.items.map((permission) => {
      const visibility = permission.key === "agent.view" || permission.key === "agent.use";
      const inactive = !visibility && permission.enforcement !== "rbac";
      return <label key={permission.id} className="flex items-start gap-3 rounded-lg bg-[var(--surface-subtle)] p-3 text-sm">
        <input type="checkbox" className="mt-1" checked={selected.includes(permission.key)} disabled={readOnly || visibility || inactive}
          onChange={() => onChange(selected.includes(permission.key) ? selected.filter((k) => k !== permission.key) : [...selected, permission.key])} />
        <span className="min-w-0"><span className="block font-medium">{permissionDisplayName(permission)}</span>
          {!showDetails && (visibility || inactive || permission.applicable_scope === "organization") && <span className="block text-xs text-[var(--ink-soft)]">{visibility ? "由可见范围决定" : inactive ? "尚未生效" : "仅限组织"}</span>}
          {showDetails && <><span className="mt-1 block text-xs leading-5 text-[var(--ink-soft)]">{visibility ? "由资源可见性规则决定，非独立开关" : inactive ? "未接入业务检查，仅保留已有配置" : permission.applicable_scope === "organization" ? "仅组织范围" : "在授权范围内生效"}</span>
          {permission.description && <span className="block text-xs leading-5 text-[var(--ink-soft)]">{permission.description}</span>}
          <code className="break-all text-xs text-[var(--ink-faint)]">{permission.key}</code></>}
        </span>
      </label>;
    })}</div></fieldset>)}</div>;
}

export function RoleEditor({ role, copy = false, forceReadOnly = false, templates = [], permissions, onClose, onDone }: {
  role: AdminRole | null; copy?: boolean; forceReadOnly?: boolean; templates?: RoleTemplate[]; permissions: AdminPermission[]; onClose: () => void; onDone: () => void;
}) {
  const readOnly = forceReadOnly || Boolean(role?.is_system && !copy);
  const [key, setKey] = useState("");
  const initialName = copy ? `${role?.display_name ?? ""}（副本）` : role?.display_name ?? "";
  const [name, setName] = useState(initialName);
  const [description, setDescription] = useState(role?.description ?? "");
  const [selected, setSelected] = useState<string[]>(role?.permission_keys ?? []);
  const [delegatable, setDelegatable] = useState(copy ? false : role?.delegatable ?? false);
  const [discard, setDiscard] = useState(false);
  const dirty = key !== "" || name !== initialName || description !== (role?.description ?? "") || delegatable !== (copy ? false : role?.delegatable ?? false) || JSON.stringify(selected) !== JSON.stringify(role?.permission_keys ?? []);
  const save = useMutation({ mutationFn: () => !role || copy
    ? api.post<AdminRole>("/admin/roles", { key, display_name: name.trim(), description, permission_keys: selected, delegatable })
    : api.patch<AdminRole>(`/admin/roles/${role.id}`, { display_name: name.trim(), description, permission_keys: selected, delegatable, expected_version: role.version }), onSuccess: onDone });
  const close = () => { if (save.isPending) return; if (dirty && !readOnly) setDiscard(true); else onClose(); };
  return <Dialog open onClose={close} title={copy ? "复制角色" : role ? `${readOnly ? "查看" : "编辑"} ${role.display_name}` : "创建角色"} width="max-w-[860px]"
    description={readOnly ? role?.is_system ? "系统角色由平台维护，可从列表复制为自定义角色。" : "你具有角色目录查看权限。修改角色需要组织角色管理权限。" : "修改将影响所有已分配此角色的用户。"}>
    <form onSubmit={(event) => { event.preventDefault(); if (!save.isPending && name.trim()) save.mutate(); }}>
      <div className="max-h-[60vh] space-y-4 overflow-y-auto pr-1">
        {!role && templates.length > 0 && <label className="block text-sm">从常用职责开始<select defaultValue="" disabled={save.isPending} className={roleInputClass} onChange={(event) => {
          const template = templates.find((item) => item.key === event.target.value);
          if (template) { setKey(template.key); setName(template.display_name); setSelected(template.permission_keys); setDelegatable(template.delegatable); }
        }}><option value="" disabled>选择角色模板（可选）</option>{templates.map((template) => <option key={template.key} value={template.key}>{template.display_name}</option>)}</select></label>}
        {role && !copy && <p className="rounded-xl bg-[var(--surface-subtle)] p-3 text-sm">已分配给 {role.user_count ?? "—"} 位用户 · {role.binding_count} 条授权</p>}
        {(!role || copy) && <label className="block text-sm">角色标识<input required pattern={"[a-z][a-z0-9_.:\\-]{1,63}"} placeholder="例如 finance_editor" className={roleInputClass} value={key} onChange={(event) => setKey(event.target.value)} /></label>}
        <div className="grid gap-4 sm:grid-cols-2"><label className="block text-sm">角色名称<input required maxLength={120} disabled={readOnly || save.isPending} className={roleInputClass} value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label className="block text-sm">用途说明<textarea rows={1} disabled={readOnly || save.isPending} className={roleInputClass} value={description} onChange={(event) => setDescription(event.target.value)} /></label></div>
        <label className="flex items-start gap-3 rounded-xl bg-[var(--surface-subtle)] p-3 text-sm"><input type="checkbox" className="mt-1" disabled={readOnly || save.isPending} checked={delegatable} onChange={(event) => setDelegatable(event.target.checked)} /><span>允许部门管理员分配</span></label>
        <details className="text-xs leading-6 text-[var(--ink-soft)]"><summary className="cursor-pointer">授权规则</summary><ul className="mt-2 list-disc space-y-1 pl-4"><li>多个角色的权限共同生效，取消此处权限不一定撤销用户的最终权限。</li><li>部门管理员只能在管理范围和自身可授予权限内分配角色；含组织专属权限的角色不可在部门分配。</li><li>智能体的查看与使用取决于归属、成员关系和发布范围。管理权限不授予他人个人空间访问权。</li></ul></details>
        <h3 className="text-sm font-semibold">角色权限</h3>
        <PermissionMatrix permissions={permissions} selected={selected} readOnly={readOnly || save.isPending} onChange={setSelected} />
      </div>
      {save.error && <p role="alert" className="mt-3 rounded-xl bg-red-50 p-3 text-sm text-red-700">{adminErrorMessage(save.error)}{(save.error as { code?: string }).code === "version_conflict" && " 当前草稿已保留；请关闭后刷新列表，重新打开角色核对最新配置。"}</p>}
      {discard && <div role="alert" className="mt-3 rounded-xl border border-amber-300 p-3 text-sm"><p>有未保存的修改。是否放弃草稿？</p><div className="mt-2 flex gap-2"><button type="button" className={roleButtonClass} onClick={() => setDiscard(false)}>继续编辑</button><button type="button" className={roleButtonClass} onClick={onClose}>放弃修改</button></div></div>}
      <div className="mt-5 flex justify-end gap-3"><button type="button" className={roleButtonClass} disabled={save.isPending} onClick={close}>{readOnly ? "关闭详情" : "取消"}</button>
        {!readOnly && <button className={`${roleButtonClass} bg-[var(--ink)] text-[var(--surface)]`} disabled={save.isPending || !name.trim()}>{save.isPending ? "保存中…" : "保存角色"}</button>}</div>
    </form>
  </Dialog>;
}
