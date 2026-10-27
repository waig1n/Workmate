"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function SkillDescription({ id, summary }: { id: string; summary?: string }) {
  const [open, setOpen] = useState(false);
  const detail = useQuery({
    queryKey: ["skill-description", id],
    queryFn: () => api.get<{ description: string; markdown: string }>(`/skills/${id}/description`),
    enabled: open,
    staleTime: 0,
    gcTime: 0,
  });
  return <details onToggle={(event) => setOpen(event.currentTarget.open)} className="mt-3 text-sm text-neutral-600 dark:text-neutral-400">
    <summary className="cursor-pointer">技能说明</summary>
    <p className="mt-2 whitespace-pre-wrap break-words">{open && detail.isSuccess && !detail.isFetching ? detail.data.description || summary || "暂无简介" : summary || "暂无简介"}</p>
    {detail.isLoading && <p>正在加载完整说明…</p>}
    {detail.isError && <button onClick={() => void detail.refetch()}>说明加载失败，点击重试</button>}
    {open && detail.isSuccess && !detail.isFetching && detail.data.markdown && <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-words font-sans text-sm">{detail.data.markdown}</pre>}
  </details>;
}
