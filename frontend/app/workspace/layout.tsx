"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import PasswordGate from "@/components/PasswordGate";
import { Button, cx } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Icon, type IconName } from "@/components/ui/Icons";
import { PanelResizeHandle } from "@/components/ui/PanelResizeHandle";
import { api, clearAuth } from "@/lib/api";
import { adminNavigation, can } from "@/lib/permissions";
import { usePersistentPanelWidth } from "@/lib/usePersistentPanelWidth";
import { usePermissions } from "@/lib/usePermissions";
import { WorkspaceDraftProvider, useWorkspaceDraft } from "@/components/workspace/WorkspaceDraftProvider";
import { ResourceNavigation, useResourceDestination, matchesDestination } from "@/components/workspace/ResourceNavigation";
import type { Conversation } from "@/lib/types";

type NavItem = { href: string; label: string; description: string; icon: IconName };

const NAV_DEFAULT_WIDTH = 260;
const NAV_MIN_WIDTH = 236;
const NAV_MAX_WIDTH = 360;

function relativeTime(value: string) {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  return days < 7 ? `${days} 天前` : new Date(value).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export default function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  return <WorkspaceDraftProvider><WorkspaceShell>{children}</WorkspaceShell></WorkspaceDraftProvider>;
}

function WorkspaceShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  const searchRef = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState("");
  const [showManagement, setShowManagement] = useState(false);
  const resource = useResourceDestination(pathname);
  const [showAgentPicker, setShowAgentPicker] = useState(false);
  const workspace = useWorkspaceDraft();
  const { usableAgents, selectedAgent, sending, selectAgent } = workspace;
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const navigationPanel = usePersistentPanelWidth({
    storageKey: "workmate:workspace-navigation-width",
    defaultWidth: NAV_DEFAULT_WIDTH,
    minWidth: NAV_MIN_WIDTH,
    maxWidth: NAV_MAX_WIDTH,
  });

  const me = useQuery({ queryKey: ["auth", "me"], queryFn: () => api.me(), staleTime: 30_000, retry: false });
  const permissions = usePermissions();
  const convs = useQuery({ queryKey: ["conversations", "recent"], queryFn: () => api.get<Conversation[]>("/conversations?limit=8") });
  const activeConversationId = pathname.match(/^\/workspace\/c\/([^/]+)/)?.[1];
  const activeConversation = useQuery({
    queryKey: ["conversation", activeConversationId],
    queryFn: () => api.get<Conversation>(`/conversations/${activeConversationId}`),
    enabled: Boolean(activeConversationId),
  });
  const displayedAgentId = activeConversationId ? activeConversation.data?.agent_id ?? "" : selectedAgent?.id ?? "";
  const displayedAgentName = activeConversationId ? activeConversation.data?.agent?.display_name ?? "历史智能体" : selectedAgent?.display_name ?? "选择智能体";
  function switchAgent(id: string) {
    if (sending) return;
    selectAgent(id);
    setShowAgentPicker(false);
    router.push("/workspace");
  }
  function agentPicker(label = "当前智能体") {
    return <div className="mb-3">
      <label className="flex items-center gap-2 rounded-xl border border-[var(--line)] bg-[var(--surface-raised)] px-3 py-2 text-[var(--ink-soft)]"><Icon name="agents" className="h-4 w-4 shrink-0" />
        <select aria-label={label} value={displayedAgentId} disabled={workspace.agentsLoading || sending || !usableAgents.length || Boolean(activeConversationId && activeConversation.isLoading)} onChange={(event) => switchAgent(event.target.value)} className="h-8 min-w-0 flex-1 bg-transparent text-sm outline-none">
          {!usableAgents.length && <option value="">{workspace.agentsLoading ? "加载中…" : workspace.agentsError ? "加载失败" : "暂无可用智能体"}</option>}
          {!activeConversationId && !selectedAgent && usableAgents.length > 0 && <option value="" disabled>请选择可用智能体</option>}
          {activeConversationId && !usableAgents.some((agent) => agent.id === displayedAgentId) && <option value={displayedAgentId}>{displayedAgentName} · 仅查看</option>}
          {usableAgents.map((agent) => <option key={agent.id} value={agent.id}>{agent.display_name}{agent.is_default ? " · 默认" : ""}</option>)}
        </select>
      </label>
      {workspace.agentsError && <button type="button" onClick={() => workspace.retryAgents()} className="mt-2 text-xs text-[var(--danger)]">智能体加载失败，重试</button>}
      {activeConversationId && <p className="mt-1.5 px-2 text-[11px] text-[var(--ink-faint)]">切换智能体将打开新对话草稿</p>}
    </div>;
  }

  const filtered = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    if (!keyword) return convs.data ?? [];
    return (convs.data ?? []).filter((conv) => `${conv.title} ${conv.agent?.display_name ?? ""}`.toLowerCase().includes(keyword));
  }, [convs.data, search]);

  function openCreate() {
    if (sending) return;
    if (activeConversation.data && usableAgents.some((agent) => agent.id === activeConversation.data.agent_id)) selectAgent(activeConversation.data.agent_id);
    router.push("/workspace");
  }

  useEffect(() => {
    function onShortcut(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchRef.current?.focus();
      }
    }
    function onCreate(event: Event) {
      openCreate();
    }
    window.addEventListener("keydown", onShortcut);
    window.addEventListener("workmate:create", onCreate);
    return () => {
      window.removeEventListener("keydown", onShortcut);
      window.removeEventListener("workmate:create", onCreate);
    };
  });

  const rename = useMutation({
    mutationFn: ({ id, nextTitle }: { id: string; nextTitle: string }) => api.patch<Conversation>(`/conversations/${id}`, { title: nextTitle }),
    onSuccess: async () => { setEditingId(null); await queryClient.invalidateQueries({ queryKey: ["conversations"] }); },
    onError: (reason) => setError((reason as Error).message),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/conversations/${id}`),
    onSuccess: async (_, id) => { setDeleteTarget(null); await queryClient.invalidateQueries({ queryKey: ["conversations"] }); if (pathname === `/workspace/c/${id}`) router.push("/workspace"); },
    onError: (reason) => setError((reason as Error).message),
  });

  const user = me.data;
  const adminNav = adminNavigation(permissions.data);
  const primaryNav: NavItem[] = [
    { href: "/workspace/tasks", label: "历史对话", description: "全部会话", icon: "tasks" },
    { href: "/workspace/channels", label: "IM 渠道", description: "机器人与身份绑定", icon: "users" },
    { href: "/workspace/schedules", label: "定时任务", description: "计划与执行记录", icon: "clock" },
    { href: resource.href, label: "资源中心", description: "智能体与能力", icon: "archive" },
    { href: "/workspace/usage", label: "用量与成本", description: "使用分析", icon: "grid" },
  ];
  const governanceNav: NavItem[] = [
    ...(adminNav.users ? [{ href: "/workspace/users", label: "用户管理", description: "账号与访问", icon: "users" as IconName }] : []),
    ...(adminNav.roles ? [{ href: "/workspace/roles", label: "角色权限", description: "访问策略", icon: "shield" as IconName }] : []),
    ...(adminNav.scopes ? [{ href: "/workspace/scopes", label: "部门管理", description: "成员与范围", icon: "briefcase" as IconName }] : []),
    ...(can(permissions.data, "audit.read") ? [{ href: "/workspace/audit", label: "管理审计", description: "变更记录", icon: "shield" as IconName }] : []),
  ];
  const navActive = (href: string) => href === "/workspace" ? pathname === href : matchesDestination(pathname, href);

  const managementActive = governanceNav.some((item) => pathname === item.href || pathname.startsWith(`${item.href}/`));

  if (me.data?.must_change_password) return <PasswordGate user={me.data} />;

  return (
    <div className="product-shell flex h-screen overflow-hidden text-[var(--ink)]">
      <aside style={{ "--navigation-panel-width": `${navigationPanel.width}px` } as React.CSSProperties} className="relative hidden min-h-0 w-[var(--navigation-panel-width)] shrink-0 flex-col border-r border-[var(--line)] bg-[color-mix(in_srgb,var(--surface)_88%,transparent)] px-3 py-4 backdrop-blur-xl lg:flex">
        <PanelResizeHandle edge="right" width={navigationPanel.width} minWidth={NAV_MIN_WIDTH} maxWidth={NAV_MAX_WIDTH} label="调整导航栏宽度" onResize={navigationPanel.resizeBy} onReset={navigationPanel.reset} className="hidden lg:flex" />
        <div className="flex shrink-0 items-center gap-3 px-2 pb-4">
          <Link href="/workspace" aria-label="Workmate" className="grid h-10 w-10 place-items-center rounded-[13px] bg-[var(--ink)] text-[13px] font-bold tracking-[-.06em] text-[var(--surface)] shadow-sm">W</Link>
          <div className="min-w-0 flex-1"><p className="text-[13px] font-semibold tracking-[-.01em]">Workmate</p><p className="mt-0.5 text-[11px] text-[var(--ink-faint)]">Enterprise workspace</p></div>
          <span className="h-2 w-2 rounded-full bg-[var(--positive)] status-pulse" title="服务正常" />
        </div>

        {managementActive ? <div className="flex min-h-0 flex-1 flex-col">
          <Link href="/workspace" className="mb-5 rounded-xl px-3 py-3 text-sm text-[var(--ink-soft)] hover:bg-[var(--surface-subtle)]">返回工作台</Link>
          <p className="px-3 pb-3 text-xs font-semibold text-[var(--ink-faint)]">管理中心</p>
          <nav aria-label="管理中心导航" className="min-h-0 flex-1 space-y-1 overflow-y-auto">{governanceNav.map(item => <Link key={item.href} href={item.href} aria-current={navActive(item.href) ? "page" : undefined} className={cx("flex h-11 items-center gap-3 rounded-xl px-3 text-sm", navActive(item.href) ? "bg-[var(--accent-soft)] text-[var(--accent-strong)]" : "text-[var(--ink-soft)] hover:bg-[var(--surface-subtle)]")}><Icon name={item.icon} className="h-4 w-4" />{item.label}</Link>)}</nav>
        </div> : <>
        <div className="shrink-0">{agentPicker()}</div>

        <Button aria-label="新建对话" onClick={() => openCreate()} disabled={sending} className="mb-3 w-full shrink-0 justify-between px-3.5" size="md">
          <span className="flex items-center gap-2"><Icon name="plus" className="h-4 w-4" />新建对话</span><span className="text-[10px] opacity-55">N</span>
        </Button>

        <nav aria-label="主导航" className="shrink-0 space-y-1">
          {primaryNav.filter(item => item.href !== "/workspace/usage").map((item) => (
            <Link key={item.href} href={item.href} aria-label={item.label} aria-current={navActive(item.href) ? "page" : undefined} className={cx("flex h-11 items-center gap-3 rounded-[13px] px-3", navActive(item.href) ? "bg-[var(--surface-raised)] text-[var(--ink)] shadow-sm ring-1 ring-[var(--line)]" : "text-[var(--ink-soft)] hover:bg-[var(--surface-subtle)] hover:text-[var(--ink)]")}>
              <Icon name={item.icon} className={cx("h-[17px] w-[17px]", navActive(item.href) && "text-[var(--accent)]")} />
              <span className="flex-1 text-[13px] font-medium">{item.label}</span>
              <span className="text-[10px] text-[var(--ink-faint)]">{item.description}</span>
            </Link>
          ))}
        </nav>

        <div className="my-3 h-px shrink-0 bg-[var(--line)]" />
        <label className="product-control flex h-10 shrink-0 min-h-0 items-center gap-2 px-3 text-[var(--ink-faint)]">
          <Icon name="search" className="h-4 w-4" />
          <input ref={searchRef} className="min-w-0 flex-1 bg-transparent text-[12px] text-[var(--ink)] outline-none placeholder:text-[var(--ink-faint)]" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索最近对话" />
          <kbd className="rounded-md border border-[var(--line)] bg-[var(--surface-subtle)] px-1.5 py-0.5 text-[9px]">⌘K</kbd>
        </label>
        <div className="mt-3 flex shrink-0 items-center justify-between px-2"><p className="text-[10px] font-semibold uppercase tracking-[.13em] text-[var(--ink-faint)]">最近对话</p></div>
        <nav aria-label="最近会话" className="mt-2 min-h-0 flex-1 overflow-y-auto">
          {convs.isLoading && <div className="space-y-2 p-1">{[1,2,3].map((item) => <div key={item} className="h-14 animate-pulse rounded-xl bg-[var(--surface-subtle)]" />)}</div>}
          <ul className="space-y-1">
            {filtered.map((conv) => {
              const active = pathname === `/workspace/c/${conv.id}`;
              const editing = editingId === conv.id;
              return (
                <li key={conv.id} className={cx("group rounded-[13px]", active ? "bg-[var(--accent-soft)]" : "hover:bg-[var(--surface-subtle)]")}>
                  {editing ? (
                    <form className="p-2" onSubmit={(event) => { event.preventDefault(); const nextTitle = editingTitle.trim(); if (nextTitle) rename.mutate({ id: conv.id, nextTitle }); }}>
                      <input autoFocus className="product-control h-9 min-h-0 w-full px-2.5 text-xs outline-none" value={editingTitle} onChange={(event) => setEditingTitle(event.target.value)} onBlur={() => setEditingId(null)} />
                    </form>
                  ) : (
                    <div className="flex items-center px-1.5 py-1.5">
                      <Link href={`/workspace/c/${conv.id}`} className="min-w-0 flex-1 px-2 py-1.5" title={conv.title}>
                        <p className={cx("truncate text-[12px] font-medium", active && "text-[var(--accent-strong)]")}>{conv.title || "未命名对话"}</p>
                        <p className="mt-1 truncate text-[10px] text-[var(--ink-faint)]">{conv.agent?.display_name ?? "只读会话"} · {relativeTime(conv.updated_at)}</p>
                      </Link>
                      <div className="flex opacity-0 group-focus-within:opacity-100 group-hover:opacity-100">
                        <button aria-label={`重命名 ${conv.title}`} className="grid h-7 w-7 place-items-center rounded-lg text-[var(--ink-faint)] hover:bg-[var(--surface-raised)] hover:text-[var(--ink)]" onClick={() => { setEditingId(conv.id); setEditingTitle(conv.title); }}>✎</button>
                        <button aria-label={`删除 ${conv.title}`} className="grid h-7 w-7 place-items-center rounded-lg text-[var(--ink-faint)] hover:bg-red-50 hover:text-red-600" onClick={() => setDeleteTarget(conv)}>×</button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
          {convs.isError && <button onClick={() => convs.refetch()} className="p-3 text-sm text-[var(--danger)]">最近对话加载失败，点击重试</button>}
          {search && <Link href={`/workspace/tasks?q=${encodeURIComponent(search.trim())}`} className="block p-3 text-xs text-[var(--accent)]">搜索全部历史对话 →</Link>}
          {!convs.isLoading && !convs.isError && !filtered.length && <div className="mx-1 mt-5 rounded-xl border border-dashed border-[var(--line-strong)] px-4 py-6 text-center text-[12px] text-[var(--ink-faint)]">{search ? "没有匹配的会话" : "还没有会话"}</div>}
        </nav>

        <nav aria-label="工作台工具" className="shrink-0 space-y-1 border-t border-[var(--line)] py-2">
          <Link href="/workspace/usage" aria-current={navActive("/workspace/usage") ? "page" : undefined} className={cx("flex h-9 items-center gap-2.5 rounded-xl px-2.5 text-xs", navActive("/workspace/usage") ? "bg-[var(--accent-soft)] font-medium text-[var(--accent-strong)]" : "text-[var(--ink-soft)] hover:bg-[var(--surface-subtle)]")}><Icon name="grid" className="h-4 w-4" />用量与成本</Link>
          {governanceNav.length > 0 && <Link href={governanceNav[0].href} className="flex h-9 items-center gap-2.5 rounded-xl px-2.5 text-xs text-[var(--ink-soft)] hover:bg-[var(--surface-subtle)]"><Icon name="settings" className="h-4 w-4" />管理中心</Link>}
        </nav>
        </>}
        <div className="flex shrink-0 items-center gap-3 border-t border-[var(--line)] pt-3">
          <div className="grid h-9 w-9 place-items-center rounded-full bg-[var(--accent-soft)] text-[12px] font-bold text-[var(--accent-strong)]">{(user?.display_name ?? user?.email ?? "U").slice(0, 1).toUpperCase()}</div>
          <div className="min-w-0 flex-1"><p className="truncate text-[12px] font-medium">{user?.display_name ?? "企业成员"}</p><p className="truncate text-[10px] text-[var(--ink-faint)]">{user?.email ?? ""}</p></div>
          <Link href="/workspace/settings" aria-label="设置" className="grid h-8 w-8 place-items-center rounded-lg text-[var(--ink-faint)] hover:bg-[var(--surface-subtle)] hover:text-[var(--ink)]"><Icon name="settings" className="h-4 w-4" /></Link>
          <button onClick={() => { clearAuth(); queryClient.clear(); router.push("/login"); }} className="text-[11px] text-[var(--ink-faint)] hover:text-[var(--danger)]">退出</button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between border-b border-[var(--line)] bg-[var(--surface)] px-4 lg:hidden">
          <Link href="/workspace" aria-label="Workmate" className="flex items-center gap-2.5"><span className="grid h-8 w-8 place-items-center rounded-[11px] bg-[var(--ink)] text-[11px] font-bold text-[var(--surface)]">W</span></Link><button type="button" aria-label="切换智能体" onClick={() => setShowAgentPicker(true)} className="mx-2 min-w-0 flex-1 truncate text-left text-sm font-medium">{displayedAgentName} ▾</button>
          <Button aria-label="新建对话" size="sm" disabled={sending} onClick={() => openCreate()}><Icon name="plus" className="h-4 w-4" />新建</Button>
        </header>
        {error && <p role="alert" className="px-5 py-2 text-sm text-[var(--danger)]">{error}</p>}
        <main className="m-0 flex flex-col min-h-0 min-w-0 flex-1 overflow-hidden bg-[var(--surface)] lg:m-2 lg:ml-0 lg:rounded-[22px] lg:border lg:border-[var(--line)] lg:shadow-sm">{resource.active && <ResourceNavigation pathname={pathname} />}<div className="min-h-0 flex-1 overflow-hidden">{children}</div></main>
      </div>

      <nav className="fixed inset-x-3 bottom-3 z-40 flex h-16 items-center justify-around rounded-[20px] border border-[var(--line)] bg-[color-mix(in_srgb,var(--surface)_92%,transparent)] px-2 shadow-[var(--shadow-md)] backdrop-blur-xl lg:hidden">
        {[{ href: "/workspace", label: "对话", icon: "grid" as IconName }, ...primaryNav].map((item) => <Link key={item.href} href={item.href} aria-label={item.label} className={cx("flex h-11 min-w-14 flex-col items-center justify-center gap-0.5 rounded-xl text-[9px]", navActive(item.href) ? "bg-[var(--accent-soft)] text-[var(--accent-strong)]" : "text-[var(--ink-faint)]")}><Icon name={item.icon} className="h-[18px] w-[18px]" />{item.label}</Link>)}
        {governanceNav.length > 0 && <button aria-label="管理中心" aria-expanded={showManagement} className="flex h-11 min-w-14 flex-col items-center justify-center gap-1 rounded-xl text-xs text-[var(--ink-soft)]" onClick={() => setShowManagement(true)}><Icon name="shield" className="h-[18px] w-[18px]" />管理中心</button>}
      </nav>

      <Dialog open={showAgentPicker} onClose={() => setShowAgentPicker(false)} title="选择智能体">{agentPicker("当前智能体（移动端）")}</Dialog>

      <Dialog open={showManagement} onClose={() => setShowManagement(false)} title="管理中心"><nav aria-label="管理导航" className="space-y-2">{governanceNav.map((item) => <Link key={item.href} href={item.href} aria-current={navActive(item.href) ? "page" : undefined} onClick={() => setShowManagement(false)} className="flex items-center gap-3 rounded-xl border border-[var(--line)] p-4 text-sm aria-[current=page]:bg-[var(--accent-soft)] aria-[current=page]:text-[var(--accent-strong)]"><Icon name={item.icon} className="h-5 w-5" />{item.label}</Link>)}</nav></Dialog>

      <Dialog open={Boolean(deleteTarget)} onClose={() => setDeleteTarget(null)} title="删除这项工作？" description={`“${deleteTarget?.title ?? ""}”的历史消息和成果将一并删除，此操作无法撤销。`} footer={<><Button variant="ghost" onClick={() => setDeleteTarget(null)}>取消</Button><Button variant="danger" onClick={() => deleteTarget && remove.mutate(deleteTarget.id)} disabled={remove.isPending}>{remove.isPending ? "正在删除…" : "确认删除"}</Button></>}>
        <div className="rounded-[14px] border border-red-100 bg-red-50/70 p-3.5 text-[12px] leading-5 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">如需保留成果，请先进入会话下载相关文件。</div>
      </Dialog>
    </div>
  );
}
