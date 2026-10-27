"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Dialog } from "@/components/ui/Dialog";
import { api } from "@/lib/api";
import { adminErrorMessage } from "@/lib/errors";
import { permissionDisplayName } from "@/lib/permissionLabels";
import type { AdminPermission } from "@/lib/types";
import { roleButtonClass, roleInputClass } from "./roles-editor";

export function PermissionDirectory({ permissions, canManage, onDone }: { permissions: AdminPermission[]; canManage: boolean; onDone: () => Promise<unknown> }) {
  const [editing, setEditing] = useState<{ permission: AdminPermission | null } | null>(null);
  const [deleting, setDeleting] = useState<AdminPermission | null>(null);
  const remove = useMutation({ mutationFn: (id: string) => api.delete(`/admin/permissions/${id}`), onSuccess: async () => { setDeleting(null); await onDone(); } });
  return <details className="mt-8 rounded-2xl border border-[var(--line)] p-5"><summary className="cursor-pointer text-sm font-semibold">高级权限目录（{permissions.length}）</summary>
    <p className="mt-3 text-sm leading-6 text-[var(--ink-soft)]">新增权限记录不会自动产生业务能力。未接入的扩展权限仅保留定义，不作为已生效的权限开关。</p>
    {canManage && <button className={`${roleButtonClass} mt-3`} onClick={() => setEditing({ permission: null })}>新增扩展权限</button>}
    <div className="mt-4 space-y-3">{permissions.map((permission) => <article key={permission.id} className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-[var(--surface-subtle)] p-3 text-sm"><div><p className="font-medium">{permissionDisplayName(permission)}</p><code className="text-xs text-[var(--ink-soft)]">{permission.key}</code><p className="mt-1 text-xs text-[var(--ink-soft)]">{permission.enforcement === "visibility" || ["agent.view", "agent.use"].includes(permission.key) ? "资源可见性规则，非独立开关" : permission.enforcement === "rbac" ? "已接入业务权限检查" : "尚未接入业务检查"} · {permission.applicable_scope === "organization" ? "仅组织范围" : "授权范围"}</p></div>{permission.is_system ? <span className="text-xs text-[var(--ink-faint)]">系统定义</span> : canManage && <div className="flex gap-2"><button className={roleButtonClass} onClick={() => setEditing({ permission })}>编辑</button><button className={`${roleButtonClass} text-red-600`} onClick={() => { remove.reset(); setDeleting(permission); }}>删除</button></div>}</article>)}</div>
    {editing && <PermissionEditor key={editing.permission?.id ?? "new"} permission={editing.permission} onClose={() => setEditing(null)} onDone={async () => { setEditing(null); await onDone(); }} />}
    {deleting && <Dialog open title={`删除权限 ${deleting.display_name}`} onClose={() => { if (!remove.isPending) setDeleting(null); }}><p className="text-sm">仍被角色引用的权限不能删除；请先检查角色配置。</p>{remove.error && <p role="alert" className="mt-3 text-sm text-red-600">{adminErrorMessage(remove.error)}</p>}<div className="mt-4 flex justify-end gap-3"><button className={roleButtonClass} disabled={remove.isPending} onClick={() => setDeleting(null)}>取消</button><button className={`${roleButtonClass} text-red-600`} disabled={remove.isPending} onClick={() => remove.mutate(deleting.id)}>{remove.isPending ? "删除中…" : "确认删除"}</button></div></Dialog>}
  </details>;
}

function PermissionEditor({ permission, onClose, onDone }: { permission: AdminPermission | null; onClose: () => void; onDone: () => Promise<void> }) {
  const [key, setKey] = useState("");
  const [name, setName] = useState(permission?.display_name ?? "");
  const [description, setDescription] = useState(permission?.description ?? "");
  const [discard, setDiscard] = useState(false);
  const dirty = key !== "" || name !== (permission?.display_name ?? "") || description !== (permission?.description ?? "");
  const save = useMutation({ mutationFn: () => permission ? api.patch(`/admin/permissions/${permission.id}`, { display_name: name.trim(), description }) : api.post("/admin/permissions", { key, display_name: name.trim(), description }), onSuccess: onDone });
  const close = () => { if (save.isPending) return; if (dirty) setDiscard(true); else onClose(); };
  return <Dialog open title={permission ? "编辑扩展权限" : "新增扩展权限"} onClose={close} description="权限标识创建后不可修改；需要后端业务检查才能真正生效。"><form onSubmit={(event) => { event.preventDefault(); if (!save.isPending && name.trim()) save.mutate(); }}>
    <div className="max-h-[55vh] space-y-4 overflow-y-auto">{!permission && <label className="block text-sm">权限标识<input required pattern="[a-z][a-z0-9_.:-]{1,63}" className={roleInputClass} value={key} onChange={(event) => setKey(event.target.value)} /></label>}
      <label className="block text-sm">权限名称<input required className={roleInputClass} disabled={save.isPending} value={name} onChange={(event) => setName(event.target.value)} /></label><label className="block text-sm">说明<textarea className={roleInputClass} disabled={save.isPending} value={description} onChange={(event) => setDescription(event.target.value)} /></label></div>
    {save.error && <p role="alert" className="mt-3 text-sm text-red-600">{adminErrorMessage(save.error)}</p>}
    {discard && <div role="alert" className="mt-3 text-sm">有未保存的修改。<button type="button" className={roleButtonClass} onClick={() => setDiscard(false)}>继续编辑</button><button type="button" className={roleButtonClass} onClick={onClose}>放弃修改</button></div>}
    <div className="mt-5 flex justify-end gap-3"><button type="button" className={roleButtonClass} disabled={save.isPending} onClick={close}>取消</button><button className={roleButtonClass} disabled={save.isPending || !name.trim()}>{save.isPending ? "保存中…" : "保存权限"}</button></div>
  </form></Dialog>;
}
