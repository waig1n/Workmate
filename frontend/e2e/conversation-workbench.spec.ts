import { expect, test, type Page, type Route } from "@playwright/test";
import { resourceResponse } from "./resource-response";

export const conversation = {
  id: "conversation-1",
  agent_id: "agent-1",
  title: "示例工作会话",
  created_at: "2026-08-31T00:00:00Z",
  updated_at: "2026-08-31T00:00:00Z",
  read_only: false,
  read_only_reason: null,
  agent: {
    id: "agent-1",
    slug: "analyst",
    display_name: "企业分析助手",
    system_prompt: "",
    scope_id: "scope-1",
    scope: { id: "scope-1", kind: "organization", name: "企业", parent_id: null, permissions: [] },
    visibility: "private",
    include_descendants: false,
    created_by: "user-1",
    skill_binding_ids: [],
    mcp_binding_ids: [],
    unavailable_capability_ids: [],
    actions: { use: true, update: true, delete: true, publish: true },
    is_default: true,
    created_at: "2026-08-31T00:00:00Z",
    updated_at: "2026-08-31T00:00:00Z",
  },
};

const task = {
  id: "task-1",
  conversation_id: "conversation-1",
  title: "生成报告",
  status: "completed",
  run_id: "reply-1",
  approval_profile: "standard",
  error: null,
  duration_ms: 1200,
  steps: 1,
  model_name: "enterprise-model",
  token_usage: { input_tokens: 10, output_tokens: 20 },
  created_at: "2026-08-31T00:00:00Z",
  updated_at: "2026-08-31T00:00:02Z",
};

const artifact = {
  id: "artifact-1",
  task_id: "task-1",
  conversation_id: "conversation-1",
  name: "report.md",
  mime_type: "text/markdown",
  size: 36,
  version: 1,
  change_type: "created",
  metadata: {},
  created_at: "2026-08-31T00:00:02Z",
};

function json(route: Route, value: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(resourceResponse(route.request().url(), value)) });
}

async function mockProductApi(
  page: Page,
  mode: "completed" | "approval" | "dispatch" = "completed",
) {
  let streamCalls = 0;
  let taskResponse = mode === "approval"
    ? { ...task, status: "awaiting_permission" }
    : task;
  const observed = { upload: false, confirm: false, stop: false };
  await page.addInitScript(() => {
    localStorage.setItem("workmate_token", "e2e-token");
    localStorage.setItem("workmate_user", JSON.stringify({ id: "user-1", email: "user@example.test", display_name: "Example User", status: "active", must_change_password: false }));
    localStorage.setItem("workmate_permissions", JSON.stringify({ permissions: [], personal_scope_id: "personal-1", root_scope_id: "scope-1", is_root_admin: false }));
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/tasks/conversations/conversation-1/events") {
      streamCalls += 1;
      const body = streamCalls === 1 && mode === "approval"
        ? 'event: CUSTOM\nid: boot-1:1\ndata: {"type":"CUSTOM","name":"HITL_APPROVAL_REQUEST","value":{"toolCalls":[{"id":"tool-approval","name":"write","input":"{\\"path\\":\\"output/report.md\\"}"}],"decisionVersion":"decision-v1","taskId":"task-1","approvalProfile":"standard","permissionMode":"accept_edits"}}\n\n'
        : streamCalls === 1 && mode === "completed"
        ? [
            'event: RUN_STARTED\nid: boot-1:1\ndata: {"type":"RUN_STARTED","threadId":"conversation-1","runId":"reply-1"}',
            'event: STEP_STARTED\nid: boot-1:2\ndata: {"type":"STEP_STARTED","stepName":"enterprise-model"}',
            'event: TEXT_MESSAGE_START\nid: boot-1:2a\ndata: {"type":"TEXT_MESSAGE_START","messageId":"progress-1","role":"assistant"}',
            'event: TEXT_MESSAGE_CONTENT\nid: boot-1:2b\ndata: {"type":"TEXT_MESSAGE_CONTENT","messageId":"progress-1","delta":"我先读取资料并确认内容结构。"}',
            'event: TEXT_MESSAGE_END\nid: boot-1:2c\ndata: {"type":"TEXT_MESSAGE_END","messageId":"progress-1"}',
            'event: TOOL_CALL_START\nid: boot-1:3\ndata: {"type":"TOOL_CALL_START","toolCallId":"tool-1","toolCallName":"Read","parentMessageId":"reply-1"}',
            'event: TOOL_CALL_ARGS\nid: boot-1:4\ndata: {"type":"TOOL_CALL_ARGS","toolCallId":"tool-1","delta":"{\\"path\\":\\"input/notes.md\\"}"}',
            'event: TOOL_CALL_RESULT\nid: boot-1:5\ndata: {"type":"TOOL_CALL_RESULT","toolCallId":"tool-1","messageId":"reply-1","content":"会议纪要内容"}',
            'event: TOOL_CALL_START\nid: boot-1:6\ndata: {"type":"TOOL_CALL_START","toolCallId":"tool-bash","toolCallName":"Bash","parentMessageId":"reply-1"}',
            'event: TOOL_CALL_ARGS\nid: boot-1:7\ndata: {"type":"TOOL_CALL_ARGS","toolCallId":"tool-bash","delta":"{\\"command\\":\\"rg --files docs\\"}"}',
            'event: TOOL_CALL_RESULT\nid: boot-1:8\ndata: {"type":"TOOL_CALL_RESULT","toolCallId":"tool-bash","messageId":"reply-1","content":"docs/en/user-guide.md"}',
            'event: TEXT_MESSAGE_START\nid: boot-1:9\ndata: {"type":"TEXT_MESSAGE_START","messageId":"answer-1","role":"assistant"}',
            'event: TEXT_MESSAGE_CONTENT\nid: boot-1:10\ndata: {"type":"TEXT_MESSAGE_CONTENT","messageId":"answer-1","delta":"# 周报\\n\\n| 项目 | 状态 |\\n| --- | --- |\\n| 示例 | 完成 |"}',
            'event: TEXT_MESSAGE_END\nid: boot-1:11\ndata: {"type":"TEXT_MESSAGE_END","messageId":"answer-1"}',
            'event: STEP_FINISHED\nid: boot-1:12\ndata: {"type":"STEP_FINISHED","stepName":"enterprise-model"}',
            'event: RUN_FINISHED\nid: boot-1:13\ndata: {"type":"RUN_FINISHED","threadId":"conversation-1","runId":"reply-1"}',
          ].join("\n\n") + "\n\n"
        : 'event: CUSTOM\nid: boot-1:14\ndata: {"type":"CUSTOM","name":"REPLAY_DONE","value":{"bootId":"boot-1"}}\n\n';
      return route.fulfill({ status: 200, contentType: "text/event-stream", headers: { "cache-control": "no-cache" }, body });
    }
    if (path === "/api/auth/me/permissions") return json(route, { permissions: [], personal_scope_id: "personal-1", root_scope_id: "scope-1", is_root_admin: false });
    if (path === "/api/auth/me") return json(route, { id: "user-1", email: "user@example.test", display_name: "Example User", status: "active", must_change_password: false });
    if (/^\/api\/conversations\/[^/]+\/recovery$/.test(path)) return json(route, { eligible: false, warnings: [] });
    if (path === "/api/conversations/conversation-1/messages") return json(route, []);
    if (path === "/api/conversations/conversation-1") return json(route, conversation);
    if (path === "/api/conversations" && request.method() === "POST") {
      expect(request.postDataJSON().agent_id).toBe("agent-1");
      return json(route, { ...conversation, id: "conversation-new" });
    }
    if (path === "/api/conversations/conversation-new") return json(route, { ...conversation, id: "conversation-new" });
    if (path === "/api/conversations/conversation-new/messages") return json(route, []);
    if (path === "/api/tasks/conversations/conversation-new/events") return route.fulfill({ status: 200, contentType: "text/event-stream", body: 'event: CUSTOM\nid: boot-1:1\ndata: {"type":"CUSTOM","name":"REPLAY_DONE","value":{"bootId":"boot-1"}}\n\n' });
    if (path === "/api/conversations") return json(route, [conversation]);
    if (path === "/api/agents") return json(route, [conversation.agent, { ...conversation.agent, id: "agent-2", display_name: "写作助手", is_default: false }]);
    if (path === "/api/models") return json(route, [{ id: "enterprise-model", provider: "openai", model_name: "enterprise-model", is_default: true, supports_images: true, supports_audio: false, supports_video: false, supports_thinking: true }]);
    if (path === "/api/tasks" && request.method() === "GET") return json(route, [taskResponse]);
    if (path === "/api/tasks/conversations/conversation-1/tasks-with-files" && request.method() === "POST") {
      observed.upload = true;
      taskResponse = { ...task, status: "running" };
      return json(route, { task: taskResponse, queued: false });
    }
    if (path === "/api/tasks/task-1/pending-confirm") return json(route, { toolCalls: [{ id: "tool-approval", name: "write", input: '{"path":"output/report.md"}' }], decisionVersion: "decision-v1", taskId: "task-1", approvalProfile: "standard", permissionMode: "accept_edits" });
    if (path === "/api/tasks/task-1/confirm" && request.method() === "POST") {
      observed.confirm = true;
      taskResponse = { ...taskResponse, status: "running" };
      return json(route, taskResponse);
    }
    if (path === "/api/tasks/task-1/stop" && request.method() === "POST") {
      observed.stop = true;
      taskResponse = { ...taskResponse, status: "cancelling" };
      return json(route, taskResponse);
    }
    if (path === "/api/artifacts" && request.method() === "GET") return json(route, [artifact]);
    if (path === "/api/artifacts/artifact-1/download") return route.fulfill({ status: 200, contentType: "text/markdown", body: "# 交付总结\n\n成果内容安全可预览。" });
    if (path.endsWith('/knowledge-events')) return json(route, []);
    if (path === "/api/tasks/task-1/events") return json(route, [
      { id: 1, task_id: "task-1", seq: 1, event_type: "task_started", payload: {}, created_at: "2026-08-31T00:00:00Z" },
      { id: 2, task_id: "task-1", seq: 2, event_type: "artifact", payload: { name: "report.md" }, created_at: "2026-08-31T00:00:02Z" },
      { id: 3, task_id: "task-1", seq: 3, event_type: "run_finished", payload: { reason: "completed" }, created_at: "2026-08-31T00:00:02Z" },
    ]);
    return json(route, { detail: `unmocked ${request.method()} ${path}` }, 404);
  });
  return observed;
}

test('RAG sources stay compact until explicit expansion, including after reload', async ({ page }, testInfo) => {
  await mockProductApi(page);
  const source = { source_id: 'source-1', knowledge_base_id: 'kb', document_id: 'doc', filename: '制度.pdf', chunk_index: 0, metadata: { page: 2 } };
  let detailRequests = 0;
  await page.route('**/api/tasks/task-1/knowledge-events?**', route => {
    const details = new URL(route.request().url()).searchParams.get('details') === 'true';
    if (details) detailRequests += 1;
    return json(route, [{ id: 1, task_id: 'task-1', seq: 1, event_type: 'knowledge_sources', payload: { sources: [{ ...source, ...(details ? { excerpt: '仅展开后加载的制度片段', score: 1 } : {}) }] }, created_at: task.created_at }]);
  });
  await page.route('**/api/tasks/conversations/conversation-1/events', route => {
    const frames = [
      { type: 'RUN_STARTED', runId: 'reply-1', threadId: conversation.id },
      { type: 'CUSTOM', name: 'knowledge_status', runId: 'reply-1', value: { status: 'running', search_count: 0, failure_count: 0 } },
      { type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '请假需要提前申请。' },
      { type: 'TEXT_MESSAGE_END', messageId: 'answer' },
      { type: 'CUSTOM', name: 'knowledge_sources', runId: 'reply-1', value: { sources: [source] } },
      { type: 'CUSTOM', name: 'knowledge_status', runId: 'reply-1', value: { status: 'completed', search_count: 20, failure_count: 0 } },
      { type: 'RUN_FINISHED', runId: 'reply-1', threadId: conversation.id },
      { type: 'CUSTOM', name: 'REPLAY_DONE', value: {} },
    ];
    return route.fulfill({ contentType: 'text/event-stream', body: frames.map((event, index) => `event: ${event.type}\nid: rag:${index + 1}\ndata: ${JSON.stringify(event)}\n\n`).join('') });
  });
  await page.goto('/workspace/c/conversation-1');
  await expect(page.getByText('请假需要提前申请。')).toBeVisible();
  await expect(page.getByRole('button', { name: '查看检索来源与详情' })).toHaveCount(1);
  expect(detailRequests).toBe(0);
  await expect(page.getByText('仅展开后加载的制度片段')).toHaveCount(0);
  await page.getByRole('button', { name: '查看检索来源与详情' }).click();
  await expect(page.getByText('仅展开后加载的制度片段')).toBeVisible();
  expect(detailRequests).toBe(1);
  await page.reload();
  await expect(page.getByRole('button', { name: '查看检索来源与详情' })).toHaveCount(1);
  await expect(page.getByText('仅展开后加载的制度片段')).toHaveCount(0);
  expect(detailRequests).toBe(1);
  await page.screenshot({ path: testInfo.outputPath('rag-compact.png'), fullPage: true });
});

async function mockLoginAndCreate(page: Page, gates?: { create: Promise<void>; task: Promise<void> }) {
  const observed = { login: false, create: false, sent: false, message: "" };
  const createdConversation = {
    ...conversation,
    id: "conversation-new",
    title: "季度复盘",
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/auth/registration-status") return json(route, { registration_available: false });
    if (path === "/api/auth/login" && request.method() === "POST") {
      observed.login = true;
      return json(route, { access_token: "login-token", user: { id: "user-1", email: "user@example.test", display_name: "Example User", status: "active", must_change_password: false } });
    }
    if (path === "/api/auth/me/permissions") return json(route, { permissions: [], personal_scope_id: "personal-1", root_scope_id: "scope-1", is_root_admin: false });
    if (path === "/api/auth/me") return json(route, { id: "user-1", email: "user@example.test", display_name: "Example User", status: "active", must_change_password: false });
    if (path === "/api/conversations" && request.method() === "POST") {
      observed.create = true;
      await gates?.create;
      return json(route, createdConversation);
    }
    if (path === "/api/conversations") return json(route, []);
    if (path === "/api/conversations/conversation-new") return json(route, createdConversation);
    if (path === "/api/conversations/conversation-new/messages") return json(route, observed.sent ? [{ id: "user-new", role: "user", task_id: "task-new", content: { text: observed.message }, created_at: "2026-09-11T00:00:00Z" }] : []);
    if (path === "/api/agents") return json(route, [conversation.agent]);
    if (path === "/api/models") return json(route, [{ id: "enterprise-model", display_name: "企业模型", is_default: true, available: true }]);
    if (path === "/api/tasks/conversations/conversation-new/tasks" && request.method() === "POST") {
      await gates?.task;
      observed.sent = true;
      observed.message = request.postDataJSON().message;
      expect(request.postDataJSON().approval_profile).toBe("standard");
      return json(route, { task: { ...task, id: "task-new", conversation_id: "conversation-new" }, queued: false });
    }
    if (path === "/api/tasks") return json(route, observed.sent ? [{ ...task, id: "task-new", conversation_id: "conversation-new" }] : []);
    if (path === "/api/artifacts") return json(route, []);
    if (path === "/api/tasks/conversations/conversation-new/events") return route.fulfill({ status: 200, contentType: "text/event-stream", body: 'event: CUSTOM\nid: boot-1:1\ndata: {"type":"CUSTOM","name":"REPLAY_DONE","value":{"bootId":"boot-1"}}\n\n' });
    return json(route, { detail: `unmocked ${request.method()} ${path}` }, 404);
  });
  return observed;
}

test("logs in and creates a governed conversation", async ({ page }) => {
  let releaseCreate!: () => void;
  let releaseTask!: () => void;
  const gates = { create: new Promise<void>((resolve) => { releaseCreate = resolve; }), task: new Promise<void>((resolve) => { releaseTask = resolve; }) };
  const observed = await mockLoginAndCreate(page, gates);
  await page.addInitScript(() => localStorage.setItem("workmate_approval_profile", "full_access"));
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("user@example.test");
  await page.getByLabel("密码").fill("correct-password");
  await page.getByRole("button", { name: /进入工作区/ }).click();
  await expect.poll(() => observed.login).toBe(true);
  await expect(page).toHaveURL(/\/workspace$/);
  await expect(page.getByRole("combobox", { name: "当前智能体" })).toHaveValue("agent-1");
  await page.getByRole("button", { name: "新建对话", exact: true }).first().click();
  expect(observed.create).toBe(false);
  await expect(page).toHaveURL(/\/workspace$/);
  await expect(page.getByRole("heading", { name: "今天，我能帮你做些什么？" })).toBeVisible();
  const input = page.getByRole("textbox", { name: "任务描述" });
  const model = page.getByRole("combobox", { name: "对话模型" });
  await expect(model).toBeVisible();
  const inputBox = await input.boundingBox();
  const modelBox = await model.boundingBox();
  expect(modelBox!.y).toBeGreaterThan(inputBox!.y + inputBox!.height);
  expect(inputBox!.y).toBeLessThan(500);
  await expect(page).toHaveScreenshot("conversation-empty.png", { animations: "disabled", fullPage: true });
  await input.click();
  await expect(page).toHaveScreenshot("conversation-input-focused.png", { animations: "disabled", fullPage: true });
  await model.click();
  await expect(page).toHaveScreenshot("conversation-model-picker.png", { animations: "disabled", fullPage: true });
  await page.keyboard.press("Escape");
  await expect(page.getByRole("combobox", { name: "下一任务审批等级" })).toHaveValue("standard");
  await input.fill("帮我准备季度复盘");
  expect(observed.create).toBe(false);
  await page.getByRole("button", { name: "发送任务" }).click();
  await expect(page).toHaveURL(/\/workspace\/pending\//);
  await expect(page.getByText("帮我准备季度复盘", { exact: true })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "工作中…" })).toBeVisible();
  expect(observed.sent).toBe(false);
  releaseCreate();
  await expect(page.getByRole("status").filter({ hasText: "工作中…" })).toBeVisible();
  releaseTask();
  await expect.poll(() => observed.sent).toBe(true);
  await expect(page).toHaveURL(/\/workspace\/c\/conversation-new$/, { timeout: 30_000 });
  await expect(page.getByRole("combobox", { name: "下一任务审批等级" })).toHaveValue("standard");
  await expect(page.getByText("帮我准备季度复盘", { exact: true })).toBeVisible();
});

test("opens a centered draft and preserves drafts when switching agents", async ({ page }) => {
  const browserErrors: string[] = [];
  page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.clock.setFixedTime(new Date("2026-09-07T15:00:00+08:00"));
  await mockProductApi(page);
  await page.goto("/workspace");
  await expect(page.getByText("正在与「企业分析助手」对话")).toBeVisible();
  await expect(page.getByRole("link", { name: /示例工作会话/ }).first()).toBeVisible();
  expect(browserErrors).toEqual([]);
  await expect(page).toHaveScreenshot("workspace-overview.png", { animations: "disabled", fullPage: true });
  await page.keyboard.press("Control+k");
  await expect(page.getByPlaceholder("搜索最近对话")).toBeFocused();
  await page.getByRole("button", { name: "分析数据与风险" }).click();
  await expect(page).toHaveURL(/\/workspace$/);
  const input = page.getByRole("textbox", { name: "任务描述" });
  await expect(input).toHaveValue("分析数据与风险");
  await page.getByRole("combobox", { name: "当前智能体" }).selectOption("agent-2");
  await expect(input).toHaveValue("");
  await input.fill("另一位助手的草稿");
  await page.getByRole("combobox", { name: "当前智能体" }).selectOption("agent-1");
  await expect(input).toHaveValue("分析数据与风险");
  await page.getByRole("link", { name: /示例工作会话/ }).first().click();
  await expect(page.getByRole("heading", { name: "周报" })).toBeVisible();
  await input.fill("历史对话未发送的内容");
  await page.locator('input[type="file"]').setInputFiles({ name: "draft.txt", mimeType: "text/plain", buffer: Buffer.from("draft") });
  await page.getByRole("combobox", { name: "当前智能体" }).selectOption("agent-2");
  await expect(page).toHaveURL(/\/workspace$/);
  await expect(input).toHaveValue("另一位助手的草稿");
  await page.getByRole("link", { name: /示例工作会话/ }).first().click();
  await expect(input).toHaveValue("历史对话未发送的内容");
  await expect(page.getByRole("button", { name: "移除 draft.txt" })).toBeVisible();
  expect(browserErrors).toEqual([]);
});

test("renders streamed Markdown and previews a governed artifact", async ({ page }) => {
  await mockProductApi(page);
  await page.goto("/workspace/c/conversation-1");
  await expect(page.getByRole("heading", { name: "周报" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("table")).toContainText("示例");
  // Execution collapses into one aggregated summary line, not tool cards.
  await expect(page.getByText("已完成")).toBeVisible();
  await expect(page.getByText(/阅读 1 个文件/)).toBeVisible();
  await expect(page.getByText(/生成 1 个成果/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "打开成果 report.md", exact: true })).toHaveCount(1);
  await expect(page.getByText("我先读取资料并确认内容结构。")).toBeVisible();
  const runSummary = page.getByRole("button", { name: /已完成/ }).first();
  await runSummary.click();
  await expect(page.getByText("我先读取资料并确认内容结构。")).toBeVisible();
  await expect(page.getByText("读取文件", { exact: true })).toBeVisible();
  await expect(page.getByText("执行命令", { exact: true })).toBeVisible();
  await runSummary.click();
  await expect(page.getByText("我先读取资料并确认内容结构。")).toBeVisible();
  await expect(page.getByText("成功")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "打开成果 report.md" })).toBeVisible();
  await expect(page).toHaveScreenshot("conversation-desktop.png", { animations: "disabled", fullPage: true });
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 report.md", exact: true }).click();
  expect((await downloaded).suggestedFilename()).toBe("report.md");
  // REST hydration must not steal focus by opening the sidecar on refresh.
  await expect(page.getByRole("dialog", { name: "会话工作台侧栏" })).toHaveCount(0);
  await page.getByRole("button", { name: "打开成果 report.md" }).click();
  await expect(page).toHaveURL(/artifact=artifact-1/);
  await expect(page.getByRole("heading", { name: "交付总结" })).toBeVisible();
  const sidecar = page.getByRole("dialog", { name: "会话工作台侧栏" });
  const resizeHandle = page.getByRole("separator", { name: "调整成果栏宽度" });
  await resizeHandle.hover();
  const widthBefore = (await sidecar.boundingBox())?.width ?? 0;
  const handleBox = await resizeHandle.boundingBox();
  expect(handleBox).not.toBeNull();
  await page.mouse.move(handleBox!.x + handleBox!.width / 2, handleBox!.y + 180);
  await page.mouse.down();
  await page.mouse.move(handleBox!.x - 60, handleBox!.y + 180, { steps: 4 });
  await page.mouse.up();
  await expect.poll(async () => (await sidecar.boundingBox())?.width ?? 0).toBeGreaterThan(widthBefore);
  const expandedHandle = await resizeHandle.boundingBox();
  const halfScreen = Math.floor(page.viewportSize()!.width / 2);
  await page.mouse.move(expandedHandle!.x + expandedHandle!.width / 2, expandedHandle!.y + 180);
  await page.mouse.down();
  await page.mouse.move(expandedHandle!.x - halfScreen, expandedHandle!.y + 180, { steps: 6 });
  await page.mouse.up();
  await expect.poll(async () => (await sidecar.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(halfScreen - 1);
  await expect(resizeHandle).toHaveAttribute("aria-valuemax", String(halfScreen));
  await resizeHandle.dblclick();
  await page.getByRole("button", { name: "执行活动" }).click();
  await expect(page.getByText("任务已创建")).toBeVisible();
  await expect(page.getByText("生成成果")).toBeVisible();
  // Tool-call details live in the Activity panel, collapsed by default.
  await expect(page.getByText("本次执行的工具调用")).toBeVisible();
  await page.getByRole("button", { name: /本次执行的工具调用/ }).click();
  await expect(page.getByText("Read", { exact: true })).toBeVisible();
  await expect(page.getByText("rg --files docs", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /Bash/ }).click();
  await expect(page.getByText("docs/en/user-guide.md")).toBeVisible();
});

test("opens the artifact workbench as a mobile drawer", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockProductApi(page);
  await page.goto("/workspace/c/conversation-1");
  await expect(page.getByRole("heading", { name: "周报" })).toBeVisible({ timeout: 15_000 });
  await expect(page).toHaveScreenshot("conversation-mobile.png", { animations: "disabled", fullPage: true });
  await page.setViewportSize({ width: 390, height: 500 });
  const messages = page.getByLabel("对话消息", { exact: true });
  await messages.evaluate((element) => { element.scrollTop = 0; element.dispatchEvent(new Event("scroll")); });
  await expect(page.getByRole("button", { name: "回到最新", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "回到最新", exact: true }).click();
  await expect.poll(() => messages.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThan(80);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "打开成果（1）" }).click();
  await expect(page.getByText("交付总结")).toBeVisible();
  await page.getByRole("button", { name: "关闭侧栏" }).last().click();
  await expect(page.getByText("交付总结")).not.toBeVisible();
});

test("keeps enterprise approval and stop controls actionable", async ({ page }) => {
  const observed = await mockProductApi(page, "approval");
  await page.goto("/workspace/c/conversation-1");
  await expect(page.getByText("需要你确认")).toBeVisible();
  const approvalTray = page.getByRole("region", { name: "待审批操作" });
  const composer = page.getByRole("textbox", { name: "任务描述" });
  const trayBox = await approvalTray.boundingBox();
  const composerBox = await composer.boundingBox();
  expect(trayBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  expect(trayBox!.y + trayBox!.height).toBeLessThanOrEqual(composerBox!.y);
  await expect(page.getByLabel("对话消息", { exact: true }).getByRole("region", { name: "待审批操作" })).toBeVisible();
  await expect(page.getByRole("status", { name: "智能体执行状态" })).toHaveCount(0);
  await expect(page).toHaveScreenshot("conversation-approval.png", { animations: "disabled", fullPage: true });
  // The card explains itself with the task's frozen approval profile.
  await page.getByText("查看操作详情", { exact: true }).click();
  await expect(page.getByText("本任务：重要操作询问", { exact: true })).toBeVisible();
  await page.route("**/api/tasks/task-1/confirm", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "审批暂时不可用" }) }), { times: 1 });
  await page.getByRole("button", { name: "允许本次", exact: true }).click();
  await expect(approvalTray.getByRole("alert")).toContainText("提交失败");
  await expect(page.getByText("任务执行失败", { exact: true })).toHaveCount(0);
  await expect(approvalTray.getByRole("button", { name: "允许本次", exact: true })).toBeEnabled();
  await approvalTray.getByRole("button", { name: "允许本次", exact: true }).click();
  await expect.poll(() => observed.confirm).toBe(true);
  await expect(page.getByText("需要你确认")).toHaveCount(0);
  // The finite mocked SSE response may already be reconnecting; neither state is terminal.
  await expect(page.getByRole("status", { name: "智能体执行状态" })).toHaveText(/^(工作中|连接中断，正在恢复…)$/);
  await expect(page.locator("form").getByRole("status", { name: "智能体执行状态" })).toHaveCount(0);
  await expect(page.getByRole("status", { name: "智能体执行状态" })).toBeVisible();
  await expect(page).toHaveScreenshot("conversation-running.png", { animations: "disabled", fullPage: true });
  await composer.fill("下一项任务");
  await expect(page.getByRole("button", { name: "发送任务", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "停止执行", exact: true })).toBeVisible();
  await composer.fill("");
  await page.getByRole("button", { name: "停止执行", exact: true }).click();
  await expect.poll(() => observed.stop).toBe(true);
});

test("uploads an attachment and exposes reliable dispatch feedback", async ({ page }) => {
  const observed = await mockProductApi(page, "dispatch");
  await page.goto("/workspace/c/conversation-1");
  await page.locator('input[type="file"]').setInputFiles({
    name: "brief.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# Brief"),
  });
  await expect(page.getByText("brief.md")).toBeVisible();
  await page.getByRole("textbox", { name: "任务描述" }).fill("分析附件");
  await page.getByRole("button", { name: "发送任务" }).click();
  await expect.poll(() => observed.upload).toBe(true);
  // The finite mocked SSE response may already be reconnecting; neither state is terminal.
  await expect(page.getByRole("status", { name: "智能体执行状态" })).toHaveText(/^(工作中|连接中断，正在恢复…)$/);
  await expect(page.getByRole("status", { name: "仍在运行" })).toBeVisible();
  // While the task runs, the composer distinguishes the frozen profile
  // from the still-editable next-task approval selector.
  await expect(page.getByText(/当前任务：重要操作询问/).first()).toBeVisible();
  await expect(page.getByRole("combobox", { name: "下一任务审批等级" })).toBeVisible();
});

test("ordinary users browse the agent catalog and create a personal agent", async ({ page }) => {
  await mockProductApi(page);
  const personal = { id: "personal-1", name: "个人空间", kind: "personal", permissions: [], parent_id: null };
  const shared = { ...conversation.agent, visibility: "public", description: "分析业务数据，整理资料并生成报告。", actions: { use: true, update: false, delete: false, publish: false } };
  let created: Record<string, unknown> | null = null;
  await page.route("**/api/scopes/accessible", (route) => json(route, [personal, conversation.agent.scope]));
  await page.route("**/api/agents", async (route) => {
    if (route.request().method() === "POST") {
      created = route.request().postDataJSON();
      return json(route, { ...shared, ...created, id: "personal-agent", scope: personal, scope_id: personal.id, actions: { use: true, update: true, delete: true, publish: false } }, 201);
    }
    const items = created ? [shared, { ...shared, ...created, id: "personal-agent", scope: personal, scope_id: personal.id, actions: { use: true, update: true, delete: true, publish: false } }] : [shared];
    return json(route, resourceResponse(route.request().url(), items));
  });
  await page.route("**/api/agents?*", (route) => {
    const items = created ? [shared, { ...shared, ...created, id: "personal-agent", scope: personal, scope_id: personal.id, actions: { use: true, update: true, delete: true, publish: false } }] : [shared];
    return json(route, resourceResponse(route.request().url(), items));
  });
  await page.route("**/api/agents/agent-1", (route) => json(route, shared));
  await page.route("**/api/sharing/agents/agent-1/access", (route) => json(route, {
    can_use: true, allow_download: false, sources: [{ target_scope_id: "scope-1", target_name: "企业", target_kind: "organization", include_descendants: true, inherited: false, allow_download: false }],
  }));
  await page.route("**/api/agents/personal-agent", (route) => json(route, {
    ...shared,
    ...created,
    id: "personal-agent",
    scope: personal,
    scope_id: personal.id,
    actions: { use: true, update: true, delete: true, publish: false },
  }));
  await page.goto("/workspace/agents");
  await expect(page.getByRole("button", { name: "创建个人智能体", exact: true })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page).toHaveScreenshot("agents-catalog.png", { animations: "disabled", fullPage: true });
  await page.getByRole("button", { name: /企业分析助手.*分析业务/ }).click();
  await expect(page.getByRole("dialog").getByRole("button", { name: "编辑", exact: true })).toHaveCount(0);
  await expect(page.getByRole("dialog").getByText("已获得使用授权")).toBeVisible();
  await expect(page.getByRole("dialog").getByText("正在校验详情权限…")).toHaveCount(0);
  await expect(page).toHaveScreenshot("agent-details.png", { animations: "disabled", fullPage: true });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "创建个人智能体", exact: true }).click();
  await page.getByLabel("显示名称").fill("我的写作助手");
  await page.getByLabel("简介说明").fill("帮助整理素材并撰写周报");
  await page.getByRole("button", { name: "创建智能体", exact: true }).click();
  await expect.poll(() => created?.display_name).toBe("我的写作助手");
  expect(created).toMatchObject({ visibility: "private", platform_tool_profile: "none" });
  expect(created).not.toHaveProperty("scope_id");
  await expect(page.getByRole("dialog").getByRole("button", { name: "编辑", exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page).toHaveScreenshot("agent-details-mobile.png", { animations: "disabled", fullPage: true });
});

test("user access editing separates department drafts from saved permissions", async ({ page }) => {
  await mockProductApi(page);
  const root = { id: "root", kind: "organization", name: "总公司", parent_id: null, permissions: ["membership.manage", "role.bind"], children: [{ id: "dept", kind: "department", name: "研发部", parent_id: "root", permissions: [], children: [] }] };
  const user = { id: "employee", display_name: "张明", email: "zhang@example.com", status: "active", must_change_password: false, access_version: 1, profile_version: 1, departments: [], roles: [], memberships: ["root"], role_bindings: [], root_scope_id: "root" };
  await page.route("**/api/auth/me/permissions", (route) => json(route, { personal_scope_id: "personal-1", root_scope_id: "root", permissions: [], root_permissions: ["user.read", "user.manage", "role.bind", "membership.manage"], is_root_admin: true }));
  await page.route("**/api/admin/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/assignable-roles")) return json(route, [{ id: "editor", key: "editor", display_name: "智能体编辑者", permission_keys: ["agent.create", "agent.update"] }]);
    if (path.endsWith("/effective-access")) return json(route, { permissions: [], sources: [], resource_access_note: "资源使用仍遵循成员关系与共享范围。" });
    if (path === "/api/admin/scopes/tree") return json(route, [root]);
    if (path === "/api/admin/users/employee") return json(route, user);
    return json(route, { items: [user], next_cursor: null });
  });
  await page.goto("/workspace/users?user=employee");
  await page.getByRole("button", { name: "部门与职责", exact: true }).click();
  await expect(page.getByRole("group", { name: "所属部门", exact: true })).toBeVisible();
  await page.getByLabel("总公司 / 研发部", { exact: true }).check();
  await page.getByRole("button", { name: "新增管理职责" }).click();
  await page.getByLabel("生效范围", { exact: true }).selectOption("dept");
  await page.getByRole("button", { name: "加入待保存列表" }).click();
  await expect(page.getByText("有待保存的部门或职责修改")).toBeVisible();
  await expect(page).toHaveScreenshot("user-access-editor.png", { animations: "disabled", fullPage: true });
  await page.getByRole("button", { name: "当前权限", exact: true }).click();
  await expect(page.getByText("部门与职责还有未保存修改。此处显示已保存的权限，不包含草稿。")).toBeVisible();
  await expect(page.getByText("已授予的权限（0）")).toBeVisible();
});

test("management pages keep permission explanations collapsed", async ({ page }) => {
  await mockProductApi(page);
  const permissions = ["role.read", "role.manage", "scope.manage", "membership.read", "membership.manage", "role.bind"];
  const root = { id: "root", kind: "organization", name: "总公司", parent_id: null, permissions, children: [] };
  await page.route("**/api/auth/me/permissions", (route) => json(route, { root_scope_id: "root", personal_scope_id: "personal-1", permissions, root_permissions: permissions, managed_scope_ids: ["root"] }));
  await page.route("**/api/admin/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/roles")) return json(route, [{ id: "editor", key: "editor", display_name: "智能体编辑者", description: "创建和维护团队智能体", permission_keys: ["agent.create", "agent.update"], is_system: false, delegatable: true, user_count: 3, binding_count: 3, version: 1 }]);
    if (path.endsWith("/permissions")) return json(route, ["create", "update", "delete", "publish"].map((action, i) => ({ id: action, key: `agent.${action}`, display_name: ["创建智能体", "编辑智能体", "删除智能体", "发布智能体"][i], enforcement: "rbac", applicable_scope: "all", is_system: true, description: "适用于授权范围内的智能体" })));
    if (path.endsWith("/role-templates")) return json(route, []);
    if (path.endsWith("/scopes/tree")) return json(route, [root]);
    if (path.endsWith("/members")) return json(route, { items: [{ id: "employee", display_name: "张明", email: "zhang@example.com", status: "active", membership_scope_ids: ["root"] }], next_cursor: null, counts: { direct: 1, total: 1, limited: false } });
    return json(route, { items: [], next_cursor: null });
  });
  await page.goto("/workspace/roles");
  await page.getByRole("button", { name: "编辑权限", exact: true }).click();
  await expect(page.getByRole("button", { name: "显示权限说明" })).toHaveAttribute("aria-expanded", "false");
  await expect(page).toHaveScreenshot("role-permissions-simple.png", { animations: "disabled", fullPage: true });
  await page.getByRole("button", { name: "显示权限说明" }).click();
  await expect(page.getByRole("dialog").getByText("agent.create", { exact: true })).toBeVisible();
  await page.goto("/workspace/scopes");
  await page.getByRole("button", { name: "总公司", exact: true }).click();
  await expect(page.getByText("zhang@example.com", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "未分配部门", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "移出部门", exact: true })).toHaveCount(0);
  await expect(page).toHaveScreenshot("department-simple.png", { animations: "disabled", fullPage: true });
});

test("department sharing previews skills and revokes only the agent", async ({ page }, testInfo) => {
  await mockProductApi(page);
  await page.route("**/api/auth/me/permissions", (route) => json(route, { permissions: ["agent.update", "agent.publish"], personal_scope_id: "personal-1", root_scope_id: "scope-1" }));
  await page.route("**/api/scopes/accessible", (route) => json(route, [conversation.agent.scope]));
  await page.route("**/api/agents/capability-candidates?*", (route) => json(route, []));
  await page.route(/\/api\/(skills|mcps|knowledge-bases)(?:\?.*)?$/, (route) => json(route, []));
  await page.route("**/api/agents", (route) => json(
    route,
    resourceResponse(route.request().url(), [conversation.agent]),
  ));
  let visibility = "private";
  const sharedScopes: string[] = [];
  let submitted: Record<string, unknown> | null = null;
  const plan = { resource_id: "agent-1", name: "企业分析助手", target_scope_id: "department-b", target_name: "运营部", skills: [{ id: "skill-1", name: "报告整理", scope_id: "scope-1" }], blockers: [], confirmation: "preview-version" };
  await page.route("**/api/agents?*", (route) => json(
    route,
    resourceResponse(route.request().url(), [conversation.agent]),
  ));
  await page.route("**/api/agents/agent-1", (route) => json(route, { ...conversation.agent, visibility }));
  await page.route("**/api/sharing/**", async (route) => {
    if (route.request().method() === "DELETE") {
      expect(new URL(route.request().url()).pathname).toBe("/api/sharing/agents/agent-1/department-b");
      sharedScopes.splice(0);
      return route.fulfill({ status: 204 });
    }
    if (new URL(route.request().url()).pathname.endsWith("/preview")) return json(route, plan);
    if (route.request().method() === "POST") { submitted = route.request().postDataJSON(); visibility = "public"; sharedScopes.push("department-b"); return json(route, plan); }
    return json(route, { targets: [{ id: "department-b", name: "运营部" }], shared_scope_ids: sharedScopes });
  });
  await page.goto("/workspace/agents");
  await page.getByRole("button", { name: /企业分析助手/ }).first().click();
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await page.getByRole("button", { name: "发布并设置使用范围", exact: true }).click();
  await page.getByLabel("运营部", { exact: true }).check();
  await page.getByRole("button", { name: "发布并设置使用范围", exact: true }).click();
  await expect(page.getByText("同时共享技能：报告整理")).toBeVisible();
  expect(submitted).toBeNull();
  await page.screenshot({ path: testInfo.outputPath("sharing-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText("同时共享技能：报告整理")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("sharing-mobile.png"), fullPage: true });
  await page.getByRole("button", { name: "确认共享", exact: true }).click();
  await expect.poll(() => submitted).toEqual({ targets: [{ target_scope_id: "department-b", include_descendants: false, confirmation: "preview-version" }] });
  await page.getByRole("button", { name: "共享", exact: true }).click();
  await page.getByRole("button", { name: "取消共享", exact: true }).click();
  await expect(page.getByText(/已共享的技能保持共享/)).toBeVisible();
  expect(sharedScopes).toEqual(["department-b"]);
  await page.getByRole("button", { name: "确认取消共享", exact: true }).click();
  await expect(page.getByRole("button", { name: "取消共享", exact: true })).toHaveCount(0);
  expect(sharedScopes).toEqual([]);
});

test("shared Artifact revision survives task history refresh and downloads as v1", async ({ page }) => {
  await mockProductApi(page);
  await page.route("**/api/tasks/conversations/conversation-1/events", (route) => route.fulfill({
    status: 200, contentType: "text/event-stream", body: 'event: CUSTOM\ndata: {"type":"CUSTOM","name":"REPLAY_DONE","value":{}}\n\n',
  }));
  await page.route("**/api/tasks?*", (route) => json(route, [task, { ...task, id: "task-2", run_id: "reply-2" }]));
  await page.route("**/api/conversations/conversation-1/messages*", (route) => json(route, [
    { id: "answer-1", task_id: "task-1", role: "assistant", content: { text: "MD 文件已完成" }, created_at: artifact.created_at },
    { id: "answer-2", task_id: "task-2", role: "assistant", content: { text: "PDF 已生成，附上原 MD" }, created_at: "2026-08-31T00:01:00Z" },
  ]));
  await page.route("**/api/artifacts?*", (route) => json(route, [
    { ...artifact, task_id: "task-2", change_type: "presented", created_at: "2026-08-31T00:01:00Z" }, artifact,
  ]));
  await page.goto("/workspace/c/conversation-1");
  for (let visit = 0; visit < 2; visit += 1) {
    await expect(page.getByRole("button", { name: "打开成果 report.md", exact: true })).toHaveCount(2);
    await page.getByRole("button", { name: "打开成果 report.md", exact: true }).last().click();
    await expect(page.getByRole("heading", { name: "交付总结" })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "成果版本" })).toHaveCount(0);
    await expect(page.getByText(/v2/)).toHaveCount(0);
    const downloaded = page.waitForEvent("download");
    await page.getByRole("button", { name: "下载 report.md", exact: true }).last().click();
    expect((await downloaded).suggestedFilename()).toBe("report.md");
    if (visit === 0) await page.reload();
  }
});

for (const managed of [false, true]) {
  test(`usage dashboard ${managed ? "managed" : "personal"} controls`, async ({ page }) => {
    await mockProductApi(page);
    const summary = { task_count: 12, recorded_tasks: 12, input_tokens: 12500, output_tokens: 3500,
      unpriced_tasks: 0, costs: [{ currency: "CNY", amount: "0.32" }] };
    await page.route("**/api/usage**", (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/usage/scopes") return json(route, managed ? [{ id: "org", name: "研发组织", kind: "organization" }] : []);
      if (url.pathname === "/api/usage/options") return json(route, { users: [{ id: "u1", name: "张明" }], models: [{ id: "m1", name: "工作模型" }], agents: [{ id: "a1", name: "工作助手" }] });
      return json(route, { summary, daily: [{ date: "2026-09-22", ...summary }], groups: [{ id: "g1", name: url.searchParams.get("group_by") === "user" ? "张明" : "工作模型", ...summary }], total_groups: 1, limit: 50, offset: 0 });
    });
    await page.goto("/workspace/usage");
    await expect(page.getByRole("heading", { name: managed ? "用量与成本" : "我的用量", exact: true })).toBeVisible();
    await expect(page.getByLabel("分组方式")).toHaveValue("model");
    await expect(page.getByLabel("成员", { exact: true })).toHaveCount(0);
    if (managed) {
      await page.getByLabel("统计范围").selectOption("org");
      await expect(page.getByLabel("成员", { exact: true })).toBeVisible();
      await expect(page.getByLabel("分组方式")).toHaveValue("user");
      await expect(page.getByText("张明", { exact: true }).last()).toBeVisible();
    } else {
      await expect(page.getByLabel("统计范围")).toHaveCount(0);
      await expect(page.getByRole("option", { name: "按成员" })).toHaveCount(0);
    }
    await page.screenshot({ path: `/tmp/usage-${managed ? "managed" : "personal"}.png`, fullPage: true });
    if (managed) {
      await page.getByLabel("统计范围").selectOption("");
      await expect(page.getByLabel("分组方式")).toHaveValue("model");
      await expect(page.getByLabel("成员", { exact: true })).toHaveCount(0);
    }
  });
}


test("interactive HTML charts use local libraries and isolate workbench identity", async ({ page }) => {
  await mockProductApi(page);
  const requests: string[] = [];
  const popups: string[] = [];
  page.on("popup", popup => popups.push(popup.url()));
  await page.route("**/blocked-preview/**", route => { requests.push(route.request().url()); return route.abort(); });
  const source = `<!doctype html><html lang="zh"><head>
    <meta http-equiv="refresh" content="0;url=https://invalid.test/blocked-preview/refresh">
    <style>@import url('https://invalid.test/blocked-preview/import');
      body {font-family:sans-serif;background:#f7f8fc} h1 {color:#303666}
      .card {background:white;border-radius:12px;padding:18px;margin-bottom:16px}
      button {background:#4f46e5;color:white;border:0;padding:10px 18px;border-radius:8px}
    </style>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head><body class="report" style="padding:16px">
    <h1>销售趋势</h1><div class="card"><div id="echart" style="height:240px"></div></div>
    <div class="card"><canvas id="chart" width="320" height="160"></canvas></div>
    <button onclick="updateCharts()">更新数据</button>
    <img src="https://invalid.test/blocked-preview/image">
    <a href="https://invalid.test/blocked-preview/link" target="_top">外部链接</a>
    <form action="https://invalid.test/blocked-preview/form"><input value="example"></form>
    <script>
      const ec = echarts.init(document.getElementById('echart'),null,{renderer:'svg'});
      ec.setOption({animation:false,xAxis:{type:'category',data:['一月','二月','三月']},yAxis:{},series:[{type:'bar',data:[12,24,18]}]});
      const cc = new Chart(document.getElementById('chart'),{type:'line',data:{labels:['一月','二月','三月'],datasets:[{label:'销售额',data:[12,24,18],borderColor:'#4f46e5'}]},options:{animation:false,responsive:false}});
      function updateCharts(){ec.setOption({series:[{data:[20,32,28]}]});cc.data.datasets[0].data=[20,32,28];cc.update();document.body.dataset.updated='yes'}
      try { parent.document.body.dataset.compromised='yes'; } catch { document.body.dataset.parentBlocked='yes'; }
      try { localStorage.setItem('token','bad'); } catch { document.body.dataset.storageBlocked='yes'; }
      fetch('https://invalid.test/blocked-preview/fetch').catch(()=>document.body.dataset.fetchBlocked='yes');
      window.open('https://invalid.test/blocked-preview/popup','_blank');
      document.body.dataset.ready='yes';
    </script></body></html>`;
  await page.route("**/api/artifacts?*", route => json(route, [{ ...artifact, name: "report.html", mime_type: "text/html", size: source.length }]));
  await page.route("**/api/artifacts/artifact-1/download?*", route => {
    expect(route.request().headers().authorization).toBe("Bearer e2e-token");
    return route.fulfill({ status: 200, contentType: "text/html", headers: { "content-disposition": 'attachment; filename="report.html"' }, body: source });
  });
  await page.goto("/workspace/c/conversation-1");
  await page.getByRole("button", { name: "打开成果 report.html", exact: true }).click();
  const iframe = page.getByTitle("report.html 交互预览");
  await expect(iframe).toHaveAttribute("sandbox", "allow-scripts");
  const frame = page.frameLocator('iframe[title="report.html 交互预览"]');
  await expect(frame.locator("body")).toHaveAttribute("data-ready", "yes");
  await expect(frame.locator("body")).toHaveClass("report");
  await expect(frame.locator("body")).toHaveCSS("padding", "16px");
  await expect(frame.locator("#echart svg")).toBeVisible();
  expect(await frame.locator("canvas").evaluate(canvas => {
    const ctx = (canvas as HTMLCanvasElement).getContext("2d")!;
    return ctx.getImageData(0,0,320,160).data.some(value => value !== 0);
  })).toBe(true);
  await frame.getByRole("button", { name: "更新数据" }).click();
  await expect(frame.locator("body")).toHaveAttribute("data-updated", "yes");
  for (const name of ["parent", "storage", "fetch"]) await expect(frame.locator("body")).toHaveAttribute(`data-${name}-blocked`, "yes");
  await expect(page.locator("body")).not.toHaveAttribute("data-compromised", "yes");
  expect(await page.evaluate(() => localStorage.getItem("workmate_token"))).toBe("e2e-token");
  await frame.getByText("外部链接").click();
  expect(requests).toEqual([]);
  expect(popups).toEqual([]);
  const sidecar = page.getByRole("dialog", { name: "会话工作台侧栏" });
  await sidecar.getByRole("button", { name: "源码", exact: true }).click();
  expect(await sidecar.locator("pre").textContent()).toBe(source);
  await expect(iframe).toHaveCount(0);
  await sidecar.getByRole("button", { name: "预览", exact: true }).click();
  await expect(frame.locator("body")).toHaveAttribute("data-ready", "yes");
  await page.screenshot({ path: "/tmp/html-interactive-preview.png", fullPage: true });
  // Document the accepted browser boundary: self-navigation is possible even
  // though fetch/images/imports are blocked. Intercept it; never contact a host.
  await frame.locator("body").evaluate(() => { location.href = 'https://invalid.test/blocked-preview/self-navigation'; });
  await expect.poll(() => requests).toEqual(['https://invalid.test/blocked-preview/self-navigation']);
  await expect(page).toHaveURL(/\/workspace\/c\/conversation-1/);
});
