import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { expect, it, vi, afterEach } from "vitest";
import { api, listModelProfiles } from "./api";
import { useConversationModel } from "./useConversationModel";
import type { Conversation } from "./types";
vi.mock("./api", () => ({ api: { patch: vi.fn() }, listModelProfiles: vi.fn(async () => [{ id: "a", is_default: true }, { id: "b" }]) }));
afterEach(() => vi.clearAllMocks());
const conversation = { id: "c", model_profile: "a", read_only: false } as Conversation;
function wrapper({ children }: { children: React.ReactNode }) { return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>; }
it("keeps the old model on failure and blocks switching during active work", async () => {
 vi.mocked(api.patch).mockRejectedValueOnce(new Error("切换失败"));
 const view = renderHook(({ busy }) => useConversationModel(conversation, busy), { wrapper, initialProps: { busy: false } });
 await waitFor(() => expect(view.result.current.profiles.length).toBe(2));
 act(() => view.result.current.onChange("b"));
 await waitFor(() => expect(view.result.current.error).toBe("切换失败"));
 expect(view.result.current.value).toBe("a");
 view.rerender({ busy: true });
 expect(view.result.current.disabled).toBe(true);
 act(() => view.result.current.onChange("b"));
 expect(api.patch).toHaveBeenCalledTimes(1);
});
it("blocks sending while a model change is pending and uses the server result", async () => {
 let finish!: (value: Conversation) => void;
 vi.mocked(api.patch).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 client.setQueryData(["conversation", "c"], conversation);
 const view = renderHook(() => {
  const current = useQuery({ queryKey: ["conversation", "c"], queryFn: async () => conversation, staleTime: Infinity });
  return useConversationModel(current.data, false);
 }, { wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
 await waitFor(() => expect(view.result.current.profiles.length).toBe(2));
 act(() => view.result.current.onChange("b"));
 await waitFor(() => expect(view.result.current.pending).toBe(true));
 await act(async () => finish({ ...conversation, model_profile: "server-choice" }));
 await waitFor(() => expect(view.result.current.pending).toBe(false));
 expect(view.result.current.value).toBe("server-choice");
 expect(client.getQueryData(["conversation", "c"])).toEqual({ ...conversation, model_profile: "server-choice" });
 expect(api.patch).toHaveBeenCalledWith("/conversations/c/model", { model_profile: "b" });
});

it("blocks switching for read-only conversations", async () => {
 const view = renderHook(() => useConversationModel({ ...conversation, read_only: true }, false), { wrapper });
 await waitFor(() => expect(view.result.current.profiles.length).toBe(2));
 act(() => view.result.current.onChange("b"));
 expect(view.result.current.disabled).toBe(true);
 expect(api.patch).not.toHaveBeenCalled();
});
it("locks immediately so rapid changes cannot race before the pending render", async () => {
 let finish!: (value: Conversation) => void;
 vi.mocked(api.patch).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
 const view = renderHook(() => useConversationModel(conversation, false), { wrapper });
 await waitFor(() => expect(view.result.current.profiles.length).toBe(2));
 act(() => {
  view.result.current.onChange("b");
  expect(view.result.current.isSwitching()).toBe(true);
  view.result.current.onChange("b");
 });
 await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
 await act(async () => finish(conversation));
 expect(view.result.current.isSwitching()).toBe(false);
});
