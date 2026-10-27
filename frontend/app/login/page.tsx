"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, setAuth, type ApiError } from "@/lib/api";
import { adminErrorMessage } from "@/lib/errors";
import { Icon } from "@/components/ui/Icons";

export default function LoginPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [organizationName, setOrganizationName] = useState("");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Hidden rather than disabled: a closed registration policy must not
  // advertise an entry that the backend would reject (plan §3.1).
  const registrationQuery = useQuery({
    queryKey: ["auth", "registration-status"],
    queryFn: () => api.registrationStatus(),
    retry: false,
  });
  const registrationAvailable = registrationQuery.isSuccess && !registrationQuery.isError && (registrationQuery.data?.registration_available ?? false);
  const bootstrap = registrationAvailable && registrationQuery.data?.bootstrap_required === true;
  const registering = bootstrap || (mode === "register" && registrationAvailable);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (registrationQuery.isLoading || (bootstrap && (!organizationName.trim() || !displayName.trim()))) return;
    setBusy(true);
    setError(null);
    try {
      const res =
        !registering
          ? await api.login(email, password)
          : bootstrap ? await api.register(email, password, displayName.trim(), organizationName.trim()) : await api.register(email, password, displayName.trim() || email);
      queryClient.clear();
      setAuth(res.access_token, res.user);
      router.push("/workspace");
    } catch (err) {
      if (bootstrap && (err as ApiError).status === 409 && (err as ApiError).code === "organization_already_initialized") {
        setMode("login");
        setError("工作空间已由其他管理员创建，请登录或联系管理员开通账号。");
        await registrationQuery.refetch();
      } else setError(err instanceof Error ? adminErrorMessage(err) : "请求失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="product-shell product-noise relative flex min-h-screen overflow-hidden">
      <section className="relative hidden flex-[1.25] flex-col justify-between overflow-hidden p-12 lg:flex xl:p-16">
        <div className="product-grid absolute inset-0 opacity-70" />
        <div className="absolute -right-28 -top-32 h-[480px] w-[480px] rounded-full bg-[var(--accent-soft)] blur-3xl" />
        <div className="relative flex items-center gap-3"><span className="grid h-11 w-11 place-items-center rounded-[14px] bg-[var(--ink)] text-[13px] font-bold tracking-[-.08em] text-[var(--surface)] shadow-sm">W</span><div><p className="text-[14px] font-semibold">Workmate</p><p className="mt-0.5 text-[11px] text-[var(--ink-faint)]">Enterprise Agent Workspace</p></div></div>
        <div className="relative max-w-[680px]"><span className="mb-7 inline-flex items-center gap-2 rounded-full border border-[var(--line)] bg-[color-mix(in_srgb,var(--surface)_78%,transparent)] px-3 py-1.5 text-[11px] font-medium text-[var(--ink-soft)] shadow-sm backdrop-blur"><span className="h-1.5 w-1.5 rounded-full bg-[var(--positive)] status-pulse" /> Workmate Runtime 已就绪</span><h2 className="text-[46px] font-semibold leading-[1.1] tracking-[-.05em] xl:text-[56px]">把复杂工作，<br/><span className="text-[var(--accent)]">交给可信任的智能体。</span></h2><p className="mt-6 max-w-xl text-[15px] leading-7 text-[var(--ink-soft)]">从目标到交付，统一管理执行过程、人工审批与工作成果。每一次行动都有边界，每一个结果都可追溯。</p><div className="mt-10 flex flex-wrap gap-x-8 gap-y-4">{[["shield","策略约束"],["agents","智能体协作"],["file","成果追踪"]].map(([icon,label]) => <div key={label} className="flex items-center gap-2.5 text-[12px] font-semibold text-[var(--ink-soft)]"><span className="grid h-9 w-9 place-items-center rounded-xl bg-[var(--surface)] shadow-sm"><Icon name={icon as "shield" | "agents" | "file"} className="h-4 w-4 text-[var(--accent)]"/></span>{label}</div>)}</div></div>
        <p className="relative text-[11px] text-[var(--ink-faint)]">Private by design · Governed by your organization</p>
      </section>
      <section className="flex w-full items-center justify-center border-l border-[var(--line)] bg-[var(--surface)] p-6 lg:w-[500px] xl:w-[560px]">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-[340px]"
      >
        <div className="mb-9 flex items-center gap-3 lg:hidden"><span className="grid h-10 w-10 place-items-center rounded-[13px] bg-[var(--ink)] text-sm font-bold text-[var(--surface)]">W</span><span className="font-semibold">Workmate</span></div>
        <p className="product-eyebrow mb-2">Secure workspace</p><h1 className="text-[30px] font-semibold tracking-[-.04em]">{bootstrap ? "创建企业工作空间" : registering ? "创建企业账号" : "欢迎回来"}</h1>
        <p className="mb-9 mt-2 text-[13px] leading-5 text-[var(--ink-soft)]">
          {bootstrap ? "设置组织名称，并创建首位管理员账号。" : registering ? "创建账号，加入现有企业工作空间。" : "登录后继续你的智能体任务。"}
        </p>

        {registrationQuery.isError && <p role="alert" className="mb-4 text-sm text-red-600">无法读取注册状态。<button type="button" className="ml-2 underline" onClick={() => void registrationQuery.refetch()}>重试</button></p>}
        {registrationQuery.isLoading && <p role="status" className="mb-4 text-sm">正在检查工作空间状态…</p>}
        {bootstrap && <label className="mb-4 block text-sm font-medium">组织名称<input className="product-control mt-2 w-full px-3.5 text-sm" required maxLength={200} value={organizationName} onChange={(event) => setOrganizationName(event.target.value)} /></label>}
        <label className="mb-1 block text-sm font-medium" htmlFor="email">
          邮箱
        </label>
        <input
          id="email"
          type="email"
          className="product-control mb-5 mt-2 w-full px-3.5 text-sm outline-none"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          required
        />

        {registering && (
          <>
            <label className="mb-1 block text-sm font-medium" htmlFor="displayName">
              {bootstrap ? "管理员姓名" : "显示名称"}
            </label>
            <input
              id="displayName"
              required={bootstrap}
              maxLength={100}
              className="product-control mb-5 mt-2 w-full px-3.5 text-sm outline-none"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </>
        )}

        <label className="mb-1 block text-sm font-medium" htmlFor="password">
          密码
        </label>
        <input
          id="password"
          minLength={registering ? 8 : undefined}
          type="password"
          className="product-control mb-6 mt-2 w-full px-3.5 text-sm outline-none"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={registering ? "new-password" : "current-password"}
          required
        />

        {error && (
          <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600 dark:bg-red-950 dark:text-red-400">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy || registrationQuery.isLoading || (bootstrap && (!organizationName.trim() || !displayName.trim()))}
          className="flex h-12 w-full items-center justify-center gap-2 rounded-[14px] bg-[var(--ink)] text-[13px] font-semibold text-[var(--surface)] shadow-sm hover:-translate-y-0.5 hover:shadow-md disabled:opacity-50"
        >
          {busy ? "请稍候…" : bootstrap ? "创建工作空间" : !registering ? <>进入工作区 <Icon name="arrow" className="h-3.5 w-3.5" /></> : "注册并登录"}
        </button>

        {registrationAvailable && !bootstrap && (
          <button
            type="button"
            className="mt-5 w-full text-xs text-neutral-500 hover:text-neutral-700 dark:hover:text-neutral-300"
            onClick={() => {
              setMode(mode === "login" ? "register" : "login");
              setError(null);
            }}
          >
            {mode === "login" ? "没有账号？注册" : "已有账号？登录"}
          </button>
        )}
        <div className="mt-9 flex items-center justify-center gap-2 border-t border-[var(--line)] pt-5 text-[10px] text-[var(--ink-faint)]"><Icon name="lock" className="h-3 w-3" />连接受企业安全策略保护</div>
      </form>
      </section>
    </main>
  );
}
