import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { MCPConfiguration } from "./MCPConfiguration";
import { api } from "@/lib/api";
vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }));
beforeEach(() => vi.resetAllMocks());
const props = { scopeId: "org", onClose: vi.fn(), onSaved: vi.fn() };
function fill() {
 fireEvent.change(screen.getByLabelText("连接名称"), { target: { value: "reports" } });
 fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "https://example.com/mcp" } });
}
test("creates HTTP connection in organization and tests without invoking tools", async () => {
 vi.mocked(api.post).mockResolvedValue({ ok: true, tools: [{ name: "search" }] });
 render(<MCPConfiguration {...props} />); fill();
 fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
 expect(await screen.findByText("连接成功，发现 1 个工具。")).toBeVisible();
 expect(api.post).toHaveBeenCalledWith("/mcps/probe", { scope_id: "org", configuration: { name: "reports", is_stateful: false, mcp_config: { type: "http_mcp", url: "https://example.com/mcp", timeout: 30, headers: {} } } });
 fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/mcps/custom", expect.objectContaining({ scope_id: "org", name: "reports", headers: {} })));
});
test("rejects credentials in URL without sending request", async () => {
 render(<MCPConfiguration {...props} />); fill();
 fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "https://example.com/mcp?token=secret" } });
 fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
 expect(await screen.findByRole("alert")).toHaveTextContent("地址"); expect(api.post).not.toHaveBeenCalled();
});
test("preserves existing headers by default and never displays raw errors", async () => {
 vi.mocked(api.get).mockResolvedValue({ source: "custom", name: "reports", url: "https://example.com/mcp", timeout: 30, is_stateful: false, header_names: ["Authorization"], configured_fields: [] });
 vi.mocked(api.patch).mockRejectedValue(new Error("secret-token"));
 render(<MCPConfiguration {...props} id="mcp" />);
 await screen.findByDisplayValue("reports");
 fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
 await screen.findByRole("alert");
 expect(api.patch).toHaveBeenCalledWith("/mcps/mcp/custom", expect.not.objectContaining({ headers: expect.anything() }));
 expect(screen.queryByText(/secret-token/)).not.toBeInTheDocument();
});
test("catalog edits submit only entered values", async () => {
 vi.mocked(api.get).mockResolvedValue({ source: "catalog", name: "reports", header_names: [], configured_fields: ["token"], inputs_schema: { properties: { token: { title: "Token", writeOnly: true } }, required: ["token"] } });
 vi.mocked(api.patch).mockResolvedValue({});
 render(<MCPConfiguration {...props} id="mcp" />); await screen.findByLabelText("Token");
 fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
 await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/mcps/mcp", { name: "reports", values: {} }));
});
test("save and test patches changed configuration before probing and keeps the sidebar open", async () => {
 const saved = { id: "mcp", name: "reports", display_name: "新报表", metadata: {} };
 vi.mocked(api.get).mockResolvedValue({ source: "custom", name: "reports", url: "https://example.com/mcp", timeout: 30, is_stateful: false, header_names: ["Authorization"], configured_fields: [] });
 let finishSave!: (value: typeof saved) => void;
 vi.mocked(api.patch).mockImplementation(() => new Promise((resolve) => { finishSave = resolve; }));
 vi.mocked(api.post).mockResolvedValue({ ok: true, tools: [{ name: "search" }] });
 render(<MCPConfiguration {...props} id="mcp" />); await screen.findByDisplayValue("reports");
 fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "https://changed.example/mcp" } });
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "新报表" } });
 fireEvent.click(screen.getByRole("button", { name: "保存并测试" }));
 await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/mcps/mcp/custom", expect.objectContaining({ url: "https://changed.example/mcp", display_name: "新报表" })));
 expect(api.post).not.toHaveBeenCalled();
 expect(props.onSaved).not.toHaveBeenCalled();
 finishSave(saved);
 expect(await screen.findByText("连接成功，发现 1 个工具。")).toBeVisible();
 expect(api.post).toHaveBeenCalledWith("/mcps/mcp/probe", {});
 expect(vi.mocked(api.patch).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.post).mock.invocationCallOrder[0]);
 expect(props.onSaved).toHaveBeenCalledWith(saved, { keepOpen: true });
 expect(props.onClose).not.toHaveBeenCalled();
 expect(screen.getByLabelText("服务地址")).toHaveValue("https://changed.example/mcp");
});
test("save and test never probes if updating the connection fails", async () => {
 vi.mocked(api.get).mockResolvedValue({ source: "custom", name: "reports", url: "https://example.com/mcp", timeout: 30, is_stateful: false, header_names: [], configured_fields: [] });
 vi.mocked(api.patch).mockRejectedValue(new Error("secret-token"));
 render(<MCPConfiguration {...props} id="mcp" />); await screen.findByDisplayValue("reports");
 fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "https://changed.example/mcp" } });
 fireEvent.click(screen.getByRole("button", { name: "保存并测试" }));
 await screen.findByRole("alert");
 expect(api.patch).toHaveBeenCalledTimes(1);
 expect(api.post).not.toHaveBeenCalled();
 expect(props.onSaved).not.toHaveBeenCalled();
 expect(props.onClose).not.toHaveBeenCalled();
 expect(screen.queryByText(/secret-token/)).not.toBeInTheDocument();
});
test("explicit no authentication clears saved credentials", async () => {
 vi.mocked(api.get).mockResolvedValue({ source: "custom", name: "reports", url: "https://example.com/mcp", timeout: 30, is_stateful: false, header_names: ["Authorization"], configured_fields: [] });
 vi.mocked(api.patch).mockResolvedValue({ id: "mcp", name: "reports" });
 render(<MCPConfiguration {...props} id="mcp" />); await screen.findByDisplayValue("reports");
 fireEvent.change(screen.getByLabelText("认证方式"), { target: { value: "none" } });
 fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
 await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/mcps/mcp/custom", expect.objectContaining({ headers: {} })));
});
test("new headers are masked, removable, and sent only in explicit configuration", async () => {
 vi.mocked(api.post).mockResolvedValue({}); render(<MCPConfiguration {...props} />); fill();
 fireEvent.click(screen.getByText("高级选项"));
 fireEvent.change(screen.getByLabelText("认证方式"), { target: { value: "custom" } });
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "报表" } });
 fireEvent.change(screen.getByLabelText("说明"), { target: { value: "报表查询" } });
 fireEvent.change(screen.getByLabelText("超时（秒）"), { target: { value: "60" } });
 fireEvent.click(screen.getByLabelText("保持会话状态"));
 fireEvent.click(screen.getByRole("button", { name: "＋ 添加请求头" }));
 fireEvent.change(screen.getByLabelText("请求头名称 1"), { target: { value: "Authorization" } });
 fireEvent.change(screen.getByLabelText("请求头值 1"), { target: { value: "Bearer secret" } });
 expect(screen.getByLabelText("请求头值 1")).toHaveAttribute("type", "password");
 fireEvent.click(screen.getByRole("button", { name: "＋ 添加请求头" }));
 fireEvent.click(screen.getByRole("button", { name: "移除请求头 2" }));
 fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/mcps/custom", expect.objectContaining({ headers: { Authorization: "Bearer secret" } })));
});
test("configuration read failure shows safe error and no editable form", async () => {
 vi.mocked(api.get).mockRejectedValue(new Error("secret")); render(<MCPConfiguration {...props} id="mcp" />);
 expect(await screen.findByRole("alert")).toHaveTextContent("连接配置加载失败");
 expect(screen.queryByLabelText("连接名称")).not.toBeInTheDocument();
});
test("Bearer authentication is explicit and testing needs no name", async () => {
 vi.mocked(api.post).mockResolvedValue({ ok: false, tools: [], error_code: "authentication_required", error: "secret" });
 render(<MCPConfiguration {...props} />);
 fireEvent.change(screen.getByLabelText("连接名称"), { target: { value: "" } });
 fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "https://example.com/mcp" } });
 fireEvent.change(screen.getByLabelText("认证方式"), { target: { value: "bearer" } });
 fireEvent.change(screen.getByLabelText("Bearer Token"), { target: { value: "private" } });
 fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
 expect(await screen.findByRole("alert")).toHaveTextContent("认证");
 expect(api.post).toHaveBeenCalledWith("/mcps/probe", expect.objectContaining({ configuration: expect.objectContaining({ name: expect.stringMatching(/^mcp-[A-Za-z0-9_-]+$/), mcp_config: expect.objectContaining({ headers: { Authorization: "Bearer private" } }) }) }));
});
test("JSON import only fills form and does not make a request", async () => {
 render(<MCPConfiguration {...props} />);
 fireEvent.click(screen.getByRole("button", { name: "导入 JSON" }));
 fireEvent.change(screen.getByLabelText("JSON 配置"), { target: { value: JSON.stringify({ servers: { reports: { type: "http", url: "https://example.com/mcp", headers: { "X-API-Key": "private" } } } }) } });
 fireEvent.click(screen.getByRole("button", { name: "填入表单" }));
 expect(screen.getByLabelText("连接名称")).toHaveValue("reports");
 expect(screen.getByLabelText("认证方式")).toHaveValue("custom");
 expect(screen.getByLabelText("请求头值 1")).toHaveAttribute("type", "password");
 expect(api.post).not.toHaveBeenCalled();
});
test("API key uses a default header and newly generated name can be saved immediately", async () => {
 const saved = { id: "created-mcp", name: "mcp-created", display_name: "报表" };
 vi.mocked(api.post).mockResolvedValue(saved); render(<MCPConfiguration {...props} />);
 const generatedName = (screen.getByLabelText("连接名称") as HTMLInputElement).value;
 expect(generatedName).toMatch(/^mcp-[A-Za-z0-9_-]+$/);
 expect(screen.getByLabelText("显示名称").closest("details")).toBeNull();
 expect(screen.getByLabelText("说明").closest("details")).toBeNull();
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "报表" } });
 fireEvent.change(screen.getByLabelText("说明"), { target: { value: "企业报表查询" } });
 fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "https://example.com/mcp" } });
 fireEvent.change(screen.getByLabelText("认证方式"), { target: { value: "apiKey" } });
 expect(screen.getByLabelText("API Key 请求头名称")).toHaveValue("X-API-Key");
 fireEvent.change(screen.getByLabelText("API Key", { exact: true }), { target: { value: "private" } });
 fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/mcps/custom", expect.objectContaining({ name: generatedName, display_name: "报表", description: "企业报表查询", headers: { "X-API-Key": "private" } })));
 expect(props.onSaved).toHaveBeenCalledWith(saved);
 expect(props.onClose).toHaveBeenCalledTimes(1);
});
test("local address validation does not silently rewrite target or send request", async () => {
 render(<MCPConfiguration {...props} />);
 fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "http://127.0.0.1:7676/mcp" } });
 fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
 expect(await screen.findByRole("alert")).toHaveTextContent("执行服务器");
 expect(screen.getByLabelText("服务地址")).toHaveValue("http://127.0.0.1:7676/mcp"); expect(api.post).not.toHaveBeenCalled();
});
test("API error code is mapped safely", async () => {
 vi.mocked(api.post).mockRejectedValue(Object.assign(new Error("secret"), { code: "connection_refused" }));
 render(<MCPConfiguration {...props} />); fill();
 fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
 expect(await screen.findByRole("alert")).toHaveTextContent("目标拒绝连接");
 expect(screen.queryByText("secret")).not.toBeInTheDocument();
});
test("catalog save and test publishes entered inputs before probing", async () => {
 const saved = { id: "catalog-mcp", name: "reports" };
 vi.mocked(api.get).mockResolvedValue({ source: "catalog", name: "reports", header_names: [], configured_fields: ["token"], inputs_schema: { properties: { token: { title: "Token", writeOnly: true } }, required: ["token"] } });
 vi.mocked(api.patch).mockResolvedValue(saved);
 vi.mocked(api.post).mockResolvedValue({ ok: true, tools: [] });
 render(<MCPConfiguration {...props} id="catalog-mcp" />);
 fireEvent.change(await screen.findByLabelText("Token"), { target: { value: "new-private-token" } });
 fireEvent.click(screen.getByRole("button", { name: "保存并测试" }));
 expect(await screen.findByText("连接成功，发现 0 个工具。")).toBeVisible();
 expect(api.patch).toHaveBeenCalledWith("/mcps/catalog-mcp", { name: "reports", values: { token: "new-private-token" } });
 expect(api.post).toHaveBeenCalledWith("/mcps/catalog-mcp/probe", {});
 expect(vi.mocked(api.patch).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.post).mock.invocationCallOrder[0]);
 expect(props.onSaved).toHaveBeenCalledWith(saved, { keepOpen: true });
 expect(props.onClose).not.toHaveBeenCalled();
});
test("a failed probe retains the successfully saved item and safe error in the open sidebar", async () => {
 const saved = { id: "mcp", name: "reports", display_name: "新报表" };
 vi.mocked(api.get).mockResolvedValue({ source: "custom", name: "reports", url: "https://example.com/mcp", timeout: 30, is_stateful: false, header_names: [], configured_fields: [] });
 vi.mocked(api.patch).mockResolvedValue(saved);
 vi.mocked(api.post).mockResolvedValue({ ok: false, tools: [], error_code: "authentication_required", error: "secret-token" });
 render(<MCPConfiguration {...props} id="mcp" />); await screen.findByDisplayValue("reports");
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "新报表" } });
 fireEvent.click(screen.getByRole("button", { name: "保存并测试" }));
 expect(await screen.findByRole("alert")).toHaveTextContent("认证");
 expect(props.onSaved).toHaveBeenCalledWith(saved, { keepOpen: true });
 expect(props.onClose).not.toHaveBeenCalled();
 expect(screen.queryByText(/secret-token/)).not.toBeInTheDocument();
});

test("opens custom connection on HTTP origins without randomUUID", () => {
 const getRandomValues = crypto.getRandomValues.bind(crypto);
 vi.stubGlobal("crypto", { getRandomValues });
 try {
  render(<MCPConfiguration {...props} />);
  expect((screen.getByLabelText("连接名称") as HTMLInputElement).value).toMatch(/^mcp-[0-9a-f]{8}$/);
  expect(screen.getByRole("button", { name: "保存连接" })).toBeVisible();
  expect(api.post).not.toHaveBeenCalled();
 } finally {
  vi.unstubAllGlobals();
 }
});
