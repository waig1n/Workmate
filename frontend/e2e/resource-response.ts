/** Keep resource mocks faithful to resource page responses. */
export function resourceResponse(rawUrl: string, body: unknown): unknown {
  const url = new URL(rawUrl);
  if (!Array.isArray(body) || !["/api/agents", "/api/skills", "/api/mcps", "/api/knowledge-bases"].includes(url.pathname)) return body;
  const q = (url.searchParams.get("q") ?? "").toLocaleLowerCase();
  const source = url.searchParams.get("source") ?? "all";
  const category = url.searchParams.get("category") ?? "all";
  const view = url.searchParams.get("view") ?? "all";
  const filtered = body.filter(item => {
    const itemSource = item.metadata?.hub_id === "workmate-builtin" ? "builtin" : item.metadata?.hub_id === "uploaded" ? "uploaded" : "third-party";
    return (!q || `${item.name ?? ""} ${item.display_name ?? ""} ${item.description ?? item.metadata?.description ?? ""} ${item.scope?.name ?? ""}`.toLocaleLowerCase().includes(q))
      && (source === "all" || source === itemSource)
      && (view === "all" || (view === "personal" && item.scope?.kind === "personal") || (view === "shared" && item.scope?.kind !== "personal" && item.actions?.use) || (view === "managed" && item.scope?.kind !== "personal" && item.actions?.manage))
      && (category === "all" || (category === "mine" && item.scope?.kind === "personal") || (category === "shared" && item.scope?.kind !== "personal" && !item.system_key) || (category === "builtin" && !!item.system_key));
  });
  const page = Number(url.searchParams.get("page") ?? 1);
  const pageSize = Number(url.searchParams.get("page_size") ?? 12);
  return { items: filtered.slice((page - 1) * pageSize, page * pageSize), total: filtered.length, page, page_size: pageSize };
}
