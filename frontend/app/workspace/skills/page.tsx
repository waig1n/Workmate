"use client";
import { ResourceToolbar } from "@/components/ui/ResourceToolbar";
import { ResourcePagination, CatalogPagination } from "@/components/ui/ResourcePagination";
import { useResourcePage, useCatalogCursor } from "@/lib/useResourcePage";
import { ResourceCard, resourceDrawerWidth, resourceGridClass } from "@/components/ui/ResourceCard";

import { ResourceAccessSources } from "@/components/sharing/ResourceAccessSources";
import { ResourceSharing } from "@/components/sharing/ResourceSharing";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import { SkillDescription } from "@/components/SkillDescription";
import { Icon } from "@/components/ui/Icons";
import { Dialog } from "@/components/ui/Dialog";
import { canManageCapability } from "@/lib/permissions";
import type { InstalledSkill, Scope, SkillCard, SkillCatalogPage, SkillHub } from "@/lib/types";
import { usePermissions } from "@/lib/usePermissions";

const skillSource = (skill: InstalledSkill) => skill.metadata.hub_id === "uploaded" ? "uploaded" : skill.metadata.hub_id === "workmate-builtin" ? "builtin" : "third-party";
const sourceLabels: Record<string, string> = { uploaded: "自建技能", builtin: "平台内置", "third-party": "第三方来源" };

const creationChannelLabels: Record<string, string> = { upload: "文件上传", assistant: "平台助手创建" };

const sourceDescription = (skill: InstalledSkill) => {
  const origin = skill.metadata.central_library_origin?.scope_name;
  const channel = skillSource(skill) === "uploaded" && typeof skill.metadata.creation_channel === "string"
    ? creationChannelLabels[skill.metadata.creation_channel]
    : undefined;
  return `${sourceLabels[skillSource(skill)]}${channel ? ` · ${channel}` : ""}${origin ? ` · 历史来源：${origin}` : ""}`;
};

export default function SkillsPage() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [adding, setAdding] = useState(false);
  const [selectedSnapshot, setSelected] = useState<InstalledSkill | null>(null);
  const [pendingRemove, setPendingRemove] = useState(false);
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [hubId, setHubId] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [pendingInstall, setPendingInstall] = useState<SkillCard | null>(null);

  const installed = useResourcePage<InstalledSkill>("skills", { q: search.trim(), source: sourceFilter || "all" });
  const currentSelected = installed.items.find((skill) => skill.id === selectedSnapshot?.id);
  const selected = useMemo(() => selectedSnapshot && currentSelected
    ? { ...selectedSnapshot, manageable: currentSelected.manageable, usable: currentSelected.usable }
    : selectedSnapshot, [selectedSnapshot, currentSelected]);
  const catalogCursor = useCatalogCursor(`${hubId}:${query}`);
  const scopes = useQuery({ queryKey: ["scopes"], queryFn: () => api.get<Scope[]>("/scopes/accessible") });
  const canInstall = (scopes.data ?? []).some((scope) => scope.kind === "organization" && scope.permissions.includes("skill.manage"));
  const hubs = useQuery({
    queryKey: ["skill-hubs"],
    queryFn: () => api.get<SkillHub[]>("/skills/hubs"),
    enabled: canInstall,
  });
  const importHubs = useMemo(() => (hubs.data ?? []).filter((hub) => hub.hub_id !== "uploaded" && hub.hub_id !== "workmate-builtin"), [hubs.data]);
  const catalog = useQuery({
    queryKey: ["skill-catalog", hubId, query, catalogCursor.cursor],
    queryFn: () => api.get<SkillCatalogPage>(`/skills/catalog?hub_id=${encodeURIComponent(hubId)}&q=${encodeURIComponent(query)}${catalogCursor.cursor ? `&cursor=${encodeURIComponent(catalogCursor.cursor)}` : ""}`),
    enabled: canInstall && Boolean(hubId),
  });

  useEffect(() => {
    if (!importHubs.some((hub) => hub.hub_id === hubId)) setHubId(importHubs[0]?.hub_id ?? "");
  }, [hubId, importHubs]);

  const organizationSkills = installed.items.filter((skill) => skill.scope_kind !== "personal");
  const [recentlyImported, setRecentlyImported] = useState<Set<string>>(new Set());

  const install = useMutation({
    mutationFn: (card: SkillCard) => {
      return api.post<InstalledSkill>("/skills", { hub_id: card.hub_id, card_id: card.id, confirmed: true });
    },
    onSuccess: (skill, card) => {
      setRecentlyImported((current) => new Set(current).add(`${card.hub_id}:${card.id}`));
      queryClient.invalidateQueries({ queryKey: ["skill-catalog"] });
      installed.setPage(1);
      setSourceFilter(skillSource(skill));
      setSearch("");
      setSelected(skill);
      setPendingRemove(false);
      setPendingInstall(null);
      setAdding(false);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (reason) => setError((reason as Error).message),
  });
  const toggleEnabled = useMutation({
    mutationFn: (skill: InstalledSkill) => api.patch<InstalledSkill>(`/skills/${skill.id}`, { enabled: !skill.enabled }),
    onSuccess: (skill) => {
      setSelected(skill);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["skills"] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      queryClient.invalidateQueries({ queryKey: ["agent-capability-candidates"] });
    },
    onError: (reason) => setError((reason as Error).message),
  });
  const uninstall = useMutation({
    mutationFn: (id: string) => api.delete(`/skills/${id}`),
    onSuccess: () => {
      setError(null);
      setRecentlyImported(new Set());
      queryClient.invalidateQueries({ queryKey: ["skill-catalog"] });
      setSelected(null); setPendingRemove(false);
      queryClient.invalidateQueries({ queryKey: ["skills"] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (reason) => setError((reason as Error).message),
  });

  async function uploadSkill(file: File) {
    setUploading(true);
    setError(null);
    const form = new FormData();
    form.append("archive", file);
    try {
      const skill = await api.upload<InstalledSkill>("/skills/upload", form);
      setAdding(false);
      setSelected(skill);
      setPendingRemove(false);
      installed.setPage(1);
      setSourceFilter("uploaded");
      setSearch("");
      await queryClient.invalidateQueries({ queryKey: ["skills"] });
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setUploading(false);
    }
  }

  const visibleSkills = organizationSkills;

  return (
    <div className="h-full overflow-y-auto bg-[var(--surface)] p-5 pb-28 sm:px-6 lg:pb-6">
      <div className="mx-auto max-w-[1180px]">
        {pendingInstall && (
          <Dialog open onClose={() => { if (!install.isPending) setPendingInstall(null); }}
            title={`确认入库 ${pendingInstall.display_name || pendingInstall.name}`}
            footer={<><button disabled={install.isPending} onClick={() => setPendingInstall(null)}>取消</button><button disabled={install.isPending} onClick={() => install.mutate(pendingInstall)} className="rounded-lg bg-[var(--ink)] px-4 py-2 text-[var(--surface)]">{install.isPending ? "入库中…" : "确认入库"}</button></>}
          >
            <div className="max-h-[60vh] space-y-4 overflow-y-auto text-sm">
              <p className="whitespace-pre-wrap">{pendingInstall.description || "该技能未提供说明，请确认来源后入库。"}</p>
              <p>来源：{pendingInstall.hub_id}</p>
              <p className="text-[var(--ink-soft)]">入库后默认不开放使用，请在详情中共享到部门，再由部门在智能体配置中选用。</p>
              {error && <p role="alert">{error}</p>}
            </div>
          </Dialog>
        )}
        {error && <p role="alert" className="mb-4 rounded-xl bg-red-50 p-4 text-sm text-red-700">{error}</p>}
        <ResourceToolbar variant="compact" label="筛选技能来源" options={[{ value: "", label: "全部来源" }, ...Object.entries(sourceLabels).map(([value, label]) => ({ value, label }))]}
          value={sourceFilter} onChange={setSourceFilter} searchLabel="搜索技能库" placeholder="搜索技能名称或用途" search={search} onSearch={setSearch}
          actions={canInstall && <button className="flex h-10 shrink-0 items-center gap-2 rounded-xl bg-[var(--ink)] px-4 text-sm text-[var(--surface)]" onClick={() => setAdding(true)}><Icon name="plus" className="h-4 w-4" />添加技能</button>} />
        {installed.isError ? <div role="alert" className="py-12 text-center text-sm">技能加载失败。<button className="ml-2 underline" onClick={() => void installed.refetch()}>重试</button></div> : installed.isLoading ? <p role="status" className="py-12 text-center text-sm text-[var(--ink-soft)]">正在加载技能…</p> : <div className={resourceGridClass}>
          {visibleSkills.map((skill) => <ResourceCard key={skill.id} onClick={() => { setSelected(skill); setPendingRemove(false); }} icon="tool" title={skill.display_name || skill.name}
            description={skill.metadata.description || "打开查看技能说明与使用方式。"}
            metadata={`${sourceDescription(skill)}${!skill.enabled ? " · 已停用" : ""}`}
          />)}
          {!visibleSkills.length && <div className="col-span-full py-20 text-center"><Icon name="tool" className="mx-auto mb-4 h-8 w-8 text-[var(--ink-faint)]" /><p className="font-medium">{search || sourceFilter ? "没有匹配的技能" : "还没有可用技能"}</p><p className="mt-2 text-sm text-[var(--ink-soft)]">{search || sourceFilter ? "试试其他关键词，或调整范围。" : "管理员入库并共享后，可在智能体配置中选择使用。"}</p></div>}
        </div>}
        <ResourcePagination page={installed.page} totalPages={installed.totalPages} total={installed.total} busy={installed.isFetching} onPage={installed.setPage} />
        <Dialog open={Boolean(selected)} onClose={() => { if (!uninstall.isPending && !toggleEnabled.isPending) setSelected(null); }} title={selected?.display_name || selected?.name || "技能详情"} description={selected ? sourceDescription(selected) : undefined} side width={resourceDrawerWidth}>
          {selected && <div className="space-y-6"><p className="text-sm leading-7 text-[var(--ink-soft)]">{selected.metadata.description || "暂无简介"}</p><div className="flex flex-wrap gap-2">{(selected.metadata.tags ?? []).map((tag) => <span key={tag} className="rounded-lg bg-[var(--surface-subtle)] px-3 py-1 text-xs text-[var(--ink-soft)]">{tag}</span>)}</div><p className="rounded-xl bg-[var(--surface-subtle)] p-4 text-sm leading-6">{canManageCapability(selected) ? "入库不自动开放使用。请先共享到部门或选择全组织可用，再由有权限的成员在智能体配置中选用。" : "此技能已向你开放，可在有权限编辑的智能体配置中选用。"}</p><ResourceAccessSources kind="skills" resourceId={selected.id} />{canManageCapability(selected) && <SkillDescription id={selected.id} summary={selected.metadata.description} />}{canManageCapability(selected) && <><ResourceSharing key={selected.id} kind="skills" resourceId={selected.id} /><div className="border-t border-[var(--line)] pt-5">{skillSource(selected) === "builtin" ? <button disabled={toggleEnabled.isPending} className="text-sm disabled:opacity-50" onClick={() => toggleEnabled.mutate(selected)}>{selected.enabled ? "停用技能" : "启用技能"}</button> : pendingRemove ? <div className="space-y-3 text-sm"><p>仍被智能体引用的技能无法移除，请先解除绑定。确认从技能库移除？</p><button disabled={uninstall.isPending} className="text-red-600" onClick={() => uninstall.mutate(selected.id)}>确认移除</button><button disabled={uninstall.isPending} className="ml-4" onClick={() => setPendingRemove(false)}>取消</button></div> : <button className="text-sm text-red-600" onClick={() => setPendingRemove(true)}>移除技能</button>}</div></>}{error && <p role="alert" className="text-sm text-red-600">{error}</p>}</div>}
        </Dialog>
        <Dialog open={adding && !pendingInstall} onClose={() => { if (!uploading && !install.isPending) setAdding(false); }} title="添加技能" description="创建或导入自建技能，也可从第三方来源入库。平台内置技能已在技能库中。" side width="max-w-[800px]">
        {permissions.isError || scopes.isError ? (
          <section className="rounded-2xl border border-red-200 bg-red-50 p-6 text-sm text-red-700 dark:border-red-900 dark:bg-red-950"><p>入库权限加载失败，请重试。</p><button className="mt-3 rounded-lg border border-red-300 px-3 py-1.5 text-xs" onClick={() => { void permissions.refetch(); void scopes.refetch(); }}>重试</button></section>
        ) : permissions.isLoading || scopes.isLoading ? (
          <section className="rounded-2xl border border-neutral-200 bg-white p-6 text-sm text-neutral-500 dark:border-neutral-800 dark:bg-neutral-900">正在加载入库权限…</section>
        ) : !canInstall ? (
          <section className="rounded-2xl border border-neutral-200 bg-white p-6 text-sm text-neutral-600 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-400">
            你目前没有入库权限，请联系组织管理员添加技能。
          </section>
        ) : (
          <section>
            <div className="mb-4 flex flex-wrap items-center gap-3">
              <div className="mr-auto"><h2 className="font-medium">自建或从来源入库</h2><p className="mt-1 text-xs text-neutral-500">文件上传和平台助手创建的技能都属于自建技能，入库后再按需要共享。</p></div>
              <label className="cursor-pointer rounded-lg border border-neutral-300 bg-white px-3 py-2 text-xs hover:bg-neutral-50 dark:border-neutral-700 dark:bg-neutral-900">
                {uploading ? "上传中…" : "上传 ZIP"}
                <input className="hidden" type="file" accept=".zip,application/zip" disabled={uploading} onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadSkill(file); event.currentTarget.value = ""; }} />
              </label>
            </div>
            <div className="mb-4 flex flex-wrap items-center gap-3">
              <select className="rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm dark:border-neutral-700 dark:bg-neutral-900" value={hubId} onChange={(event) => setHubId(event.target.value)}>{importHubs.map((hub) => <option key={hub.hub_id} value={hub.hub_id}>{hub.display_name}</option>)}</select>
              <form className="flex min-w-0 flex-1 gap-2 sm:flex-none" onSubmit={(event) => { event.preventDefault(); setQuery(searchInput.trim()); }}>
                <input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="搜索技能" className="min-w-0 flex-1 rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm sm:w-56 dark:border-neutral-700 dark:bg-neutral-900" />
                <button className="rounded-lg bg-neutral-900 px-4 py-2 text-sm text-[var(--surface)] dark:bg-white dark:text-neutral-900">搜索</button>
              </form>
            </div>

            {hubs.error || catalog.error ? (
              <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300">技能库暂时无法加载。<button onClick={() => { void hubs.refetch(); void catalog.refetch(); }}>重试目录</button></div>
            ) : hubs.isLoading || catalog.isLoading ? <p className="text-sm text-neutral-500">正在加载技能库…</p> : !importHubs.length ? (
              <div className="rounded-xl border border-dashed p-6 text-center text-sm text-neutral-500">当前没有可用的技能库，可上传技能包。</div>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2">
                {(catalog.data?.cards ?? []).map((card) => {
                  const present = card.installed === true || recentlyImported.has(`${card.hub_id}:${card.id}`);
                  return (
                    <article key={`${card.hub_id}:${card.id}`} className="rounded-2xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
                      <h3 className="text-sm font-semibold">{card.display_name || card.name}</h3>
                      <p className="mt-2 line-clamp-3 min-h-14 text-sm text-neutral-600 dark:text-neutral-400">{card.description || "暂无说明"}</p>
                      <div className="mt-4 flex items-center justify-between"><span className="text-xs text-neutral-400">{card.author || card.version || card.name}</span><button disabled={present || install.isPending} onClick={() => setPendingInstall(card)} className="rounded-lg bg-[var(--ink)] px-3 py-1.5 text-xs text-[var(--surface)] disabled:bg-neutral-300 dark:disabled:bg-neutral-700">{present ? "已入库" : "查看并入库"}</button></div>
                    </article>
                  );
                })}
                {!catalog.data?.cards.length && (
                  <div className="col-span-full rounded-xl border border-dashed p-6 text-center text-sm text-neutral-500">没有匹配的技能。</div>
                )}
              </div>
            )}
            <CatalogPagination page={catalogCursor.page} nextCursor={catalog.data?.next_cursor} busy={catalog.isFetching} onPrevious={catalogCursor.previous} onNext={catalogCursor.next} />
          </section>
        )}
        </Dialog>
      </div>
    </div>
  );
}
