import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import LoginPage from "./page";
import { api } from "@/lib/api";
const mocks = vi.hoisted(() => ({ push: vi.fn(), setAuth: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock("@/lib/api", () => ({ setAuth: mocks.setAuth, api: {
 registrationStatus: vi.fn(),
 login: vi.fn(),
 register: vi.fn(),
} }));
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.login).mockResolvedValue({ access_token: "member-token", user: { id: "member" } }); vi.mocked(api.registrationStatus).mockResolvedValue({ registration_available: true, bootstrap_required: false }); vi.mocked(api.register).mockResolvedValue({ access_token: "member-token", user: { id: "member" } }); });
it.each(["login", "register"])("clears previous account content before %s navigation", async (mode) => {
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 client.setQueryData(["agents"], [{ system_prompt: "admin secret" }]);
 client.setQueryData(["skill-description", "skill"], "private markdown");
 mocks.push.mockImplementation(() => {
  expect(client.getQueryData(["agents"])).toBeUndefined();
  expect(client.getQueryData(["skill-description", "skill"])).toBeUndefined();
 });
 render(<QueryClientProvider client={client}><LoginPage /></QueryClientProvider>);
 await screen.findByText("没有账号？注册");
 if (mode === "register") fireEvent.click(screen.getByText("没有账号？注册"));
 fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "member@example.com" } });
 fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password123" } });
 fireEvent.click(screen.getByRole("button", { name: mode === "login" ? /进入工作区/ : "注册并登录" }));
 await waitFor(() => expect(client.getQueryData(["agents"])).toBeUndefined());
 expect(client.getQueryData(["skill-description", "skill"])).toBeUndefined();
 expect(mocks.push).toHaveBeenCalledWith("/workspace");
});

function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><LoginPage /></QueryClientProvider>); }
it("opens first-time workspace creation and submits an organization name", async () => {
 vi.mocked(api.registrationStatus).mockResolvedValue({ registration_available: true, bootstrap_required: true }); mount();
 expect(await screen.findByRole("heading", { name: "创建企业工作空间" })).toBeVisible();
 fireEvent.change(screen.getByLabelText("组织名称"), { target: { value: "示例企业" } });
 fireEvent.change(screen.getByLabelText("管理员姓名"), { target: { value: "张三" } });
 fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "admin@example.com" } });
 fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password123" } });
 fireEvent.click(screen.getByRole("button", { name: "创建工作空间" }));
 await waitFor(() => expect(api.register).toHaveBeenCalledWith("admin@example.com", "password123", "张三", "示例企业"));
});
it("does not guess bootstrap when status fails and offers retry", async () => {
 vi.mocked(api.registrationStatus).mockRejectedValueOnce(new Error("网络错误")); mount();
 expect(await screen.findByRole("alert")).toHaveTextContent("无法读取注册状态");
 expect(screen.queryByLabelText("组织名称")).not.toBeInTheDocument();
 expect(screen.getByRole("button", { name: /进入工作区/ })).toBeEnabled();
 expect(screen.queryByText("没有账号？注册")).not.toBeInTheDocument();
 vi.mocked(api.registrationStatus).mockResolvedValue({ registration_available: false, bootstrap_required: false });
 fireEvent.click(screen.getByRole("button", { name: "重试" }));
 await waitFor(() => expect(screen.getByRole("button", { name: /进入工作区/ })).toBeEnabled());
 expect(screen.queryByText("没有账号？注册")).not.toBeInTheDocument();
});
it("refreshes status after another administrator initializes without losing email", async () => {
 vi.mocked(api.registrationStatus).mockResolvedValueOnce({ registration_available: true, bootstrap_required: true }).mockResolvedValue({ registration_available: false, bootstrap_required: false });
 vi.mocked(api.register).mockRejectedValue(Object.assign(new Error("已初始化"), { status: 409, code: "organization_already_initialized" })); mount();
 await screen.findByLabelText("组织名称");
 fireEvent.change(screen.getByLabelText("组织名称"), { target: { value: "示例企业" } });
 fireEvent.change(screen.getByLabelText("管理员姓名"), { target: { value: "张三" } });
 fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "admin@example.com" } });
 fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password123" } });
 fireEvent.click(screen.getByRole("button", { name: "创建工作空间" }));
 expect(await screen.findByText(/工作空间已由其他管理员创建/)).toBeVisible();
 await waitFor(() => expect(screen.queryByLabelText("组织名称")).not.toBeInTheDocument());
 expect(screen.getByLabelText("邮箱")).toHaveValue("admin@example.com");
});
it("closed registration never exposes workspace creation even on an empty installation", async () => {
 vi.mocked(api.registrationStatus).mockResolvedValue({ registration_available: false, bootstrap_required: true }); mount();
 await waitFor(() => expect(screen.getByRole("button", { name: /进入工作区/ })).toBeEnabled());
 expect(screen.queryByLabelText("组织名称")).not.toBeInTheDocument();
 expect(screen.queryByRole("button", { name: "创建工作空间" })).not.toBeInTheDocument();
});
it("existing users can log in when registration status is unavailable", async () => {
 vi.mocked(api.registrationStatus).mockRejectedValue(new Error("状态服务暂不可用")); mount();
 await screen.findByRole("alert");
 fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "member@example.com" } });
 fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password123" } });
 fireEvent.click(screen.getByRole("button", { name: /进入工作区/ }));
 await waitFor(() => expect(api.login).toHaveBeenCalledWith("member@example.com", "password123"));
 expect(api.register).not.toHaveBeenCalled();
});
it("does not mislabel unrelated registration conflicts as workspace initialization", async () => {
 vi.mocked(api.registrationStatus).mockResolvedValue({ registration_available: true, bootstrap_required: true });
 vi.mocked(api.register).mockRejectedValue(Object.assign(new Error("邮箱已存在"), { status: 409, code: "email_conflict" })); mount();
 fireEvent.change(await screen.findByLabelText("组织名称"), { target: { value: "示例企业" } });
 fireEvent.change(screen.getByLabelText("管理员姓名"), { target: { value: "张三" } });
 fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "admin@example.com" } });
 fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password123" } });
 fireEvent.click(screen.getByRole("button", { name: "创建工作空间" }));
 expect(await screen.findByText("邮箱已存在")).toBeVisible();
 expect(screen.queryByText(/工作空间已由其他管理员创建/)).not.toBeInTheDocument();
 expect(screen.getByLabelText("组织名称")).toHaveValue("示例企业");
});
