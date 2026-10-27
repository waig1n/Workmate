"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { api } from "./api";
import type { Permissions } from "./types";
import { ADMIN_CHANGE_EVENT, ADMIN_CHANGE_KEY } from "./administrationEvents";

/** Request-aware permission facts; never read localStorage during render. */
export function usePermissions() {
  const cache = useQueryClient();
  useEffect(() => {
    const refresh = () => { void cache.invalidateQueries({ predicate: (query) => {
      const key = String(query.queryKey[0]);
      return key.startsWith("admin-") || key === "auth" || key === "scopes" || key === "agents";
    } }); };
    const onStorage = (event: StorageEvent) => { if (event.key === ADMIN_CHANGE_KEY) refresh(); };
    window.addEventListener(ADMIN_CHANGE_EVENT, refresh);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(ADMIN_CHANGE_EVENT, refresh);
      window.removeEventListener("storage", onStorage);
    };
  }, [cache]);
  return useQuery({
    queryKey: ["auth", "permissions"],
    queryFn: () => api.get<Permissions>("/auth/me/permissions"),
    staleTime: 30_000,
    refetchOnWindowFocus: true,
  });
}
