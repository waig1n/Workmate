"use client";

import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, setAuth } from "@/lib/api";
import type { User } from "@/lib/types";

/**
 * Full-screen gate rendered while must_change_password is set - the
 * workspace stays hidden until the password is changed (user management
 * plan §7; the backend independently rejects all other endpoints).
 */
export default function PasswordGate({ user }: { user: User }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);

  const change = useMutation({
    mutationFn: () => api.changePassword(currentPassword, newPassword),
    onSuccess: async (res) => {
      setAuth(res.access_token, res.user);
      await queryClient.invalidateQueries();
      router.replace("/workspace");
    },
    onError: (reason) => setError((reason as Error).message),
  });

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    if (newPassword.length < 8) {
      setError("新密码至少 8 位");
      return;
    }
    if (newPassword !== confirm) {
      setError("两次输入的新密码不一致");
      return;
    }
    change.mutate();
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-neutral-50 dark:bg-neutral-950">
      <form
        onSubmit={onSubmit}
        className="w-96 rounded-xl border border-neutral-200 bg-white p-8 shadow-sm dark:border-neutral-800 dark:bg-neutral-900"
      >
        <h1 className="mb-1 text-xl font-semibold">需要设置新密码</h1>
        <p className="mb-6 text-sm text-neutral-500">
          {user.email}，请先修改密码后继续使用工作台。
        </p>

        <label className="mb-1 block text-sm font-medium" htmlFor="current">
          当前密码
        </label>
        <input
          id="current"
          type="password"
          autoComplete="current-password"
          className="mb-4 w-full rounded-lg border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
          value={currentPassword}
          onChange={(event) => setCurrentPassword(event.target.value)}
          required
        />

        <label className="mb-1 block text-sm font-medium" htmlFor="new">
          新密码（至少 8 位）
        </label>
        <input
          id="new"
          type="password"
          autoComplete="new-password"
          className="mb-4 w-full rounded-lg border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
          value={newPassword}
          onChange={(event) => setNewPassword(event.target.value)}
          required
        />

        <label className="mb-1 block text-sm font-medium" htmlFor="confirm">
          确认新密码
        </label>
        <input
          id="confirm"
          type="password"
          autoComplete="new-password"
          className="mb-6 w-full rounded-lg border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
          required
        />

        {error && (
          <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600 dark:bg-red-950 dark:text-red-400">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={change.isPending}
          className="w-full rounded-lg bg-blue-600 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {change.isPending ? "提交中…" : "修改密码并继续"}
        </button>
      </form>
    </main>
  );
}
