import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { api } from "./api";
import { useAgentCapabilities } from "./useAgentCapabilities";
vi.mock("./api", () => ({ api: { get: vi.fn() } }));
it("loads bounded pages and resets server pagination for a search", async () => {
 vi.mocked(api.get).mockImplementation(async (path) => {
  const params = new URL(`http://test${path}`).searchParams;
  const offset = Number(params.get("offset"));
  return Array.from({ length: params.get("q") ? 1 : offset === 0 ? 20 : 2 }, (_, i) => ({ id: `${params.get("q") || "skill"}-${i + offset}` }));
 });
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 const { result } = renderHook(() => useAgentCapabilities("/agents/capability-candidates?scope_id=personal", "skill"), { wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
 await waitFor(() => expect(result.current.items).toHaveLength(20));
 await act(async () => { await result.current.fetchNextPage(); });
 await waitFor(() => expect(result.current.items).toHaveLength(22));
 expect(result.current.hasNextPage).toBe(false);
 act(() => result.current.setSearch("写作"));
 await waitFor(() => expect(result.current.items).toHaveLength(1));
 expect(api.get).toHaveBeenLastCalledWith(expect.stringContaining("offset=0"));
 expect(result.current.items[0].id).toBe("写作-0");
});
