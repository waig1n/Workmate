"use client";
import { bindingIssueLabel } from "@/lib/agentBindingStatus";
import { ResourceToolbar } from "@/components/ui/ResourceToolbar";
import { resourceList } from "@/lib/resourceList";
import { ResourcePagination } from "@/components/ui/ResourcePagination";
import { useResourcePage } from "@/lib/useResourcePage";
import { ResourceCard, resourceDrawerWidth, resourceGridClass } from "@/components/ui/ResourceCard";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { useRouter } from "next/navigation";
import { KnowledgeBinding } from "@/components/KnowledgeBinding";
import { emptyKnowledgeConfig, type KnowledgeConfig } from "@/lib/knowledge";
import { HelpTip } from "@/components/ui/HelpTip";
import { Dialog } from "@/components/ui/Dialog";
import { useWorkspaceDraft } from "@/components/workspace/WorkspaceDraftProvider";
import { ResourceAccessSources } from "@/components/sharing/ResourceAccessSources";
import { ResourceSharing, ShareSummary, type SharePlan } from "@/components/sharing/ResourceSharing";
import { api, type ApiError } from "@/lib/api";
import { AGENT_DENIED_TOOL_GROUPS } from "@/lib/agentToolPolicy";
import {
  capabilityPickerState,
  canCreateOrganizationAgent,
  canCreatePersonalAgent,
  degradationState,
} from "@/lib/permissions";
import type { Agent, AgentDeniedToolGroup, InstalledMCP, InstalledSkill, PlatformToolProfile, Scope, Visibility } from "@/lib/types";
import { useAgentCapabilities } from "@/lib/useAgentCapabilities";
import { usePermissions } from "@/lib/usePermissions";

type Draft = {
  knowledge_config: KnowledgeConfig;
  description: string;
  display_name: string;
  system_prompt: string;
  denied_tool_groups: AgentDeniedToolGroup[];
  denied_tools: string[];
  platform_tool_profile: PlatformToolProfile;
  visibility: Visibility;
  skill_binding_ids: string[];
  mcp_binding_ids: string[];
  is_default: boolean;
};

const blankDraft: Draft = {
  knowledge_config: emptyKnowledgeConfig(),
  description: "",
  display_name: "",
  system_prompt: "",
  denied_tool_groups: [],
  denied_tools: [],
  platform_tool_profile: "none",
  visibility: "private",
  skill_binding_ids: [],
  mcp_binding_ids: [],
  is_default: false,
};

function draftOf(agent: Agent): Draft {
  return {
    knowledge_config: agent.knowledge_config ?? emptyKnowledgeConfig(),
    display_name: agent.display_name,
    description: agent.description ?? "",
    system_prompt: !agent.system_key && agent.actions.update ? agent.system_prompt ?? "" : "",
    denied_tool_groups: agent.tool_policy?.denied_groups ?? [],
    denied_tools: agent.tool_policy?.denied_tools ?? [],
    platform_tool_profile: agent.platform_tool_profile ?? "none",
    visibility: agent.visibility,
    skill_binding_ids: agent.skill_binding_ids ?? [],
    mcp_binding_ids: agent.mcp_binding_ids ?? [],
    is_default: agent.is_default,
  };
}

type Tab = "personal" | "organization";

export default function AgentsPage() {
  const queryClient = useQueryClient();
  const router = useRouter();
  const workspace = useWorkspaceDraft();
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState(false);
  const [openSharingId, setOpenSharingId] = useState<string | null>(null);
  const permissionsQuery = usePermissions();
  const permissions = permissionsQuery.data;
  const createPersonal = canCreatePersonalAgent(permissions);

  const [tab, setTab] = useState<Tab>("personal");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [targetScopeId, setTargetScopeId] = useState("");
  const [slug, setSlug] = useState("");
  const [draft, setDraft] = useState<Draft>(blankDraft);
  const [error, setError] = useState<string | null>(null);

  const agents = useResourcePage<Agent>("agents", { q: search.trim(), category: filter });
  const [selectedSnapshot, setSelectedSnapshot] = useState<Agent | null>(null);
  const detail = useQuery({ queryKey: ["agents", "detail", selectedId], queryFn: () => api.get<Agent>(`/agents/${selectedId}`), enabled: Boolean(selectedId), initialData: selectedSnapshot?.id === selectedId ? selectedSnapshot : undefined });
  const scopes = useQuery({ queryKey: ["scopes"], queryFn: () => api.get<Scope[]>("/scopes/accessible") });

  const personalScope = (scopes.data ?? []).find((scope) => scope.id === permissions?.personal_scope_id)
    ?? (scopes.data ?? []).find((scope) => scope.kind === "personal");
  const organizationScopes = (scopes.data ?? []).filter((scope) => scope.kind !== "personal");
  const creatableOrganizationScopes = organizationScopes.filter(canCreateOrganizationAgent);

  const listed = agents.items;
  const currentSelection = agents.items.find((agent) => agent.id === selectedId) ?? selectedSnapshot;
  const selected = useMemo(() => {
    const candidate = selectedId ? detail.data ?? selectedSnapshot : null;
    if (!candidate) return null;
    // A previously cached editable detail cannot elevate the current list's authorization.
    if (detail.isFetching || detail.isError || !detail.isSuccess || !currentSelection?.actions.update || !candidate.actions.update) {
      return { ...candidate, system_prompt: "", binding_issues: null, actions: { ...candidate.actions, update: false, delete: false, publish: false } };
    }
    return candidate;
  }, [selectedId, detail.data, detail.isFetching, detail.isError, detail.isSuccess, selectedSnapshot, currentSelection]);
  const writable = creating
    ? tab === "personal" ? createPersonal : Boolean(
      creatableOrganizationScopes.find((scope) => scope.id === targetScopeId),
    )
    : Boolean(selected?.actions.update);
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => resourceList<InstalledSkill>("/skills"), enabled: writable && Boolean(selectedId) && !creating && !editing });
  const mcps = useQuery({ queryKey: ["mcps"], queryFn: () => resourceList<InstalledMCP>("/mcps"), enabled: writable && Boolean(selectedId) && !creating && !editing });
  const deletable = Boolean(selected?.actions.delete);
  const candidateUrl = selected && writable
    ? `/agents/capability-candidates?agent_id=${encodeURIComponent(selected.id)}`
    : creating && tab === "personal" && personalScope
      ? `/agents/capability-candidates?scope_id=${encodeURIComponent(personalScope.id)}`
      : creating && tab === "organization" && targetScopeId
        ? `/agents/capability-candidates?scope_id=${encodeURIComponent(targetScopeId)}`
        : null;
  const skillCandidates = useAgentCapabilities(candidateUrl, "skill");
  const mcpCandidates = useAgentCapabilities(candidateUrl, "mcp");

  useEffect(() => {
    if (!selected && !creating) setSelectedId(null);
  }, [selected, creating]);

  const mayManageCurrent = Boolean(currentSelection?.actions.update);
  useEffect(() => {
    if (!selectedId || !detail.data) return;
    setDraft(draftOf({ ...detail.data, actions: { ...detail.data.actions, update: mayManageCurrent && detail.data.actions.update } }));
    setError(null);
  }, [selectedId, detail.data, mayManageCurrent]);

  useEffect(() => {
    if (
      creating
      && tab === "organization"
      && !creatableOrganizationScopes.some((scope) => scope.id === targetScopeId)
    ) {
      setTargetScopeId(creatableOrganizationScopes[0]?.id ?? "");
      setDraft((current) => ({
        ...current,
        skill_binding_ids: [],
        mcp_binding_ids: [],
        knowledge_config: emptyKnowledgeConfig(),
      }));
    }
  }, [creating, creatableOrganizationScopes, tab, targetScopeId]);

  function beginCreate(kind: Tab = "personal") {
    if ((kind === "personal" && !createPersonal) || (kind === "organization" && !creatableOrganizationScopes.length)) return;
    setTab(kind);
    setEditing(true);
    setCreating(true);
    setSelectedId(null);
    setSlug("");
    setDraft(blankDraft);
    setTargetScopeId(creatableOrganizationScopes[0]?.id ?? "");
    setError(null);
  }

  function select(agent: Agent) {
    setSharingConfirmation(null);
    setOpenSharingId(null);
    setCreating(false);
    setEditing(false);
    setTab(agent.scope.kind === "personal" ? "personal" : "organization");
    setSelectedSnapshot(agent);
    setDraft(draftOf(agent));
    setSelectedId(agent.id);
  }

  function payload() {
    const base = {
      knowledge_config: draft.knowledge_config,
      display_name: draft.display_name,
      description: draft.description,
      ...((creating || !selected?.system_key) ? { system_prompt: draft.system_prompt || undefined } : {}),
      tool_policy: {
        denied_groups: draft.denied_tool_groups,
        denied_tools: draft.denied_tools,
      },
      platform_tool_profile: draft.platform_tool_profile,
      skill_binding_ids: draft.skill_binding_ids,
      mcp_binding_ids: draft.mcp_binding_ids,
      is_default: draft.is_default,
    };
    if (tab === "personal") return creating ? { ...base, visibility: "private" as const } : base;
    return {
      ...base,
      visibility: draft.visibility,
      ...(creating ? { scope_id: targetScopeId } : {}),
    };
  }

  const [sharingConfirmation, setSharingConfirmation] = useState<{ confirmation: string; plans: SharePlan[] } | null>(null);
  const save = useMutation({
    mutationFn: async (confirmation?: string) => {
      if (!writable) throw new Error("当前没有修改权限");
      if (creating) {

        if (tab === "organization" && !targetScopeId) throw new Error("请选择管理归属");
        return api.post<Agent>("/agents", { slug: slug.trim() || `agent-${Array.from(crypto.getRandomValues(new Uint8Array(6)), (value) => value.toString(16).padStart(2, "0")).join("")}`, ...payload() });
      }
      if (!selectedId) throw new Error("请选择智能体");
      return api.patch<Agent>(`/agents/${selectedId}`, { ...payload(), sharing_confirmation: confirmation });
    },
    onSuccess: async (agent) => {
      setSharingConfirmation(null);
      if (creating && agent.scope.kind !== "personal" && agent.actions.publish) setOpenSharingId(agent.id);
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["sharing", "agents", agent.id] });
      setCreating(false);
      setEditing(false);
      setSelectedSnapshot(agent);
      queryClient.setQueryData(["agents", "detail", agent.id], agent);
      if (creating) { agents.setPage(1); setFilter("all"); setSearch(""); }
      setSelectedId(agent.id);
      setTab(agent.scope.kind === "personal" ? "personal" : "organization");
      setError(null);
    },
    onError: (reason) => {
      const failure = reason as ApiError;
      if (failure.code === "sharing_confirmation_required" && failure.detail) {
        setSharingConfirmation(failure.detail as unknown as { confirmation: string; plans: SharePlan[] });
      }
      const blockers = failure.detail?.blockers;
      setError(Array.isArray(blockers) ? `${failure.message}：${blockers.join("；")}` : failure.message);
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/agents/${id}`),
    onSuccess: async () => {
      setSelectedId(null);
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
    onError: (reason) => setError((reason as Error).message),
  });

  const restorePrompt = useMutation({
    mutationFn: (id: string) => api.post<Agent>(
      `/agents/${id}/restore-default-prompt`,
      {},
    ),
    onSuccess: async (agent) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      setDraft(draftOf(agent));
      setError(null);
    },
    onError: (reason) => setError((reason as Error).message),
  });

  function toggleSkill(id: string) {
    setDraft((current) => ({
      ...current,
      skill_binding_ids: current.skill_binding_ids.includes(id)
        ? current.skill_binding_ids.filter((item) => item !== id)
        : [...current.skill_binding_ids, id],
    }));
  }

  function toggleMCP(id: string) {
    setDraft((current) => ({
      ...current,
      mcp_binding_ids: current.mcp_binding_ids.includes(id)
        ? current.mcp_binding_ids.filter((item) => item !== id)
        : [...current.mcp_binding_ids, id],
    }));
  }

  const degradation = selected ? degradationState(selected) : { degraded: false, unavailableIds: [] as string[] };
  const bindingIssues = selected?.binding_issues ?? [];
  const issueIds = new Set(bindingIssues.map(issue => issue.id));
  const skillItems = skillCandidates.items.filter(item => !issueIds.has(item.id));
  const mcpItems = mcpCandidates.items.filter(item => !issueIds.has(item.id));
  const unavailableSkills = bindingIssues.filter(issue => issue.kind === "skill" && draft.skill_binding_ids.includes(issue.id)).map(issue => issue.id);
  const unavailableMcps = bindingIssues.filter(issue => issue.kind === "mcp" && draft.mcp_binding_ids.includes(issue.id)).map(issue => issue.id);
  const personalUseRestricted = Boolean(selected && (!selected.actions.use || degradation.degraded || selected.unavailable_knowledge_base_ids?.length));

  const publicationScope = selected?.scope.kind === "personal"
    ? null
    : selected?.scope ?? creatableOrganizationScopes.find((scope) => scope.id === targetScopeId);
  const platformToolsWritable = tab === "organization" && Boolean(
    selected
      ? selected.actions.update
      : publicationScope?.permissions.includes("agent.update"),
  );

  function closePanel() {
    if (save.isPending || remove.isPending) return;
    setCreating(false); setEditing(false); setSelectedId(null); setError(null);
  }
  function useAgent(agent: Agent) {
    if (!agent.actions.use || degradationState(agent).degraded || agent.unavailable_knowledge_base_ids?.length) return;
    workspace.selectAgent(agent.id);
    router.push("/workspace");
  }
  function copyPersonal(agent: Agent) {
    if (agent.system_key || !createPersonal || !agent.actions.update) return;
    beginCreate("personal");
    setDraft({ ...blankDraft, display_name: `${agent.display_name}（副本）`, description: agent.description ?? "",
      system_prompt: !agent.system_key && agent.actions.update ? agent.system_prompt ?? "" : "", denied_tool_groups: agent.tool_policy?.denied_groups ?? [], denied_tools: agent.tool_policy?.denied_tools ?? [] });
  }
  return (
    <div className="h-full overflow-y-auto bg-[var(--surface)] p-5 pb-28 sm:px-6 lg:pb-6">
      <div className="mx-auto max-w-[1180px]">
      <ResourceToolbar label="智能体分类" options={[{ value: "all", label: "全部" }, { value: "mine", label: "我的" }, { value: "shared", label: "组织共享" }, { value: "builtin", label: "平台内置" }]}
        value={filter} onChange={setFilter} searchLabel="搜索智能体" placeholder="搜索名称、用途或归属" search={search} onSearch={setSearch}
        actions={<>
          {creatableOrganizationScopes.length > 0 && <button className="product-control px-4 text-sm" onClick={() => beginCreate("organization")}>创建组织智能体</button>}
          {createPersonal && <button className="h-10 rounded-xl bg-[var(--ink)] px-4 text-sm font-medium text-[var(--surface)]" onClick={() => beginCreate("personal")}>创建个人智能体</button>}
        </>} />
      {(agents.isError || permissionsQuery.isError || scopes.isError) && <p role="alert" className="my-4 text-sm text-[var(--danger)]">目录或权限加载失败。<button className="ml-2 underline" onClick={() => { void agents.refetch(); void permissionsQuery.refetch(); void scopes.refetch(); }}>重试</button></p>}
      {agents.isLoading ? <p role="status">正在加载智能体…</p> : <div className={resourceGridClass}>
        {listed.map((agent) => <ResourceCard key={agent.id} onClick={() => select(agent)} icon="agents" title={agent.display_name}
          description={agent.description || "暂无简介"}
          badge={agent.is_default ? <span className="shrink-0 rounded-md bg-[var(--accent-soft)] px-2 py-1 text-xs text-[var(--accent)]">默认</span> : undefined}
          metadata={`${agent.system_key ? "平台内置" : agent.scope.kind === "personal" ? "个人" : agent.scope.name} · ${agent.actions.update && (!agent.actions.use || degradationState(agent).degraded || agent.unavailable_knowledge_base_ids?.length) ? "可管理" : degradationState(agent).degraded || agent.unavailable_knowledge_base_ids?.length ? "使用需授权" : agent.actions.use ? "可使用" : "仅查看"} · ${agent.skill_binding_ids.length} 个技能`}
        />)}
      </div>}
      {!agents.isLoading && !agents.isError && !listed.length && <div className="rounded-2xl border border-dashed border-[var(--line)] p-12 text-center text-sm text-[var(--ink-soft)]">{search ? "没有找到匹配的智能体，请尝试其他关键词。" : "暂无智能体，可以创建个人智能体开始使用。"}</div>}
      <ResourcePagination page={agents.page} totalPages={agents.totalPages} total={agents.total} busy={agents.isFetching} onPage={agents.setPage} />
      <Dialog side width={resourceDrawerWidth} open={creating || Boolean(selected)} onClose={closePanel} title={creating ? `创建${tab === "personal" ? "个人" : "组织"}智能体` : selected?.display_name ?? "智能体详情"}>
        {selectedId && detail.isFetching && <p role="status" className="mb-4 text-sm text-[var(--ink-soft)]">正在校验详情权限…</p>}
        {selectedId && detail.isError && <p role="alert" className="mb-4 text-sm text-[var(--danger)]">无法验证详情访问权限，内部配置已隐藏。<button className="ml-2 underline" onClick={() => void detail.refetch()}>重试详情</button></p>}
        {permissionsQuery.isError || scopes.isError ? (
          <div className="grid h-full min-h-48 place-items-center text-center text-sm text-red-600"><div><p>权限或 Scope 加载失败。</p><button className="mt-3 rounded-lg border px-3 py-1.5 text-xs" onClick={() => { void permissionsQuery.refetch(); void scopes.refetch(); }}>重试</button></div></div>
        ) : permissionsQuery.isLoading ? (
          <div className="grid h-full min-h-48 place-items-center text-sm text-neutral-500">正在加载权限…</div>
        ) : !creating && !selected ? (
          <div className="grid h-full min-h-48 place-items-center text-sm text-neutral-500">选择一个智能体</div>
        ) : !creating && selected && (!editing || !writable) ? (
          <article className="space-y-6">
            <p className="text-sm text-[var(--ink-faint)]">{selected.system_key ? "平台内置" : selected.scope.kind === "personal" ? "个人智能体 · 仅自己可见" : selected.scope.name}</p>
            {selected.scope.kind !== "personal" && <p className="text-sm text-[var(--ink-soft)]">管理范围：{selected.scope.name} · {writable ? "你可以编辑配置" : "你没有编辑权限"}</p>}
            {selected.scope.kind !== "personal" && selected.visibility === "public" && <ResourceAccessSources kind="agents" resourceId={selected.id} />}
            {selected.actions.publish && <ResourceSharing key={selected.id} kind="agents" resourceId={selected.id} publishOnShare={selected.visibility === "private"} initiallyOpen={openSharingId === selected.id} />}
            {writable && !selected.actions.use && <p className="text-sm text-[var(--warning)]">你可以管理此智能体，但尚未获得使用权限。请将使用范围开放给你所属的部门，或联系管理者授权。</p>}
            <p className="whitespace-pre-wrap break-words text-sm leading-7">{selected.description || "暂无简介，请联系智能体管理者补充。"}</p>
            {writable && (selected.skill_binding_ids.length > 0 || selected.mcp_binding_ids.length > 0) && <section className="rounded-xl bg-[var(--surface-subtle)] p-4"><h3 className="mb-3 font-medium">关联能力</h3><div className="flex flex-wrap gap-2">{[...selected.skill_binding_ids, ...selected.mcp_binding_ids].map((id) => {
              const item = [...(skills.data ?? []), ...(mcps.data ?? [])].find((item) => item.id === id);
              return <span key={id} className="rounded-lg border border-[var(--line)] px-3 py-2 text-sm">{item?.display_name || item?.name || "暂不可见的能力"}</span>;
            })}</div></section>}
            {writable && !!selected.knowledge_config?.knowledge_base_ids.length && <p className="text-sm text-[var(--ink-soft)]">已绑定 {selected.knowledge_config.knowledge_base_ids.length} 个知识库 · {selected.knowledge_config.parameters.mode === "agentic" ? "按需自主检索" : "每轮自动检索"}</p>}
            {personalUseRestricted && <p className="text-sm text-[var(--ink-soft)]">{writable ? "你可以管理此智能体；使用或试运行需要相应的使用授权。" : "当前账号尚不能使用此智能体，请联系管理员确认使用授权或资源状态。"}</p>}
            {bindingIssues.length > 0 && <p className="text-sm text-[var(--warning)]">{bindingIssues.length} 项绑定需要检查，可在编辑中查看具体原因。</p>}
            <div className="flex flex-wrap gap-3">
              {selected.actions.use && <button disabled={workspace.sending || degradation.degraded || Boolean(selected.unavailable_knowledge_base_ids?.length)} className="rounded-xl bg-[var(--ink)] px-4 py-2.5 text-sm text-[var(--surface)] disabled:opacity-50" onClick={() => useAgent(selected)}>{selected.scope.kind !== "personal" && selected.visibility === "private" ? "试用此智能体" : "使用此智能体"}</button>}
              {writable && <button className="product-control px-4 text-sm" onClick={() => setEditing(true)}>编辑</button>}
              {createPersonal && !selected.system_key && selected.actions.update && <button className="product-control px-4 text-sm" onClick={() => copyPersonal(selected)}>复制为个人智能体</button>}
            </div>
            {createPersonal && !selected.system_key && selected.actions.update && <p className="text-xs leading-5 text-[var(--ink-faint)]">复制名称、简介、指令和工具限制；技能与连接需在个人范围内重新选择。</p>}
          </article>
        ) : (
          <div className="mx-auto max-w-3xl space-y-6">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">{creating ? `创建${tab === "personal" ? "个人" : "组织"}智能体` : "智能体配置"}</h2>
                <p className="text-sm text-neutral-500">能力绑定会在会话运行前同步到工作区。</p>
                {!writable && <p className="mt-1 text-xs text-amber-600">当前账号在此 Scope 没有编辑权限，配置为只读。</p>}
              </div>
              {!creating && selected && (
                <div className="flex items-center gap-3">
                  {selected.system_key === "workspace_assistant" && selected.actions.update && (
                    <button className="text-sm text-blue-600 disabled:opacity-50" disabled={restorePrompt.isPending} onClick={() => { if (window.confirm("恢复平台维护的默认指令？此操作将替换当前指令。")) restorePrompt.mutate(selected.id); }}>{restorePrompt.isPending ? "恢复中…" : "恢复默认提示词"}</button>
                  )}
                  {deletable && <button className="text-sm text-red-500 disabled:opacity-50" disabled={remove.isPending} onClick={() => { if (window.confirm(`确定删除“${selected.display_name}”？删除后历史会话仍可查看，但无法继续发送消息。`)) remove.mutate(selected.id); }}>删除</button>}
                </div>
              )}
            </div>

            {!creating && selected?.actions.publish && <ResourceSharing key={selected.id} kind="agents" resourceId={selected.id} publishOnShare={selected.visibility === "private"} disabled={JSON.stringify(draft) !== JSON.stringify(draftOf(selected))} />}
            {sharingConfirmation && <div className="rounded-xl border p-4 space-y-3">
              <ShareSummary plans={sharingConfirmation.plans} />
              <button disabled={save.isPending} className="product-control px-3 py-2 text-sm" onClick={() => save.mutate(sharingConfirmation.confirmation)}>确认共享新增依赖并保存</button>
              <button className="ml-3 text-sm" onClick={() => setSharingConfirmation(null)}>取消</button>
            </div>}
            {selected && selected.scope.kind !== "personal" && (
              <p className="rounded-lg bg-neutral-50 px-3 py-2 text-xs text-neutral-500 dark:bg-neutral-900">
                管理归属：{selected.scope.name} · {selected.visibility === "public" ? "已发布，按显式共享授权使用" : "私有草稿，仅创建者可试用，管理权限不授予运行权"}
              </p>
            )}

            <div className="grid gap-4 rounded-xl border border-neutral-200 p-5 sm:grid-cols-2 dark:border-neutral-800">
              <label className="text-sm sm:col-span-2">简介说明<textarea maxLength={2000} className="mt-1 w-full rounded-lg border px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900" value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} disabled={!writable} placeholder="向使用者介绍用途、适用场景和能力边界，不填写内部提示词。" /></label>
              {creating && <label className="text-sm">标识（可选，自动生成）<input className="mt-1 w-full rounded-lg border px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900" value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="research-assistant" /></label>}
              <label className="text-sm">显示名称<input className="mt-1 w-full rounded-lg border px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900" value={draft.display_name} onChange={(event) => setDraft({ ...draft, display_name: event.target.value })} disabled={!writable} /></label>
              {creating && tab === "organization" && (
                <label className="text-sm">管理归属
                  <select
                    className="mt-1 w-full rounded-lg border px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
                    value={targetScopeId}
                    onChange={(event) => {
                      setTargetScopeId(event.target.value);
                      // Capability eligibility is scope-relative. Keeping
                      // hidden selections from the old scope causes a 422.
                      setDraft((current) => ({
                        ...current,
                        skill_binding_ids: [],
                        mcp_binding_ids: [],
                        knowledge_config: emptyKnowledgeConfig(),
                      }));
                    }}
                  >
                    <option value="" disabled>请选择管理归属</option>
                    {creatableOrganizationScopes.map((scope) => <option key={scope.id} value={scope.id}>{scope.name}</option>)}
                  </select>
                </label>
              )}
              <label className="flex items-center gap-2 self-end pb-2 text-sm"><input type="checkbox" checked={draft.is_default} onChange={(event) => setDraft({ ...draft, is_default: event.target.checked })} disabled={!writable} />设为新会话默认智能体</label>
            </div>

            <KnowledgeBinding scopeId={creating ? tab === "personal" ? personalScope?.id : targetScopeId : selected?.scope_id} value={draft.knowledge_config} onChange={(knowledge_config) => setDraft({ ...draft, knowledge_config })} disabled={!writable} unavailableIds={bindingIssues.filter(issue => issue.kind === "knowledge").map(issue => issue.id)} issueLabels={Object.fromEntries(bindingIssues.map(issue => [issue.id, bindingIssueLabel(issue.reason)]))} />

            <details className="rounded-xl border border-[var(--line)] p-4"><summary className="cursor-pointer text-sm font-medium">高级配置：指令、技能与权限</summary><div className="mt-4 space-y-5">
            {tab === "organization" && (
              <div className="rounded-xl border border-neutral-200 p-5 dark:border-neutral-800">
                <h3 className="text-sm font-medium">发布状态</h3>
                <p className="mt-1 text-xs text-neutral-500">{draft.visibility === "public" ? "已发布，使用范围见共享设置。" : "草稿保存后，选择使用范围并确认发布。"}</p>
                {selected?.actions.publish && selected.visibility === "public" && draft.visibility === "public" && <button type="button" className="mt-3 text-sm text-red-600" onClick={() => setDraft({ ...draft, visibility: "private" })}>撤回为草稿</button>}
                {selected?.actions.publish && selected.visibility === "public" && draft.visibility === "private" && <button type="button" className="mt-3 text-sm underline" onClick={() => setDraft({ ...draft, visibility: "public" })}>取消撤回</button>}
                {selected?.visibility === "public" && draft.visibility === "private" && <p className="mt-4 text-sm text-amber-700">保存为私有草稿将撤销全部智能体共享；已共享的技能、MCP 连接和知识库授权保留。以后重新发布也不会恢复旧授权。</p>}
              </div>
            )}

            {tab === "organization" && (
              <div className="rounded-xl border border-neutral-200 p-5 dark:border-neutral-800">
                <h3 className="text-sm font-medium">平台管理工具</h3>
                <p className="mt-1 text-xs text-neutral-500">允许该 Agent 调用受 Product API 逐次授权的 Agent 与 Skill 管理工具；此开关本身不授予用户权限。</p>
                <label className="mt-4 flex items-start gap-3 text-sm">
                  <input
                    className="mt-1"
                    type="checkbox"
                    checked={draft.platform_tool_profile === "workspace"}
                    disabled={!platformToolsWritable}
                    onChange={(event) => setDraft({
                      ...draft,
                      platform_tool_profile: event.target.checked ? "workspace" : "none",
                    })}
                  />
                  <span>启用工作台工具目录<span className="mt-0.5 block text-[11px] text-[var(--ink-faint)]">具体工具仍受当前用户 RBAC、Agent DENY、Task 审批和 Runtime 护栏约束。</span></span>
                </label>
              </div>
            )}

            {writable && (creating || !selected?.system_key) && <div className="rounded-xl border border-neutral-200 p-5 dark:border-neutral-800">
              <h3 className="mb-1 text-sm font-medium">系统提示词</h3><p className="mb-3 text-xs text-neutral-500">定义角色边界、工作方式和输出要求。</p>
              <textarea aria-label="系统提示词" className="h-48 w-full rounded-lg border p-3 text-sm dark:border-neutral-700 dark:bg-neutral-900" value={draft.system_prompt} onChange={(event) => setDraft({ ...draft, system_prompt: event.target.value })} />
            </div>}

            <div className="rounded-xl border border-neutral-200 p-5 dark:border-neutral-800">
              <h3 className="text-sm font-medium">用户资料查询</h3>
              {[
                { tool: "get_current_user", label: "允许读取当前用户资料", description: "读取当前使用者的姓名、邮箱和所属部门。只能获取本人资料，不支持指定其他用户。" },
                { tool: "list_scope_users", label: "允许查询授权范围内的成员资料", description: "有成员查看权限的用户可查询授权范围内的姓名、邮箱和部门。普通成员仅能查询本人；共享智能体不会共享创建者的权限。" },
              ].map(({ tool, label, description }) => (
                <div key={tool} className="mt-3 flex items-start gap-1.5">
                  <label className="flex min-w-0 items-start gap-3 text-sm leading-6">
                    <input type="checkbox" className="mt-1.5 shrink-0" disabled={!writable}
                      checked={!draft.denied_tools.some((name) => name.trim().toLowerCase() === tool)}
                      onChange={(event) => setDraft((current) => ({ ...current,
                        denied_tools: event.target.checked
                          ? current.denied_tools.filter((name) => name.trim().toLowerCase() !== tool)
                          : [...current.denied_tools.filter((name) => name.trim().toLowerCase() !== tool), tool],
                      }))} />
                    <span>{label}</span>
                  </label>
                  <HelpTip label={`${label}说明`}>
                    <p className="mb-1.5 font-medium text-[var(--ink)]">{label}</p>
                    <p>{description}</p>
                  </HelpTip>
                </div>
              ))}
            </div>

            <div className="rounded-xl border border-neutral-200 p-5 dark:border-neutral-800">
              <h3 className="text-sm font-medium">工具权限上限</h3>
              <p className="mt-1 text-xs leading-5 text-neutral-500">这里的禁止规则是智能体的硬边界。用户即使为任务选择“完全访问”，命中这些规则时仍会被 Runtime 拒绝。</p>
              <div className="mt-4 grid gap-2 sm:grid-cols-2">
                {AGENT_DENIED_TOOL_GROUPS.map((group) => (
                  <label key={group.value} className="flex items-start gap-3 rounded-xl border border-[var(--line)] bg-[var(--surface-subtle)] p-3 text-sm">
                    <input
                      className="mt-1"
                      type="checkbox"
                      checked={draft.denied_tool_groups.includes(group.value)}
                      disabled={!writable}
                      onChange={() => setDraft((current) => ({
                        ...current,
                        denied_tool_groups: current.denied_tool_groups.includes(group.value)
                          ? current.denied_tool_groups.filter((item) => item !== group.value)
                          : [...current.denied_tool_groups, group.value],
                      }))}
                    />
                    <span><span className="block font-medium">禁止{group.label}</span><span className="mt-0.5 block text-[11px] leading-4 text-[var(--ink-faint)]">{group.description}</span></span>
                  </label>
                ))}
              </div>
              <label className="mt-4 block text-xs font-medium">额外禁止的工具标识
                <textarea
                  className="mt-2 h-20 w-full rounded-lg border p-3 font-mono text-xs dark:border-neutral-700 dark:bg-neutral-900"
                  value={draft.denied_tools.join("\n")}
                  disabled={!writable}
                  placeholder={"DeleteUser\nmcp__mail__send"}
                  onChange={(event) => setDraft({
                    ...draft,
                    denied_tools: event.target.value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean),
                  })}
                />
              </label>
            </div>

            <CapabilityPicker
              title="Skills"
              description="个人智能体可组合当前有权使用的组织能力；组织智能体可绑定自身、祖先 Scope 的能力，或显式共享到其部门的技能。"
              href="/workspace/skills"
              linkLabel="打开技能中心"
              items={skillItems}
              unavailableIds={unavailableSkills}
              selectedIds={draft.skill_binding_ids}
              writable={Boolean(writable)}
              loading={skillCandidates.isLoading}
              pagination={skillCandidates}
              failed={skillCandidates.isError}
              scopes={scopes.data ?? []}
              onToggle={toggleSkill}
              unavailableLabel="Skill 绑定需检查"
              issueLabels={Object.fromEntries(bindingIssues.map(issue => [issue.id, bindingIssueLabel(issue.reason)]))}
            />

            <CapabilityPicker
              title="MCP 连接"
              description="只显示已授权给目标范围且已启用的受管连接；发布时可确认补充共享，不转移凭据。"
              href="/workspace/mcps"
              linkLabel="查看 MCP"
              items={mcpItems}
              unavailableIds={unavailableMcps}
              selectedIds={draft.mcp_binding_ids}
              writable={Boolean(writable)}
              loading={mcpCandidates.isLoading}
              pagination={mcpCandidates}
              failed={mcpCandidates.isError}
              scopes={scopes.data ?? []}
              onToggle={toggleMCP}
              unavailableLabel="MCP 绑定需检查"
              issueLabels={Object.fromEntries(bindingIssues.map(issue => [issue.id, bindingIssueLabel(issue.reason)]))}
            />

            </div></details>
            {error && <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600 dark:bg-red-950">{error}</p>}
            {writable && (
              <div className="flex justify-end"><button className="rounded-lg bg-blue-600 px-5 py-2 text-sm text-white disabled:opacity-50" disabled={save.isPending || !draft.display_name.trim() || (creating && tab === "organization" && !targetScopeId)} onClick={() => save.mutate(undefined)}>{save.isPending ? "保存中…" : creating ? "创建智能体" : "保存配置"}</button></div>
            )}
          </div>
        )}
      </Dialog>
      </div>
    </div>
  );
}

function CapabilityPicker({
  title,
  description,
  href,
  linkLabel,
  items,
  unavailableIds,
  selectedIds,
  writable,
  loading,
  failed,
  scopes,
  onToggle,
  unavailableLabel,
  issueLabels,
  pagination,
}: {
  pagination: ReturnType<typeof useAgentCapabilities>;
  title: string;
  description: string;
  href: string;
  linkLabel: string;
  items: (InstalledSkill | InstalledMCP)[];
  unavailableIds: string[];
  selectedIds: string[];
  writable: boolean;
  loading: boolean;
  failed: boolean;
  scopes: Scope[];
  onToggle: (id: string) => void;
  unavailableLabel: string;
  issueLabels: Record<string, string>;
}) {
  const presentation = capabilityPickerState(
    loading, failed && items.length === 0, unavailableIds,
  );
  return (
    <div className="rounded-xl border border-neutral-200 p-5 dark:border-neutral-800">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div><h3 className="text-sm font-medium">{title}</h3><p className="text-xs text-neutral-500">{description}</p></div>
        <a href={href} className="text-xs text-blue-600">{linkLabel}</a>
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-3"><input aria-label={`搜索${title}`} placeholder="搜索名称" className="product-control min-w-0 flex-1 px-3 text-sm" value={pagination.search} onChange={(event) => pagination.setSearch(event.target.value)} /><span className="text-xs text-[var(--ink-soft)]">已选 {selectedIds.length} 项（切换搜索保留选择）</span></div>
      {failed && <button type="button" className="mb-3 text-sm underline" onClick={() => void pagination.refetch()}>重新加载{title}</button>}
      {failed ? <p className="text-sm text-red-600">能力列表加载失败，请重试；已有选择保持不变。</p> : loading ? <p className="text-sm text-neutral-500">加载中…</p> : null}
      {presentation.showCandidates && (
        <div className="grid gap-2 sm:grid-cols-2">
          {items.map((item) => (
            <label key={item.id} className="flex cursor-pointer items-start gap-3 rounded-lg border p-3 dark:border-neutral-800">
              <input className="mt-1" type="checkbox" checked={selectedIds.includes(item.id)} onChange={() => onToggle(item.id)} disabled={!writable} />
              <span className="min-w-0"><span className="block truncate text-sm font-medium">{item.display_name || item.name}</span>{!item.usable && <span className="block text-xs text-[var(--ink-soft)]">可绑定 · 使用需另行授权</span>}<span className="text-[11px] text-neutral-400">{scopes.find((scope) => scope.id === item.scope_id)?.name ?? "组织能力"}</span><span className="line-clamp-2 text-xs text-neutral-500">{item.metadata.description || "无描述"}</span></span>
            </label>
          ))}
          {!items.length && !unavailableIds.length && <p className="text-sm text-neutral-500">没有匹配的可绑定能力。</p>}
        </div>
      )}
      {pagination.hasNextPage && <button type="button" disabled={pagination.isFetchingNextPage} className="mt-3 rounded-lg border border-[var(--line)] px-4 py-2 text-sm disabled:opacity-50" onClick={() => void pagination.fetchNextPage()}>{pagination.isFetchingNextPage ? "加载中…" : `加载更多${title}`}</button>}
      {!loading && !failed && items.length > 0 && <p className="mt-3 text-xs text-[var(--ink-faint)]">已加载 {items.length} 项{!pagination.hasNextPage ? " · 已显示全部搜索结果" : " · 每批 20 项"}</p>}
      {presentation.showUnavailable && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {unavailableIds.map((id) => (
            <label key={id} className="flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-950">
              <input className="mt-1" type="checkbox" checked onChange={() => onToggle(id)} disabled={!writable} />
              <span className="min-w-0"><span className="block text-sm font-medium text-amber-700 dark:text-amber-300">{issueLabels[id] ?? unavailableLabel}</span><span className="block break-all font-mono text-[11px] text-amber-600">{id}</span></span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
