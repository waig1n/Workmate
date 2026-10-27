import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { WorkspaceDraftProvider, useWorkspaceDraft } from "./WorkspaceDraftProvider";
const navigation = vi.hoisted(() => ({ pathname: "/workspace" }));
const { push } = vi.hoisted(() => ({ push: vi.fn(), pathname: "/workspace" }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname, useRouter: () => ({ push }) }));
vi.mock("@/lib/api", () => ({ api: {
 me: vi.fn(async () => ({ id: "user1" })),
 get: vi.fn(async (path: string) => path.startsWith("/agents?") ? { items: [{ id: "a", is_default: true, actions: { use: true } }, { id: "b", actions: { use: true } }], total: 2, page: 1, page_size: 100 } : []),
 post: vi.fn(), patch: vi.fn(), upload: vi.fn(),
} }));
beforeEach(() => { vi.clearAllMocks(); navigation.pathname = "/workspace"; localStorage.clear(); });
function mount() {
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 const view = renderHook(useWorkspaceDraft, { wrapper: ({ children }) => <QueryClientProvider client={client}><WorkspaceDraftProvider>{children}</WorkspaceDraftProvider></QueryClientProvider> });
 return { ...view, client };
}
it("keeps agent drafts separate and creates nothing before sending", async () => {
 const view = mount();
 await waitFor(() => expect(view.result.current.selectedAgent?.id).toBe("a"));
 act(() => view.result.current.updateDraft({ input: "甲" }));
 act(() => view.result.current.selectAgent("b"));
 expect(view.result.current.draft.input).toBe("");
 act(() => view.result.current.updateDraft({ input: "乙" }));
 act(() => view.result.current.selectAgent("a"));
 expect(view.result.current.draft.input).toBe("甲");
 expect(api.post).not.toHaveBeenCalled();
});
it("reuses a created conversation on send failure, preserves files, and patches a changed model", async () => {
 vi.mocked(api.post).mockResolvedValueOnce({ id: "c", model_profile: "m1" });
 vi.mocked(api.upload).mockRejectedValueOnce(new Error("upload failed")).mockResolvedValueOnce({ task: { id: "t" } });
 vi.mocked(api.patch).mockResolvedValueOnce({ id: "c", model_profile: "m2" });
 const view = mount();
 await waitFor(() => expect(view.result.current.selectedAgent?.id).toBe("a"));
 const file = new File(["资料"], "note.txt");
 act(() => view.result.current.updateDraft({ input: "分析", attachments: [file], approvalProfile: "strict" }));
 await act(async () => view.result.current.submit("m1"));
 expect(view.result.current.error).toBe("upload failed");
 expect(view.result.current.draft.attachments).toEqual([file]);
 expect(push).toHaveBeenCalledWith(expect.stringMatching(/^\/workspace\/pending\//));
 await act(async () => view.result.current.submit("m2"));
 expect(api.post).toHaveBeenCalledTimes(1);
 expect(api.patch).toHaveBeenCalledWith("/conversations/c/model", { model_profile: "m2" });
 const form = vi.mocked(api.upload).mock.calls[1][1] as FormData;
 expect(form.get("message")).toBe("分析");
 expect(form.get("approval_profile")).toBe("strict");
 expect(form.getAll("files")).toHaveLength(1);
 expect(view.result.current.draft.input).toBe("");
 expect(view.result.current.pendingSubmission).toMatchObject({ status: "sent", conversationId: "c" });
});
it("synchronously prevents duplicate submits and draft changes during creation", async () => {
 let finish!: (value: unknown) => void;
 vi.mocked(api.post).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; })).mockResolvedValueOnce({ task: { id: "t" } });
 const view = mount();
 await waitFor(() => expect(view.result.current.selectedAgent?.id).toBe("a"));
 act(() => view.result.current.updateDraft({ input: "目标" }));
 let pending!: Promise<void>;
 act(() => { pending = view.result.current.submit("m"); void view.result.current.submit("m"); view.result.current.selectAgent("b"); view.result.current.updateDraft({ input: "changed" }); });
 expect(api.post).toHaveBeenCalledTimes(1);
 expect(view.result.current.selectedAgent?.id).toBe("a");
 expect(view.result.current.draft.input).toBe("目标");
 await act(async () => { finish({ id: "c", model_profile: "m" }); await pending; });
 expect(api.post).toHaveBeenLastCalledWith("/tasks/conversations/c/tasks", { message: "目标", approval_profile: "standard" });
});

it("restores only text for the same user and keeps conversation files in memory", async () => {
 localStorage.setItem("workmate_workspace_draft:user1:a", "已保存文本");
 localStorage.setItem("workmate_workspace_draft:other:a", "其他用户文本");
 const view = mount();
 await waitFor(() => expect(view.result.current.draft.input).toBe("已保存文本"));
 expect(view.result.current.draft.attachments).toEqual([]);
 const file = new File(["x"], "x.txt");
 act(() => view.result.current.setConversationFiles("c", [file]));
 act(() => view.result.current.selectAgent("b"));
 expect(view.result.current.getConversationFiles("c")).toEqual([file]);
 act(() => view.result.current.setConversationFiles("c", []));
 expect(view.result.current.getConversationFiles("c")).toEqual([]);
 act(() => view.result.current.selectAgent("a"));
 act(() => view.result.current.updateDraft({ input: "新文本" }));
 expect(localStorage.getItem("workmate_workspace_draft:user1:a")).toBe("新文本");
 expect(localStorage.getItem("workmate_workspace_draft:other:a")).toBe("其他用户文本");
});
it("retains the draft and retries creation after an API failure", async () => {
 vi.mocked(api.post).mockRejectedValueOnce(new Error("create failed"));
 const view = mount();
 await waitFor(() => expect(view.result.current.selectedAgent?.id).toBe("a"));
 act(() => view.result.current.updateDraft({ input: "保留" }));
 await act(async () => view.result.current.submit("m"));
 expect(view.result.current.draft.input).toBe("保留");
 expect(view.result.current.sending).toBe(false);
 expect(view.result.current.error).toBe("create failed");
 expect(push).toHaveBeenCalledWith(expect.stringMatching(/^\/workspace\/pending\//));
});

it("does not redirect after the user navigates away during sending", async () => {
 let finish!: (value: unknown) => void;
 vi.mocked(api.post).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; })).mockResolvedValueOnce({ task: { id: "t" } });
 const view = mount();
 await waitFor(() => expect(view.result.current.selectedAgent?.id).toBe("a"));
 act(() => view.result.current.updateDraft({ input: "目标" }));
 let pending!: Promise<void>;
 act(() => { pending = view.result.current.submit("m"); });
 navigation.pathname = "/workspace/skills";
 view.rerender();
 await act(async () => { finish({ id: "c", model_profile: "m" }); await pending; });
 expect(push).toHaveBeenCalledWith(expect.stringMatching(/^\/workspace\/pending\//));
});
it("refuses to resend a failed draft if its conversation has since been used", async () => {
 vi.mocked(api.post).mockResolvedValueOnce({ id: "c", model_profile: "m" }).mockRejectedValueOnce(new Error("network"));
 const view = mount();
 await waitFor(() => expect(view.result.current.selectedAgent?.id).toBe("a"));
 act(() => view.result.current.updateDraft({ input: "目标" }));
 await act(async () => view.result.current.submit("m"));
 vi.mocked(api.get).mockResolvedValueOnce([{ id: "existing-task" }]);
 await act(async () => view.result.current.submit("m"));
 expect(api.post).toHaveBeenCalledTimes(2);
 expect(view.result.current.error).toContain("未重复发送");
 expect(view.result.current.resumeConversationId).toBe("c");
 expect(view.result.current.draft.input).toBe("目标");
});

it("does not silently switch a populated default-agent draft after permission is revoked", async () => {
 const view = mount();
 await waitFor(() => expect(view.result.current.selectedAgent?.id).toBe("a"));
 act(() => view.result.current.updateDraft({ input: "只给甲助手的资料" }));
 act(() => view.client.setQueryData(["agents"], [{ id: "a", is_default: true, actions: { use: false } }, { id: "b", actions: { use: true } }]));
 await waitFor(() => expect(view.result.current.selectedAgent).toBeUndefined());
 await act(async () => view.result.current.submit("m"));
 expect(api.post).not.toHaveBeenCalled();
 act(() => view.result.current.selectAgent("b"));
 expect(view.result.current.selectedAgent?.id).toBe("b");
 expect(view.result.current.draft.input).toBe("");
 expect(localStorage.getItem("workmate_workspace_draft:user1:a")).toBe("只给甲助手的资料");
});

it("shows an immutable pending submission before creation resolves and retries that snapshot", async () => {
 let finish!: (value: unknown) => void;
 vi.mocked(api.post).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; })).mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce({ task: { id: "t" } });
 const view = mount();
 await waitFor(() => expect(view.result.current.selectedAgent?.id).toBe("a"));
 act(() => view.result.current.updateDraft({ input: "原始要求" }));
 let request!: Promise<void>;
 act(() => { request = view.result.current.submit("m"); });
 expect(push).toHaveBeenCalledWith(`/workspace/pending/${view.result.current.pendingSubmission?.token}`);
 expect(view.result.current.pendingSubmission).toMatchObject({ status: "sending", draft: { input: "原始要求" } });
 await act(async () => { finish({ id: "c", model_profile: "m" }); await request; });
 const token = view.result.current.pendingSubmission!.token;
 act(() => { view.result.current.updateDraft({ input: "另一个草稿" }); view.result.current.selectAgent("b"); });
 await act(async () => view.result.current.retrySubmission(token));
 expect(api.post).toHaveBeenLastCalledWith("/tasks/conversations/c/tasks", { message: "原始要求", approval_profile: "standard" });
 expect(view.result.current.pendingSubmission?.status).toBe("sent");
 act(() => view.result.current.selectAgent("a"));
 expect(view.result.current.draft.input).toBe("另一个草稿");
 expect(localStorage.getItem("workmate_workspace_draft:user1:a")).toBe("另一个草稿");
});
