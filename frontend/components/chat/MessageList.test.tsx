import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render as rtlRender, screen } from "@testing-library/react";
import { createElement } from "react";
import { expect, it, vi } from "vitest";
import { MessageList } from "./MessageList";
import type { Artifact, Task } from "@/lib/types";

function render(element: React.ReactElement) { return rtlRender(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{element}</QueryClientProvider>); }

it("places the gallery after its answer and keeps it outside execution details", () => {
  const { container } = render(createElement(MessageList, {
    items: [
      { kind: "tool", id: "search", runId: "reply", call: { toolCallId: "search", toolCallName: "web_search", state: "success", args: "{}", result: JSON.stringify({ category: "images", results: [{ title: "雪山", url: "https://example.com/page", image_url: "https://example.com/photo.jpg" }] }) } },
      { kind: "text", id: "answer", runId: "reply", role: "assistant", text: "这是找到的图片 https://example.com/page", done: true },
    ], runs: [{ id: "reply", status: "RUN_FINISHED", error: null }], historyLoaded: true,
    agentName: "Assistant", steps: [], artifacts: [], tasks: [],
    onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn(),
  }));
  const gallery = screen.getByRole("region", { name: "图片搜索结果" });
  const answer = screen.getByText(/这是找到的图片/);
  expect(answer.compareDocumentPosition(gallery) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /执行过程/ }));
  expect(container.querySelectorAll('[aria-label="图片搜索结果"]')).toHaveLength(1);
  expect(gallery.closest('[aria-live]')).toBeNull();
});

it("keeps historical public text visible rather than relocating it into a fold", () => {
  render(createElement(MessageList, {
    items: [{ kind: "text", id: "history", taskId: "task", role: "assistant", done: true,
      text: "准备📝完成答复", presentation: { version: 1, process_end: 3 } }],
    runs: [], historyLoaded: true, agentName: "Assistant", steps: [], artifacts: [],
    tasks: [{ id: "task", run_id: "reply", status: "completed" } as Task],
    onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn(),
  }));
  expect(screen.getByText("准备📝完成答复")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /执行过程/ })).toBeNull();
});

it("matches a replay-only Skill to the completed Product Task without a RunEntry", () => {
  render(createElement(MessageList, {
    items: [{ kind: "tool", id: "skill", runId: "reply", call: {
      toolCallId: "skill", toolCallName: "Skill", state: "success", args: "{}", result: "loaded",
    } }],
    runs: [], historyLoaded: true, agentName: "Assistant", steps: [], artifacts: [],
    tasks: [{ id: "task", run_id: "reply", status: "completed" } as Task],
    onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn(),
  }));
  expect(screen.getByText("已完成")).toBeTruthy();
  expect(screen.queryByLabelText("仍在运行")).toBeNull();
});

it("delivers artifacts only under the final answer after the task ends", () => {
  const props = { items: [
    { kind: "text" as const, id: "progress", taskId: "task", role: "assistant" as const, text: "正在生成", done: true },
    { kind: "text" as const, id: "final", runId: "reply", role: "assistant" as const, text: "处理完成", done: true },
  ], runs: [], historyLoaded: true, agentName: "Assistant", steps: [],
  artifacts: [{ id: "file", task_id: "task", name: "交付.pptx", version: 1 } as Artifact],
  tasks: [{ id: "task", run_id: "reply", status: "running" } as Task],
  onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn() };
  const { rerender } = render(createElement(MessageList, props));
  expect(screen.queryByRole("button", { name: "打开成果 交付.pptx" })).toBeNull();
  rerender(<QueryClientProvider client={new QueryClient()}><MessageList {...props} tasks={[{ ...props.tasks[0], status: "completed" }]} /></QueryClientProvider>);
  expect(screen.getAllByRole("button", { name: "打开成果 交付.pptx" })).toHaveLength(1);
  expect(screen.getByText("处理完成").compareDocumentPosition(screen.getByRole("button", { name: "打开成果 交付.pptx" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it("keeps one activity indicator after the active output and before a queued user message", () => {
  const props = { items: [
    { kind: "text" as const, id: "answer", taskId: "task", runId: "reply", role: "assistant", text: "正在检查文件", done: true },
    { kind: "text" as const, id: "queued", taskId: "next", role: "user", text: "下一项任务", done: true },
  ], runs: [], historyLoaded: true, agentName: "Assistant", steps: [], artifacts: [],
  tasks: [{ id: "task", run_id: "reply", status: "running" } as Task],
  onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn(),
  activity: { taskId: "task", runId: "reply", label: "正在处理…" } };
  const { rerender } = render(<MessageList {...props} />);
  const status = screen.getByRole("status", { name: "智能体执行状态" });
  expect(screen.getByText("正在检查文件").compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(status.compareDocumentPosition(screen.getByText("下一项任务")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  rerender(<QueryClientProvider client={new QueryClient()}><MessageList {...props} activity={undefined} /></QueryClientProvider>);
  expect(screen.queryByRole("status", { name: "智能体执行状态" })).toBeNull();
});

it("replaces the current progress indicator with an inline approval", () => {
  render(<MessageList items={[]} runs={[]} historyLoaded agentName="Assistant" steps={[]} artifacts={[]} tasks={[]}
    onPrompt={vi.fn()} onOpenActivity={vi.fn()} onOpenArtifact={vi.fn()}
    activity={{ taskId: "task", label: "等待你的审批" }} approval={<section aria-label="待审批操作">需要你确认</section>} />);
  expect(screen.getByRole("region", { name: "待审批操作" })).toBeVisible();
  expect(screen.queryByRole("status", { name: "智能体执行状态" })).toBeNull();
});

it("shows one shared file revision beneath each task that delivered it", () => {
  render(<MessageList items={[
    { kind: "text", id: "a", taskId: "first", role: "assistant", text: "MD 已完成", done: true },
    { kind: "text", id: "b", taskId: "second", role: "assistant", text: "PDF 已完成", done: true },
  ]} runs={[]} historyLoaded agentName="Assistant" steps={[]}
    artifacts={["first", "second"].map((task_id) => ({ id: "md-v1", task_id,
      name: "report.md", version: 1, size: 12 } as Artifact))}
    tasks={["first", "second"].map((id) => ({ id, status: "completed" } as Task))}
    onPrompt={vi.fn()} onOpenActivity={vi.fn()} onOpenArtifact={vi.fn()} />);
  expect(screen.getAllByRole("button", { name: "打开成果 report.md" })).toHaveLength(2);
});


it('shows a message-specific incomplete notice, including empty content, without replacing approval', () => {
  const props = { items: [{ kind: 'text' as const, id: 'answer', taskId: 'task', runId: 'reply',
    role: 'assistant', text: '', done: false, incomplete: true }], runs: [], historyLoaded: true,
    agentName: 'Assistant', steps: [], artifacts: [], tasks: [], historyIncomplete: true,
    activity: { taskId: 'task', runId: 'reply', label: '等待你的审批' },
    approval: <section aria-label="待审批操作">需要你确认</section>,
    onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn() };
  const { rerender } = render(<MessageList {...props} />);
  expect(screen.getByText('这条回复内容不完整')).toBeVisible();
  expect(screen.queryByText('工作中…')).toBeNull();
  expect(screen.queryByText('部分回复内容不完整')).toBeNull();
  expect(screen.getByRole('region', { name: '待审批操作' })).toBeVisible();
  rerender(<QueryClientProvider client={new QueryClient()}><MessageList {...props}
    items={[{ ...props.items[0], text: '保留下来的部分回复' }]} /></QueryClientProvider>);
  expect(screen.getByText('保留下来的部分回复')).toBeVisible();
  expect(screen.getByText('这条回复内容不完整')).toBeVisible();
  expect(screen.queryByText('▍')).toBeNull();
  rerender(<QueryClientProvider client={new QueryClient()}><MessageList {...props} historyIncomplete={false}
    items={[{ ...props.items[0], text: '完整回复', done: true, incomplete: false }]} /></QueryClientProvider>);
  expect(screen.queryByText('这条回复内容不完整')).toBeNull();
  expect(screen.getByText('完整回复')).toBeVisible();
});

it('shows an unassigned history notice without calling a healthy reply incomplete', () => {
  const props = { items: [{ kind: 'text' as const, id: 'healthy', role: 'assistant',
    text: '正在继续回答', done: false }], runs: [], historyLoaded: true,
    agentName: 'Assistant', steps: [], artifacts: [], tasks: [],
    onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn() };
  const { rerender } = render(<MessageList {...props} historyIncomplete />);
  expect(screen.getByText('部分回复内容不完整')).toBeVisible();
  expect(screen.queryByText('这条回复内容不完整')).toBeNull();
  expect(screen.getByText('正在继续回答')).toBeVisible();
  rerender(<QueryClientProvider client={new QueryClient()}><MessageList {...props} historyIncomplete={false} /></QueryClientProvider>);
  expect(screen.queryByText('部分回复内容不完整')).toBeNull();
});

it('keeps the history notice visible without messages instead of showing the welcome screen', () => {
  render(<MessageList items={[]} runs={[]} historyLoaded agentName="Assistant" steps={[]} artifacts={[]} tasks={[]}
    historyIncomplete onPrompt={vi.fn()} onOpenActivity={vi.fn()} onOpenArtifact={vi.fn()} />);
  expect(screen.getByText('部分回复内容不完整')).toBeVisible();
  expect(screen.queryByText('交给我一项工作')).toBeNull();
});


it('does not label ordinary partial or complete replies as incomplete', () => {
  const props = { items: [{ kind: 'text' as const, id: 'normal', role: 'assistant',
    text: '正常回复', done: false }], runs: [], historyLoaded: true,
    agentName: 'Assistant', steps: [], artifacts: [], tasks: [],
    onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn() };
  const { rerender } = render(<MessageList {...props} />);
  expect(screen.queryByText(/回复内容不完整/)).toBeNull();
  rerender(<QueryClientProvider client={new QueryClient()}><MessageList {...props}
    items={[{ ...props.items[0], done: true }]} /></QueryClientProvider>);
  expect(screen.queryByText(/回复内容不完整/)).toBeNull();
});

it('folds historical process into its own round without hiding approvals or incomplete notices', () => {
  const items = [
    { kind: 'text' as const, id: 'u1', role: 'user', text: '深圳天气', done: true },
    { kind: 'text' as const, id: 'a1', role: 'assistant', taskId: 't1', text: '查询深圳。深圳晴。', done: true, presentation: { version: 1, process_end: 5 } },
    { kind: 'text' as const, id: 'u2', role: 'user', text: '广州天气', done: true },
    { kind: 'text' as const, id: 'a2', role: 'assistant', taskId: 't2', text: '查询广州。', done: true, incomplete: true, presentation: { version: 1, process_end: 5 } },
    ...['r1', 'r2'].map(runId => ({ kind: 'tool' as const, id: runId, runId, call: { toolCallId: runId, toolCallName: 'Read', args: '', result: '', state: 'success' } })),
  ];
  render(<MessageList items={items} runs={['1', '2'].map(n => ({ id: `r${n}`, taskId: `t${n}`, status: 'RUN_FINISHED', error: null }))}
    historyLoaded agentName="Assistant" steps={[]} artifacts={[]} tasks={[]}
    activity={{ taskId: 't2', runId: 'r2', label: '等待你的审批' }} approval={<section aria-label="待审批操作">确认操作</section>}
    onPrompt={vi.fn()} onOpenActivity={vi.fn()} onOpenArtifact={vi.fn()} />);
  const processes = screen.getAllByRole('button', { name: /执行过程/ });
  expect(processes).toHaveLength(2);
  expect(processes[0].compareDocumentPosition(screen.getByText('深圳晴。')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.getByText('深圳晴。').compareDocumentPosition(screen.getByText('广州天气')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.queryByText('查询深圳。')).toBeNull();
  fireEvent.click(processes[0]);
  expect(screen.getByText('查询深圳。')).toBeVisible();
  expect(screen.getByText('这条回复内容不完整')).toBeVisible();
  expect(screen.getByRole('region', { name: '待审批操作' })).toBeVisible();
  expect(screen.queryByText('工作中…')).toBeNull();
});

it("keeps process expansion when a queued Task acquires its Reply identity", () => {
  const task = { id: "task-live", run_id: null, status: "created" } as Task;
  const props = { items: [{ kind: "text" as const, id: "a-live", taskId: task.id, role: "assistant", text: "准备中", done: false, presentation: { version: 1, process_end: 3 } }],
    runs: [{ id: "pending-live", taskId: task.id, status: "queued", error: null }], historyLoaded: true, agentName: "Assistant", steps: [], artifacts: [], tasks: [task], onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn() };
  const client = new QueryClient();
  const view = (value: typeof props) => <QueryClientProvider client={client}><MessageList {...value} /></QueryClientProvider>;
  const { rerender } = rtlRender(view(props));
  fireEvent.click(screen.getByRole("button", { name: /执行过程/ }));
  rerender(view({ ...props, tasks: [{ ...task, run_id: "reply-live", status: "running" }], runs: [{ id: "reply-live", taskId: task.id, status: "running", error: null }] }));
  expect(screen.getByRole("button", { name: /执行过程/ })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("准备中")).toBeVisible();
});

it("streams the answer tail outside collapsed process before completion without duplicating it", () => {
  const item = { kind: "text" as const, id: "tail", runId: "r", taskId: "t", role: "assistant", done: false,
    text: "准备资料。结论已得出", presentation: { version: 1, process_end: 5 } };
  const props = { items: [item], runs: [{ id: "r", taskId: "t", status: "running", error: null }],
    tasks: [{ id: "t", run_id: "r", status: "running" } as Task], historyLoaded: true, agentName: "Assistant",
    steps: [], artifacts: [], onPrompt: vi.fn(), onOpenActivity: vi.fn(), onOpenArtifact: vi.fn() };
  const client = new QueryClient();
  const view = (value: typeof props) => <QueryClientProvider client={client}><MessageList {...value} /></QueryClientProvider>;
  const { rerender } = rtlRender(view(props));
  expect(screen.getAllByText("结论已得出")).toHaveLength(1);
  expect(screen.queryByText("准备资料。")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /执行过程/ }));
  expect(screen.getByText("准备资料。")).toBeVisible();
  expect(screen.getAllByText("结论已得出")).toHaveLength(1);
  rerender(view({ ...props, tasks: [{ ...props.tasks[0], status: "completed" }],
    runs: [{ ...props.runs[0], status: "RUN_FINISHED" }], items: [{ ...item, done: true }] }));
  expect(screen.getAllByText("结论已得出")).toHaveLength(1);
  expect(screen.getByRole("button", { name: /执行过程/ })).toHaveAttribute("aria-expanded", "true");
});

it('identifies a scheduled user message as a system trigger',()=>{
 render(<MessageList items={[{kind:'text',id:'u',taskId:'t',role:'user',text:'生成日报',done:true}]} tasks={[{id:'t',origin:'schedule',approval_profile:'unattended'} as Task]} runs={[]} historyLoaded agentName="助手" steps={[]} artifacts={[]} onPrompt={vi.fn()} onOpenActivity={vi.fn()} onOpenArtifact={vi.fn()} />);
 expect(screen.getByText('定时任务触发 · 无人值守')).toBeVisible();
});
it('labels channel messages separately from unattended schedules',()=>{
 render(<MessageList items={[{kind:'text',id:'u',taskId:'t',role:'user',text:'渠道请求',done:true}]} tasks={[{id:'t',origin:'channel',approval_profile:'standard'} as Task]} runs={[]} historyLoaded agentName="助手" steps={[]} artifacts={[]} onPrompt={vi.fn()} onOpenActivity={vi.fn()} onOpenArtifact={vi.fn()} />);
 expect(screen.getByText('IM 渠道发起')).toBeVisible();
 expect(screen.queryByText('定时任务触发 · 无人值守')).toBeNull();
});
