import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import WorkspaceHome from "./page";
import { WorkspaceDraftProvider } from "@/components/workspace/WorkspaceDraftProvider";
import { api } from "@/lib/api";
vi.mock("next/navigation", () => ({ usePathname: () => "/workspace", useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/api", () => ({ listModelProfiles: async () => [{ id: "model", display_name: "测试模型", is_default: true, available: true }], api: {
 me: async () => ({ id: "u" }), get: async () => ({ items: [{ id: "agent", display_name: "默认助手", is_default: true, actions: { use: true } }], total: 1, page: 1, page_size: 100 }),
 post: vi.fn(async (path: string) => path === "/conversations" ? { id: "new", model_profile: "model" } : { task: { id: "t" }, queued: false }),
} }));
it("opens an editable draft without creating a conversation until Send", async () => {
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 render(<QueryClientProvider client={client}><WorkspaceDraftProvider><WorkspaceHome /></WorkspaceDraftProvider></QueryClientProvider>);
 await screen.findByText(/正在与「默认助手」对话/);
 const input = screen.getByRole("textbox", { name: "任务描述" });
 await waitFor(() => expect(input).not.toBeDisabled());
 expect(api.post).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole("button", { name: "分析数据与风险" }));
 expect(input).toHaveValue("分析数据与风险");
 expect(api.post).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/conversations", expect.objectContaining({ agent_id: "agent", model_profile: "model" })));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/tasks/conversations/new/tasks", { message: "分析数据与风险", approval_profile: "standard" }));
});
