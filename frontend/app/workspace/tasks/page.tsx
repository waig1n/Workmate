"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Suspense, useEffect, useState } from "react";

import { Icon } from "@/components/ui/Icons";
import { api } from "@/lib/api";
import type { Conversation } from "@/lib/types";

type HistoryPage = { items: Conversation[]; next_cursor: string | null };

export default function TasksPage() {
  return <Suspense fallback={<p role="status" className="p-8 text-sm text-[var(--ink-soft)]">正在加载历史对话…</p>}><ConversationHistory /></Suspense>;
}

function ConversationHistory() {
  const searchParams = useSearchParams();
  const initialSearch = searchParams.get("q")?.slice(0, 200) ?? "";
  const [search, setSearch] = useState(initialSearch);
  const [query, setQuery] = useState(initialSearch.trim());
  useEffect(() => {
    setSearch(initialSearch);
    setQuery(initialSearch.trim());
  }, [initialSearch]);
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);
  const history = useInfiniteQuery({
    queryKey: ["conversations", "history", query],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "20" });
      if (query) params.set("q", query);
      if (pageParam) params.set("cursor", pageParam);
      return api.get<HistoryPage>(`/conversations/history?${params}`);
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  // A conversation updated between requests can move across the cursor boundary.
  const conversations = [...new Map(
    (history.data?.pages.flatMap((page) => page.items) ?? []).map((item) => [item.id, item]),
  ).values()];

  return (
    <div className="h-full overflow-y-auto bg-[var(--surface)]">
      <div className="mx-auto max-w-[960px] px-5 pb-28 pt-8 sm:px-8 lg:pb-12 lg:pt-11">
        <header className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-[30px] font-semibold tracking-[-.04em]">历史对话</h1>
            <p className="mt-2 text-sm text-[var(--ink-soft)]">查找以前的对话，随时继续交流。</p>
          </div>
          <button type="button" onClick={() => window.dispatchEvent(new Event("workmate:create"))} className="shrink-0 rounded-xl bg-[var(--ink)] px-4 py-2.5 text-sm font-medium text-[var(--surface)]">新建对话</button>
        </header>
        <div className="relative mt-7">
          <Icon name="search" className="pointer-events-none absolute left-4 top-3.5 h-4 w-4 text-[var(--ink-faint)]" />
          <input aria-label="搜索历史对话" value={search} onChange={(event) => setSearch(event.target.value)} maxLength={200} placeholder="搜索对话标题" className="w-full rounded-2xl border border-[var(--line)] bg-[var(--surface-raised)] py-3 pl-11 pr-4 text-sm outline-none focus:border-[var(--accent)]" />
        </div>
        {history.isLoading && <p role="status" className="mt-8 text-sm text-[var(--ink-soft)]">正在加载历史对话…</p>}
        {history.isError && <div role="alert" className="mt-6 flex items-center gap-3 text-sm text-red-600"><span>对话加载失败，请重试。</span><button type="button" onClick={() => history.isFetchNextPageError ? history.fetchNextPage() : history.refetch()} className="underline">重试</button></div>}
        {!history.isLoading && !history.isError && conversations.length === 0 && <p className="mt-16 text-center text-sm text-[var(--ink-soft)]">{query ? "没有找到匹配的对话，试试其他关键词。" : "还没有历史对话，开始一段新对话吧。"}</p>}
        <ul className="mt-6 divide-y divide-[var(--line)]">
          {conversations.map((conversation) => <li key={conversation.id}>
            <Link href={`/workspace/c/${conversation.id}`} className="group flex items-center gap-4 rounded-xl px-3 py-5 transition-colors hover:bg-[var(--surface-subtle)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]">
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-[var(--accent-soft)] text-[var(--accent)]"><Icon name="clock" className="h-5 w-5" /></span>
              <div className="min-w-0 flex-1">
                <h2 className="truncate text-sm font-medium">{conversation.title || "未命名对话"}</h2>
                <p className="mt-1.5 truncate text-xs text-[var(--ink-soft)]">{conversation.agent?.display_name ?? "历史智能体"}{conversation.read_only ? " · 仅可查看" : ""}</p>
              </div>
              <time dateTime={conversation.updated_at} className="shrink-0 text-xs text-[var(--ink-faint)]">{new Date(conversation.updated_at).toLocaleDateString("zh-CN")}</time>
              <Icon name="arrow" className="h-4 w-4 shrink-0 text-[var(--ink-faint)]" />
            </Link>
          </li>)}
        </ul>
        {conversations.length > 0 && <div className="mt-6 text-center">
          {history.hasNextPage ? <button type="button" disabled={history.isFetching} onClick={() => history.fetchNextPage()} className="rounded-xl border border-[var(--line)] px-6 py-2.5 text-sm hover:bg-[var(--surface-subtle)] disabled:opacity-50">{history.isFetchingNextPage ? "正在加载…" : "加载更多对话"}</button> : <p className="text-xs text-[var(--ink-faint)]">已显示全部{query ? "搜索结果" : "对话"}</p>}
        </div>}
      </div>
    </div>
  );
}
