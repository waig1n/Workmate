"use client";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "./api";
import type { InstalledSkill, InstalledMCP } from "./types";
const PAGE_SIZE = 20;
export function useAgentCapabilities(url: string | null, type: "skill" | "mcp") {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 250); return () => clearTimeout(timer); }, [search]);
  const result = useInfiniteQuery({
    queryKey: ["agent-capability-candidates", url, type, query],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.get<(InstalledSkill | InstalledMCP)[]>(`${url}&${new URLSearchParams({ capability_type: type, q: query, limit: String(PAGE_SIZE), offset: String(pageParam) })}`),
    enabled: Boolean(url),
    getNextPageParam: (last, pages) => last.length === PAGE_SIZE ? pages.length * PAGE_SIZE : undefined,
  });
  const items = Array.from(new Map((result.data?.pages.flat() ?? []).map((item) => [item.id, item])).values());
  return { ...result, items, search, setSearch };
}
