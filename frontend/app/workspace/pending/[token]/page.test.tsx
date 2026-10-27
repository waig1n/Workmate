import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import Page from "./page";
import type { PendingSubmission } from "@/components/workspace/WorkspaceDraftProvider";
const state = vi.hoisted(() => ({ pendingSubmission: null as PendingSubmission | null, retrySubmission: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useParams: () => ({ token: "temporary" }), useRouter: () => ({ replace: state.replace }) }));
vi.mock("@/components/workspace/WorkspaceDraftProvider", () => ({ useWorkspaceDraft: () => state }));
beforeEach(() => {
 vi.clearAllMocks();
 state.pendingSubmission = { token: "temporary", draftKey: "u:a", agent: { display_name: "默认助手" }, draft: { input: "分析文件", attachments: [new File(["data"], "report.md")], modelProfile: "m", approvalProfile: "standard" }, status: "sending", error: null } as PendingSubmission;
});
it("shows the submitted message and thinking immediately without exposing setup phases", () => {
 render(<Page />);
 expect(screen.getByText("分析文件")).toBeInTheDocument();
 expect(screen.getByText("report.md")).toBeInTheDocument();
 expect(screen.getByRole("status")).toHaveTextContent("工作中…");
 expect(screen.queryByText(/正在准备|正在创建|正在上传/)).not.toBeInTheDocument();
 expect(state.replace).not.toHaveBeenCalled();
});
it("replaces the temporary URL only when the mounted submission succeeds", () => {
 const view = render(<Page />);
 state.pendingSubmission = { ...state.pendingSubmission!, status: "sent", conversationId: "real" };
 view.rerender(<Page />);
 expect(state.replace).toHaveBeenCalledWith("/workspace/c/real");
});
it("preserves failed content and retries only on explicit click", () => {
 state.pendingSubmission = { ...state.pendingSubmission!, status: "failed", error: "网络异常" };
 render(<Page />);
 expect(screen.getByRole("alert")).toHaveTextContent("内容已保留");
 expect(screen.getByText("report.md")).toBeInTheDocument();
 expect(state.retrySubmission).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole("button", { name: "重试发送" }));
 expect(state.retrySubmission).toHaveBeenCalledWith("temporary");
});
it("recovers an expired temporary URL without automatically submitting", () => {
 state.pendingSubmission = null;
 render(<Page />);
 expect(screen.getByRole("link", { name: "查看历史对话" })).toHaveAttribute("href", "/workspace/tasks");
 expect(state.retrySubmission).not.toHaveBeenCalled();
 expect(state.replace).not.toHaveBeenCalled();
});
it("does not redirect when the user has left the pending page", () => {
 const view = render(<Page />);
 view.unmount();
 state.pendingSubmission = { ...state.pendingSubmission!, status: "sent", conversationId: "real" };
 expect(state.replace).not.toHaveBeenCalled();
});
