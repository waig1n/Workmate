/** Serve page envelopes from small list fixtures while preserving detail and candidate APIs. */
export function resourcePageMock(handler: (path: string) => Promise<unknown>) {
  const records = new Map<string, { id: string }[]>();
  return async <T,>(path: string): Promise<T> => {
    const url = new URL(path, "http://test");
    const match = url.pathname.match(/^\/(agents|skills|mcps|knowledge-bases)$/);
    if (match) {
      const raw = await handler(url.pathname);
      if (!Array.isArray(raw)) return raw as T;
      records.set(match[1], raw);
      const q = (url.searchParams.get("q") ?? "").toLocaleLowerCase();
      const category = url.searchParams.get("category") ?? "all";
      const source = url.searchParams.get("source") ?? "all";
      const view = url.searchParams.get("view") ?? "all";
      const filtered = raw.filter(item => {
        const haystack = [item.name, item.display_name, item.description, item.metadata?.description, item.scope?.name].join(" ").toLocaleLowerCase();
        const itemSource = item.metadata?.hub_id === "uploaded" ? "uploaded" : item.metadata?.hub_id === "workmate-builtin" ? "builtin" : "third-party";
        const relationMatches = view === "all"
          || (view === "personal" && item.scope?.kind === "personal")
          || (view === "shared" && item.scope?.kind !== "personal" && item.actions?.use)
          || (view === "managed" && item.scope?.kind !== "personal" && item.actions?.manage);
        return (!q || haystack.includes(q)) && relationMatches
          && (source === "all" || itemSource === source)
          && (category === "all" || (category === "mine" && item.scope?.kind === "personal") || (category === "shared" && item.scope?.kind !== "personal" && !item.system_key) || (category === "builtin" && item.system_key));
      });
      const page = Number(url.searchParams.get("page") ?? 1);
      const size = Number(url.searchParams.get("page_size") ?? 12);
      return { items: filtered.slice((page - 1) * size, page * size), total: filtered.length, page, page_size: size } as T;
    }
    const detail = url.pathname.match(/^\/(agents|knowledge-bases)\/([^/]+)$/);
    if (detail && records.has(detail[1])) {
      const value = records.get(detail[1])?.find(item => item.id === detail[2]);
      if (value) return value as T;
    }
    return await handler(path) as T;
  };
}
