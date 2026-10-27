"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, setAuth } from "@/lib/api";
import { adminErrorMessage } from "@/lib/errors";
import { usePermissions } from "@/lib/usePermissions";
import { notifyAdministrationChanged } from "@/lib/administrationEvents";
import type { Scope, User } from "@/lib/types";

/** Self-service settings: display name and password (plan §5.1). */
export default function SettingsPage() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const scopes = useQuery({ queryKey: ["scopes"], queryFn: () => api.get<Scope[]>("/scopes/accessible") });
  const canManageOrganization = !permissions.isError && !scopes.isError && Boolean(scopes.data?.some((scope) => scope.kind === "organization" && scope.id === permissions.data?.root_scope_id && scope.permissions.includes("scope.manage")));
  const me = useQuery({
    queryKey: ["auth", "me"],
    queryFn: () => api.me(),
  });

  const [displayName, setDisplayName] = useState("");
  const [profileMessage, setProfileMessage] = useState<string | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [passwordMessage, setPasswordMessage] = useState<string | null>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);

  useEffect(() => {
    if (me.data) setDisplayName(me.data.display_name);
  }, [me.data]);

  const updateProfile = useMutation({
    mutationFn: () => api.updateProfile(displayName.trim()),
    onSuccess: async (user) => {
      setProfileError(null);
      setProfileMessage("显示名称已更新");
      await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
      const token = typeof window !== "undefined"
        ? window.localStorage.getItem("workmate_token")
        : null;
      if (token) setAuth(token, user);
    },
    onError: (reason) => {
      setProfileMessage(null);
      setProfileError((reason as Error).message);
    },
  });

  const changePassword = useMutation({
    mutationFn: () => api.changePassword(currentPassword, newPassword),
    onSuccess: async (res) => {
      setPasswordError(null);
      setPasswordMessage("密码已修改，其他设备需要重新登录");
      setCurrentPassword("");
      setNewPassword("");
      setConfirm("");
      setAuth(res.access_token, res.user);
      await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
    },
    onError: (reason) => {
      setPasswordMessage(null);
      setPasswordError((reason as Error).message);
    },
  });

  function onProfileSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (displayName.trim()) updateProfile.mutate();
  }

  function onPasswordSubmit(event: React.FormEvent) {
    event.preventDefault();
    setPasswordError(null);
    if (newPassword.length < 8) {
      setPasswordError("新密码至少 8 位");
      return;
    }
    if (newPassword !== confirm) {
      setPasswordError("两次输入的新密码不一致");
      return;
    }
    changePassword.mutate();
  }

  const user: User | undefined = me.data;

  return (
    <div className="h-full overflow-y-auto bg-[var(--surface)]">
      <div className="mx-auto max-w-3xl space-y-6 p-5 pb-28 sm:p-8 lg:p-10">
        <div><p className="product-eyebrow">Personal preferences</p><h1 className="mt-2 text-[30px] font-semibold tracking-[-.04em]">账户设置</h1><p className="mt-2 text-[13px] text-[var(--ink-soft)]">管理个人资料与登录密码。</p></div>

        {canManageOrganization && <OrganizationSettingsForm />}

        <section className="rounded-xl border border-neutral-200 p-6 dark:border-neutral-800">
          <h2 className="text-sm font-semibold">个人资料</h2>
          <p className="mt-1 text-xs text-neutral-500">
            {user?.email}
            {user?.status === "disabled" && "（账号已禁用）"}
          </p>
          <form className="mt-4 space-y-3" onSubmit={onProfileSubmit}>
            <label className="block text-sm">
              显示名称
              <input
                className="mt-1 w-full rounded-lg border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                maxLength={100}
                required
              />
            </label>
            {profileError && (
              <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600 dark:bg-red-950 dark:text-red-400">
                {profileError}
              </p>
            )}
            {profileMessage && (
              <p className="rounded-lg bg-green-50 px-3 py-2 text-sm text-green-700 dark:bg-green-950 dark:text-green-400">
                {profileMessage}
              </p>
            )}
            <button
              type="submit"
              disabled={updateProfile.isPending || !displayName.trim()}
              className="rounded-lg bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {updateProfile.isPending ? "保存中…" : "保存"}
            </button>
          </form>
        </section>

        <section className="rounded-xl border border-neutral-200 p-6 dark:border-neutral-800">
          <h2 className="text-sm font-semibold">修改密码</h2>
          <p className="mt-1 text-xs text-neutral-500">
            修改后当前其他设备的登录状态会立即失效。
          </p>
          <form className="mt-4 space-y-3" onSubmit={onPasswordSubmit}>
            <label className="block text-sm">
              当前密码
              <input
                type="password"
                autoComplete="current-password"
                className="mt-1 w-full rounded-lg border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                required
              />
            </label>
            <label className="block text-sm">
              新密码（至少 8 位）
              <input
                type="password"
                autoComplete="new-password"
                className="mt-1 w-full rounded-lg border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                required
              />
            </label>
            <label className="block text-sm">
              确认新密码
              <input
                type="password"
                autoComplete="new-password"
                className="mt-1 w-full rounded-lg border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                required
              />
            </label>
            {passwordError && (
              <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600 dark:bg-red-950 dark:text-red-400">
                {passwordError}
              </p>
            )}
            {passwordMessage && (
              <p className="rounded-lg bg-green-50 px-3 py-2 text-sm text-green-700 dark:bg-green-950 dark:text-green-400">
                {passwordMessage}
              </p>
            )}
            <button
              type="submit"
              disabled={changePassword.isPending}
              className="rounded-lg bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {changePassword.isPending ? "提交中…" : "修改密码"}
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}


function OrganizationSettingsForm() {
  const client = useQueryClient();
  const organization = useQuery({ queryKey: ["admin-organization"], queryFn: () => api.organization(), retry: false });
  const [name, setName] = useState("");
  const [version, setVersion] = useState<number | null>(null);
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (organization.data && !dirty) { setName(organization.data.name); setVersion(organization.data.tree_version); }
  }, [organization.data, dirty]);
  const save = useMutation({
    mutationFn: () => api.renameOrganization(name.trim(), version!),
    onSuccess: async (updated) => {
      client.setQueryData(["admin-organization"], updated);
      setName(updated.name); setVersion(updated.tree_version); setDirty(false); setMessage("组织名称已更新");
      await Promise.all(["scopes", "admin-scopes", "admin-scope-tree", "agents", "skills", "mcps", "knowledge-bases", "sharing"].map((key) => client.invalidateQueries({ queryKey: [key] })));
      notifyAdministrationChanged();
    },
  });
  async function reload() {
    const result = await organization.refetch();
    if (result.data && !result.isError) {
      setVersion(result.data.tree_version);
      if (!dirty) setName(result.data.name);
      save.reset();
      setMessage(dirty ? `当前组织名称：${result.data.name}，草稿已保留，请核对后保存。` : "已读取最新组织信息");
    }
  }
  return <section className="rounded-xl border border-neutral-200 p-6 dark:border-neutral-800">
    <h2 className="text-sm font-semibold">组织设置</h2>
    <p className="mt-1 text-xs text-neutral-500">基本信息 · 更新名称不会改变成员、部门或授权。</p>
    {organization.isLoading ? <p className="mt-4 text-sm">正在读取组织信息…</p> : <form className="mt-4 space-y-3" onSubmit={(event) => { event.preventDefault(); if (name.trim() && version !== null && !organization.isError) save.mutate(); }}>
      <label className="block text-sm">组织名称<input className="product-control mt-1 w-full px-3 text-sm" value={name} maxLength={200} required disabled={save.isPending || organization.isFetching || organization.isError} onChange={(event) => { setName(event.target.value); setDirty(true); setMessage(""); }} /></label>
      {(organization.error || save.error) && <p role="alert" className="text-sm text-red-600">{adminErrorMessage(organization.error || save.error)}</p>}
      {message && <p role="status" className="text-sm text-green-700">{message}</p>}
      <div className="flex gap-3"><button className="product-control px-3 text-sm" type="submit" disabled={save.isPending || organization.isFetching || organization.isError || !name.trim() || !dirty || version === null}>保存组织名称</button><button type="button" className="text-sm underline" disabled={save.isPending || organization.isFetching} onClick={() => void reload()}>刷新最新版本</button></div>
    </form>}
  </section>;
}
