"use client";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { ResourcePage } from "./types";
export const RESOURCE_PAGE_SIZE = 12;
export function useResourcePage<T>(resource: string, filters: Record<string, string> = {}) {
  const filterKey = JSON.stringify(filters);
  const [position, setPosition] = useState({ filterKey, page: 1 });
  useEffect(() => {
    setPosition((current) => current.filterKey === filterKey ? current : { filterKey, page: 1 });
  }, [filterKey]);
  const page = position.filterKey === filterKey ? position.page : 1;
  const setPage = (next: number) => setPosition({ filterKey, page: Math.max(1, next) });
  const params = new URLSearchParams({ page: String(page), page_size: String(RESOURCE_PAGE_SIZE), ...filters });
  const query = useQuery({ queryKey: [resource, "page", filterKey, page], queryFn: () => api.get<ResourcePage<T>>(`/${resource}?${params}`) });
  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / RESOURCE_PAGE_SIZE));
  useEffect(() => {
    if (query.isSuccess && page > totalPages) setPosition({ filterKey, page: totalPages });
  }, [query.isSuccess, page, totalPages, filterKey]);
  return { ...query, items: query.data?.items ?? [], total, page, totalPages, setPage };
}
export function useCatalogCursor(key: string) {
  const [position, setPosition] = useState({ key, cursors: [""], index: 0 });
  useEffect(() => {
    setPosition((current) => current.key === key ? current : { key, cursors: [""], index: 0 });
  }, [key]);
  const current = position.key === key ? position : { key, cursors: [""], index: 0 };
  return {
    cursor: current.cursors[current.index], page: current.index + 1,
    previous: () => setPosition({ ...current, index: Math.max(0, current.index - 1) }),
    next: (cursor: string) => setPosition({ key, cursors: [...current.cursors.slice(0, current.index + 1), cursor], index: current.index + 1 }),
  };
}
