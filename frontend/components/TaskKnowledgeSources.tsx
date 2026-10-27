'use client';

import { useState } from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { TaskEvent } from '@/lib/types';
import { parseKnowledgeSources, type TaskKnowledgeSource } from '@/lib/knowledge';
import { KnowledgeSources } from './KnowledgeSources';

const PAGE_SIZE = 100;

function useKnowledgeEvents(taskId: string | undefined, details: boolean, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: [details ? 'task-knowledge-details' : 'task-knowledge-sources', taskId],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.get<TaskEvent[]>(
      `/tasks/${encodeURIComponent(taskId!)}/knowledge-events?details=${details}&limit=${PAGE_SIZE}&offset=${pageParam}`,
    ),
    getNextPageParam: (lastPage, pages) => lastPage.length === PAGE_SIZE ? pages.length * PAGE_SIZE : undefined,
    enabled: !!taskId && enabled,
    staleTime: 10000,
  });
}

export function TaskKnowledgeSources({ taskId, runId, live, complete = true }: {
  taskId?: string;
  runId?: string | null;
  live: TaskKnowledgeSource[];
  complete?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const events = useKnowledgeEvents(taskId, false, complete);
  const details = useKnowledgeEvents(taskId, true, open);
  const active = open ? details : events;
  const records = (active.data?.pages.flat() ?? []);
  const sources = new Map(records.filter(event => event.event_type === 'knowledge_sources')
    .flatMap(event => parseKnowledgeSources(event.payload.sources)).map(source => [source.source_id, source]));
  for (const item of live) {
    if ((taskId && item.taskId === taskId) || (runId && item.runId === runId)) {
      if (!sources.has(item.source.source_id)) sources.set(item.source.source_id, item.source);
    }
  }
  const searches = records.filter(event => event.event_type === 'tool_result' || (event.event_type === 'knowledge_retrieval' && event.payload.phase === 'finished'));
  const failures = searches.filter(event => !['success', 'completed'].includes(String(event.payload.status)));
  if (!sources.size && !records.length && !active.isError && !open) return null;
  return <div className="space-y-2 text-xs text-[var(--ink-soft)]">
    <button type="button" aria-expanded={open} className="underline" onClick={() => setOpen(!open)}>
      {open ? '收起检索来源与详情' : '查看检索来源与详情'}
    </button>
    {!!failures.length && <p>有 {failures.length} 次检索失败或被拒绝，不代表未找到资料。</p>}
    {!open && sources.size > 0 && <p className="line-clamp-2 break-words">{[...new Set([...sources.values()].map(source => source.filename))].join(' · ')}</p>}
    {open && <>
      {details.isLoading && <p role="status">正在加载检索详情…</p>}
      {!!searches.length && <p>已加载 {searches.length} 次检索记录{active.hasNextPage ? '，还有更多' : ''}</p>}
      <KnowledgeSources sources={[...sources.values()]} />
      {!!searches.length && !sources.size && !failures.length && !active.hasNextPage && <p>没有可展示的来源记录。</p>}
      {searches.map(event => <details key={event.seq} className="rounded-lg border border-[var(--line)] p-3">
        <summary className="cursor-pointer">检索记录 · {['success', 'completed'].includes(String(event.payload.status)) ? '成功' : '失败或被拒绝'}</summary>
        <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words">{String(event.payload.arguments ?? event.payload.query ?? '历史记录未保存查询参数')}</pre>
      </details>)}
    </>}
    {active.isError && <p>检索来源加载失败。<button className="underline" onClick={() => void (active.isFetchNextPageError ? active.fetchNextPage() : active.refetch())}>重试</button></p>}
    {active.hasNextPage && <button type="button" disabled={active.isFetchingNextPage} className="underline" onClick={() => void active.fetchNextPage()}>加载更多检索来源</button>}
  </div>;
}
