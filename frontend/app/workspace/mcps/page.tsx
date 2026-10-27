"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { MCPConfiguration } from "@/components/mcps/MCPConfiguration";
import { ResourceSharing } from "@/components/sharing/ResourceSharing";
import { ResourceAccessSources } from "@/components/sharing/ResourceAccessSources";
import { Dialog } from "@/components/ui/Dialog";
import { Icon } from "@/components/ui/Icons";
import { ResourceToolbar } from "@/components/ui/ResourceToolbar";
import { ResourceCard, resourceDrawerWidth, resourceGridClass } from "@/components/ui/ResourceCard";
import { ResourcePagination, CatalogPagination } from "@/components/ui/ResourcePagination";
import { useResourcePage, useCatalogCursor } from "@/lib/useResourcePage";
import { api } from "@/lib/api";
import { canManageCapability } from "@/lib/permissions";
import type { InstalledMCP, MCPCard, MCPCatalogPage, Scope, SkillHub } from "@/lib/types";

const sourceLabel = (item: InstalledMCP) => item.metadata.hub_id === "workmate-custom-http" ? "自定义连接" : "服务库连接";
const actionClass = "rounded-xl bg-[var(--ink)] px-4 py-2 text-sm text-[var(--surface)] disabled:opacity-50";

export default function MCPsPage() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [editor, setEditor] = useState<{ id?: string; scopeId: string } | null>(null);
  const [selected, setSelected] = useState<InstalledMCP | null>(null);
  const [pendingRemove, setPendingRemove] = useState(false);
  const [saved, setSaved] = useState(false);
  const [hubId, setHubId] = useState("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [configCard, setConfigCard] = useState<MCPCard | null>(null);
  const [alias, setAlias] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [installedSearch, setInstalledSearch] = useState("");
  const [source, setSource] = useState("");
  const installed = useResourcePage<InstalledMCP>("mcps", { q: installedSearch.trim(), ...(source ? { source } : {}) });
  const scopes = useQuery({ queryKey: ["scopes"], queryFn: () => api.get<Scope[]>("/scopes/accessible") });
  const targetScopeId = (scopes.data ?? []).find(scope => scope.kind === "organization" && scope.permissions.includes("mcp.manage"))?.id ?? "";
  const canInstall = Boolean(targetScopeId);
  const catalogCursor = useCatalogCursor(`${hubId}:${query}:${targetScopeId}`);
  const hubs = useQuery({ queryKey: ["mcp-hubs"], queryFn: () => api.get<SkillHub[]>("/mcps/hubs"), enabled: canInstall && adding && catalogOpen });
  const catalog = useQuery({
    queryKey: ["mcp-catalog", hubId, query, targetScopeId, catalogCursor.cursor],
    queryFn: () => api.get<MCPCatalogPage>(`/mcps/catalog?hub_id=${encodeURIComponent(hubId)}&q=${encodeURIComponent(query)}&scope_id=${encodeURIComponent(targetScopeId)}${catalogCursor.cursor ? `&cursor=${encodeURIComponent(catalogCursor.cursor)}` : ""}`),
    enabled: canInstall && adding && catalogOpen && Boolean(hubId),
  });
  useEffect(() => { if (!hubId && hubs.data?.[0]) setHubId(hubs.data[0].hub_id); }, [hubId, hubs.data]);
  const [recentlyConfigured, setRecentlyConfigured] = useState<Set<string>>(new Set());
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["mcps"] });
    void queryClient.invalidateQueries({ queryKey: ["agents"] });
    void queryClient.invalidateQueries({ queryKey: ["agent-capability-candidates"] });
  };
  function onSaved(item: InstalledMCP, options?: { keepOpen?: boolean }) {
    setSelected(item); setPendingRemove(false); setSaved(true); setError(null);
    installed.setPage(1); setInstalledSearch(""); invalidate();
    if (!options?.keepOpen) { setEditor(null); setAdding(false); }
  }
  function closeCatalogConfiguration() { setConfigCard(null); setValues({}); setAlias(""); setError(null); }
  const install = useMutation({
    mutationFn: () => api.post<InstalledMCP>("/mcps", { hub_id: configCard!.hub_id, card_id: configCard!.id, name: alias.trim() || undefined, values, scope_id: targetScopeId }),
    onSuccess: item => {
      if (configCard) setRecentlyConfigured(current => new Set(current).add(`${configCard.hub_id}:${configCard.id}`));
      void queryClient.invalidateQueries({ queryKey: ["mcp-catalog"] });
      closeCatalogConfiguration(); onSaved(item);
    },
    onError: () => setError("保存失败，请检查配置和管理权限后重试。"),
  });
  const toggle = useMutation({
    mutationFn: (item: InstalledMCP) => api.patch<InstalledMCP>(`/mcps/${item.id}`, { enabled: !item.enabled }),
    onSuccess: item => { setSelected(item); setError(null); invalidate(); },
    onError: () => setError("操作失败，请检查管理权限及智能体绑定后重试。"),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/mcps/${id}`),
    onSuccess: () => { setSelected(null); setPendingRemove(false); setRecentlyConfigured(new Set()); void queryClient.invalidateQueries({ queryKey: ["mcp-catalog"] }); invalidate(); },
    onError: () => setError("删除失败，请先解除智能体绑定并检查管理权限。"),
  });
  const scopeName = (id: string) => (scopes.data ?? []).find(scope => scope.id === id)?.name ?? "组织连接";
  return <div className="h-full overflow-y-auto bg-[var(--surface)] p-5 pb-28 sm:px-6 lg:pb-6"><div className="mx-auto max-w-[1180px]">
    <ResourceToolbar variant="compact" label="连接来源" options={[{ value: "", label: "全部来源" }, { value: "custom", label: "自定义连接" }, { value: "catalog", label: "服务库连接" }]} value={source} onChange={setSource}
      searchLabel="搜索已配置连接" placeholder="搜索连接名称或用途" search={installedSearch} onSearch={setInstalledSearch}
      actions={canInstall && <button className={`flex h-10 items-center gap-2 ${actionClass}`} onClick={() => { setAdding(true); setCatalogOpen(false); setError(null); }}><Icon name="plus" className="h-4 w-4" />添加 MCP</button>} />
    {scopes.isError && <p role="alert" className="mb-4 text-sm text-red-600">管理权限加载失败。<button onClick={() => void scopes.refetch()}>重试</button></p>}
    {installed.isError ? <p role="alert" className="py-12 text-center text-sm">连接加载失败。<button onClick={() => void installed.refetch()}>重试加载</button></p> : installed.isLoading ? <p role="status" className="py-12 text-center text-sm">正在加载连接…</p> : <div className={resourceGridClass}>
      {installed.items.filter(item => item.scope_kind !== "personal").map(item => <ResourceCard key={item.id} icon="tool" title={item.display_name || item.name} description={item.metadata.description || "打开查看连接配置与使用范围。"}
        metadata={`${sourceLabel(item)} · ${scopeName(item.scope_id)}${item.enabled ? "" : " · 已停用"}`}
        onClick={() => { setSelected(item); setSaved(false); setPendingRemove(false); setError(null); }} />)}
      {!installed.items.length && <div className="col-span-full py-20 text-center"><Icon name="tool" className="mx-auto mb-4 h-8 w-8 text-[var(--ink-faint)]" /><p className="font-medium">{installedSearch ? "没有匹配的连接" : "还没有可用连接"}</p><p className="mt-2 text-sm text-[var(--ink-soft)]">{installedSearch ? "试试其他关键词。" : "管理员添加并共享后，可在智能体配置中选用。"}</p></div>}
    </div>}
    <ResourcePagination page={installed.page} totalPages={installed.totalPages} total={installed.total} busy={installed.isFetching} onPage={installed.setPage} />
    <Dialog open={Boolean(selected) && !editor} onClose={() => { if (!toggle.isPending && !remove.isPending) setSelected(null); }} title={selected?.display_name || selected?.name || "连接详情"} description={selected ? sourceLabel(selected) : undefined} side width={resourceDrawerWidth}>
      {selected && <div className="space-y-5">
        {saved && <p role="status" className="rounded-xl bg-[var(--surface-subtle)] p-4 text-sm">连接已保存。请确认共享范围，再由有权限的成员在智能体中选用。</p>}
        <p className="text-sm leading-7 text-[var(--ink-soft)]">{selected.metadata.description || "暂无说明"}</p>
        <p className="text-sm text-[var(--ink-soft)]">管理范围：{scopeName(selected.scope_id)} · {canManageCapability(selected) ? "你可以管理此连接" : "你没有连接管理权限"}</p>
        <p className="text-sm">{selected.enabled ? "已启用" : "已停用"} · {selected.usable ? "你已获得使用授权" : "你尚未获得使用授权"}</p>
        <p className="text-xs text-[var(--ink-soft)]">共享复用受管连接，不复制或转移凭据；使用权不授予查看连接参数与密钥的权限。</p>
        <ResourceAccessSources kind="mcps" resourceId={selected.id} />
        {canManageCapability(selected) && <>
          <button className={actionClass} onClick={() => setEditor({ id: selected.id, scopeId: selected.scope_id })}>编辑配置与测试连接</button>
          <ResourceSharing key={selected.id} kind="mcps" resourceId={selected.id} />
          <div className="space-y-4 border-t border-[var(--line)] pt-5">
            <button disabled={toggle.isPending || remove.isPending} className="text-sm" onClick={() => toggle.mutate(selected)}>{selected.enabled ? "停用连接" : "启用连接"}</button>
            {pendingRemove ? <div className="space-y-3 text-sm"><p>删除后将无法继续使用此连接。若仍被智能体引用，请先解除绑定。确认删除？</p><button disabled={remove.isPending} className="text-red-600" onClick={() => remove.mutate(selected.id)}>确认删除</button><button disabled={remove.isPending} className="ml-4" onClick={() => setPendingRemove(false)}>取消</button></div> : <button className="ml-5 text-sm text-red-600" onClick={() => setPendingRemove(true)}>删除连接</button>}
          </div>
        </>}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      </div>}
    </Dialog>
    <Dialog open={adding && !editor && !configCard} onClose={() => setAdding(false)} title="添加 MCP" description="添加到组织连接库，再按需要共享给部门或全组织。" side width={resourceDrawerWidth}>
      <div className="space-y-5">
        <div className="grid gap-3 sm:grid-cols-2">
          <button className="rounded-xl border border-[var(--line)] p-4 text-left" onClick={() => { setAdding(false); setSelected(null); setEditor({ scopeId: targetScopeId }); }}><span className="font-medium">自定义连接</span><span className="mt-2 block text-sm text-[var(--ink-soft)]">填写 HTTP 地址与认证，或导入 JSON 配置。</span></button>
          <button className="rounded-xl border border-[var(--line)] p-4 text-left" aria-pressed={catalogOpen} onClick={() => setCatalogOpen(true)}><span className="font-medium">从 MCP 服务库添加</span><span className="mt-2 block text-sm text-[var(--ink-soft)]">浏览 MCP 服务库，选择服务并配置连接。</span></button>
        </div>
        {catalogOpen && <section className="space-y-4">
          <div className="flex flex-wrap gap-2"><select aria-label="MCP 服务库" className="product-control px-3 text-sm" value={hubId} onChange={event => setHubId(event.target.value)}>{(hubs.data ?? []).map(hub => <option key={hub.hub_id} value={hub.hub_id}>{hub.display_name}</option>)}</select>
            <form className="flex min-w-0 flex-1 gap-2" onSubmit={event => { event.preventDefault(); setQuery(search.trim()); }}><input aria-label="搜索 MCP 服务" className="product-control min-w-0 flex-1 px-3 text-sm" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索 MCP 服务" /><button className={actionClass}>搜索</button></form>
          </div>
          {hubs.isError || catalog.isError ? <p role="alert" className="text-sm">MCP 服务库暂时无法访问。<button onClick={() => { void hubs.refetch(); void catalog.refetch(); }}>重新加载</button></p> : hubs.isLoading || catalog.isLoading ? <p role="status">正在加载 MCP 服务库…</p> : !hubs.data?.length ? <p className="py-8 text-center text-sm text-[var(--ink-soft)]">暂无可用的 MCP 服务库，可添加自定义连接。</p> : <div className="grid gap-3 sm:grid-cols-2">
            {(catalog.data?.cards ?? []).map(card => {
              const present = card.installed || recentlyConfigured.has(`${card.hub_id}:${card.id}`);
              return <article key={`${card.hub_id}:${card.id}`} className="rounded-xl border border-[var(--line)] p-4"><h3 className="font-medium">{card.display_name || card.name}</h3><p className="mt-2 line-clamp-3 text-sm text-[var(--ink-soft)]">{card.description || "暂无说明"}</p><button disabled={present} className={`mt-4 ${actionClass}`} onClick={() => { setConfigCard(card); setAlias(""); setValues({}); setError(null); }}>{present ? "已添加" : "配置连接"}</button></article>;
            })}
            {!catalog.data?.cards.length && <p className="col-span-full py-8 text-center text-sm">没有匹配的连接。</p>}
          </div>}
          <CatalogPagination page={catalogCursor.page} nextCursor={catalog.data?.next_cursor} busy={catalog.isFetching} onPrevious={catalogCursor.previous} onNext={catalogCursor.next} />
        </section>}
      </div>
    </Dialog>
    {editor && <MCPConfiguration key={editor.id ?? "new"} {...editor} onClose={() => setEditor(null)} onSaved={onSaved} />}
    <Dialog open={Boolean(configCard)} onClose={() => { if (!install.isPending) closeCatalogConfiguration(); }} title={`配置 ${configCard?.display_name || configCard?.name || "服务库连接"}`} side width={resourceDrawerWidth}>
      {configCard && <form className="space-y-4" onSubmit={event => { event.preventDefault(); install.mutate(); }}>
        <p className="text-sm text-[var(--ink-soft)]">保存到组织连接库。敏感字段提交后不会返回浏览器。</p>
        <label className="block text-sm">连接名称（可选）<input className="product-control mt-1 w-full px-3" placeholder={configCard.name} value={alias} onChange={event => setAlias(event.target.value)} /></label>
        {Object.entries(configCard.inputs_schema?.properties ?? {}).map(([key, spec]) => <label key={key} className="block text-sm">{spec.title || key}{configCard.inputs_schema.required?.includes(key) ? " *" : ""}<input required={configCard.inputs_schema.required?.includes(key)} type={spec.writeOnly || spec.format === "password" ? "password" : "text"} autoComplete="off" className="product-control mt-1 w-full px-3" value={values[key] ?? ""} onChange={event => setValues({ ...values, [key]: event.target.value })} /><span className="mt-1 block text-xs text-[var(--ink-faint)]">{spec.description}</span></label>)}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex justify-end gap-3"><button type="button" disabled={install.isPending} onClick={closeCatalogConfiguration}>返回服务库</button><button disabled={install.isPending || !targetScopeId} className={actionClass}>{install.isPending ? "保存中…" : "保存连接"}</button></div>
      </form>}
    </Dialog>
  </div></div>;
}
