import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "./api";
import { useResourcePage, useCatalogCursor } from "./useResourcePage";
vi.mock("./api", () => ({ api: { get: vi.fn() } }));
beforeEach(() => vi.resetAllMocks());
test("server pages isolate legacy caches, reset filters, and back off after the last item is deleted", async () => {
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 client.setQueryData(["skills"], [{ id: "legacy" }]);
 let total = 13;
 vi.mocked(api.get).mockImplementation(async path => { const params = new URL(path, "http://test").searchParams; const page = Number(params.get("page")); return { items: page === 1 ? [{ id: "first" }] : total === 13 ? [{ id: "last" }] : [], total, page, page_size: 12 }; });
 const { result, rerender } = renderHook(({ q }) => useResourcePage<{ id: string }>("skills", { q }), { initialProps: { q: "" }, wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
 await waitFor(() => expect(result.current.items).toEqual([{ id: "first" }]));
 act(() => result.current.setPage(2));
 await waitFor(() => expect(result.current.items).toEqual([{ id: "last" }]));
 total = 12;
 await act(async () => { await client.invalidateQueries({ queryKey: ["skills", "page"] }); });
 await waitFor(() => expect(result.current.page).toBe(1));
 act(() => result.current.setPage(2));
 rerender({ q: "reports" });
 await waitFor(() => expect(api.get).toHaveBeenCalledWith("/skills?page=1&page_size=12&q=reports"));
 expect(client.getQueryData(["skills"])).toEqual([{ id: "legacy" }]);
});

test("returning to a previous filter always starts at page one", async () => {
 vi.mocked(api.get).mockResolvedValue({items:[],total:36,page:1,page_size:12});
 const client=new QueryClient();
 const {result,rerender}=renderHook(({q})=>useResourcePage("skills",{q}),{initialProps:{q:"A"},wrapper:({children})=><QueryClientProvider client={client}>{children}</QueryClientProvider>});
 await waitFor(()=>expect(result.current.isSuccess).toBe(true));
 act(()=>result.current.setPage(3));
 expect(result.current.page).toBe(3);
 rerender({q:"B"}); expect(result.current.page).toBe(1);
 rerender({q:"A"}); expect(result.current.page).toBe(1);
});
test("catalog source changes discard prior cursor positions in both directions",()=>{
 const {result,rerender}=renderHook(({source})=>useCatalogCursor(source),{initialProps:{source:"A"}});
 act(()=>result.current.next("second"));expect(result.current.cursor).toBe("second");
 rerender({source:"B"});expect(result.current.page).toBe(1);
 rerender({source:"A"});expect(result.current.page).toBe(1);expect(result.current.cursor).toBe("");
});
