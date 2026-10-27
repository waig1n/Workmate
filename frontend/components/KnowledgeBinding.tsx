'use client';
import { resourceList } from "@/lib/resourceList";
import { useQuery } from '@tanstack/react-query';
import type { KnowledgeBase, KnowledgeConfig } from '@/lib/knowledge';
export function KnowledgeBinding({ scopeId, value, onChange, disabled = false, unavailableIds = [], issueLabels = {} }: {
    scopeId?: string;
    value: KnowledgeConfig;
    onChange: (value: KnowledgeConfig) => void;
    disabled?: boolean;
    unavailableIds?: string[];
    issueLabels?: Record<string, string>;
}) {
    const query = useQuery({ queryKey: ['knowledge-bases', 'binding', scopeId], queryFn: () => resourceList<KnowledgeBase>(`/knowledge-bases?agent_scope_id=${encodeURIComponent(scopeId!)}`), enabled: !!scopeId });
    const candidates = query.data ?? [];
    const missing = value.knowledge_base_ids.filter(id => !candidates.some(k => k.id === id) && (unavailableIds.includes(id) || (!query.isPending && !query.isError)));
    const toggle = (id: string) => onChange({ ...value, knowledge_base_ids: value.knowledge_base_ids.includes(id) ? value.knowledge_base_ids.filter(item => item !== id) : [...value.knowledge_base_ids, id] });
    return <section className="rounded-xl border border-[var(--line)] p-5 space-y-3"><div className="flex justify-between gap-3"><h3 className="font-medium">知识库（可选）</h3><a href="/workspace/knowledge-bases" className="text-sm underline">管理知识库</a></div><p className="text-sm text-[var(--ink-soft)]">默认不绑定。按智能体所属范围选择知识库；实际检索还会检查使用者权限。</p>
 {query.isLoading && <p role="status">加载知识库…</p>}{query.isError && <p role="alert">知识库加载失败。<button className="underline" onClick={() => void query.refetch()}>重新加载</button></p>}
 {!query.isPending && !query.isError && !candidates.length && <p className="text-sm">当前范围没有可绑定的知识库。</p>}
 <div className="grid gap-2 sm:grid-cols-2">{candidates.map(k => <label key={k.id} className="flex gap-3 rounded-lg border border-[var(--line)] p-3 text-sm"><input type="checkbox" checked={value.knowledge_base_ids.includes(k.id)} disabled={disabled || (!value.knowledge_base_ids.includes(k.id) && !k.enabled)} onChange={() => toggle(k.id)}/><span>{k.name}<span className="block text-xs text-[var(--ink-soft)]">{k.scope.name}{issueLabels[k.id] ? ` · ${issueLabels[k.id]}` : !k.enabled ? ' · 已停用' : !k.actions.use ? ' · 可绑定，使用需另行授权' : ''}</span></span></label>)}</div>
 {missing.map(id => <label key={id} className="flex gap-2 text-sm text-[var(--warning)]"><input type="checkbox" checked disabled={disabled} onChange={() => toggle(id)}/>{issueLabels[id] ?? "绑定范围未开放或资源已移除"}（取消勾选以移除）：{id}</label>)}
 {!!value.knowledge_base_ids.length && <div className="border-t border-[var(--line)] pt-3"><label className="flex gap-2 text-sm"><input type="checkbox" checked={value.parameters.mode === 'agentic'} disabled={disabled} onChange={event => onChange({ ...value, parameters: { ...value.parameters, mode: event.target.checked ? 'agentic' : 'static' } })}/>由 Agent 自主决定何时检索</label><p className="mt-2 text-xs text-[var(--ink-soft)]">{value.parameters.mode === 'agentic' ? 'Agent 按需检索所选知识库。' : '每轮自动检索所选知识库并提供相关片段。'}</p></div>}
 </section>;
}
