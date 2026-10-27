'use client';
import { resourceList } from "@/lib/resourceList";
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { downloadKnowledgeDocument, type KnowledgeBase, type KnowledgeSource } from '@/lib/knowledge';
export function KnowledgeSources({ sources }: {
    sources: KnowledgeSource[];
}) {
    const [error, setError] = useState('');
    const libraries = useQuery({ queryKey: ['knowledge-bases'], queryFn: () => resourceList<KnowledgeBase>('/knowledge-bases'), enabled: sources.length > 0 });
    const canDownload = (id: string) => libraries.data?.some(library => library.id === id && library.actions.download) === true;
    if (!sources.length)
        return null;
    return <section className="space-y-3 rounded-xl border border-[var(--line)] p-4"><h3 className="text-sm font-medium">检索来源</h3><p className="text-xs text-[var(--ink-soft)]">以下为检索召回片段；获得原文下载授权后可打开核对。</p>{sources.map(source => <article key={source.source_id} className="rounded-lg bg-[var(--surface-subtle)] p-3">{canDownload(source.knowledge_base_id) ? <button className="text-left text-sm font-medium underline" onClick={() => { setError(''); void downloadKnowledgeDocument(source.knowledge_base_id, source.document_id, source.filename).catch(reason => setError(reason.message)); }}>{source.filename}</button> : <p className="text-sm font-medium">{source.filename}</p>}<p className="mt-1 text-xs text-[var(--ink-soft)]">{['page', 'page_number', 'slide', 'sheet'].filter(key => source.metadata?.[key] != null).map(key => `${({ page: '页', page_number: '页', slide: '幻灯片', sheet: '工作表' } as Record<string, string>)[key]} ${String(source.metadata[key])}`).join(' · ') || `片段 ${source.chunk_index + 1}`}</p><p className="mt-2 whitespace-pre-wrap break-words text-sm">{source.excerpt}</p></article>)}{error && <p role="alert" className="text-sm text-red-600">{error}</p>}</section>;
}
