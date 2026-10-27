import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { Schedules } from './Schedules';
import { api } from '@/lib/api';
vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }));
const schedule = { id: 's1', version: 3, name: '每日简报', agent_id: 'a1', agent_name: '助手', prompt: '汇总新闻', model_profile: null, timezone: 'Asia/Shanghai', rule: { kind: 'daily', time: '09:00' }, status: 'active', next_fire_at: '2027-01-01T01:00:00Z', pause_reason: null, latest_occurrence: null };
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><Schedules /></QueryClientProvider>); }
beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.get).mockImplementation(async (path) => {
        if (path.startsWith('/agents'))
            return { items: [{ id: 'a1', display_name: '助手', actions: { use: true } }, { id: 'no', display_name: '不可用助手', actions: { use: false } }], total: 2 } as never;
        if (path === '/schedules/notification-targets') return { items: [{ binding_id: 'binding', channel_id: 'ch', channel_name: '办公机器人', provider: 'wecom', external_user_id: 'employee', available: true, reason: null }] } as never;
        if (path === '/models')
            return [{ id: 'm1', display_name: '模型', available: true }] as never;
        if (path.includes('/occurrences'))
            return { items: [{ id: 'o1', scheduled_for: '2027-01-01T01:00:00Z', outcome: 'skipped_busy', reason: '前一次执行尚未结束', task_id: null, conversation_id: null, missed_count: 0 }], total: 21 } as never;
        if (path === '/schedules/s1')
            return schedule as never;
        return { items: [schedule], total: 1 } as never;
    });
    vi.mocked(api.post).mockResolvedValue({ times: ['2027-01-01T01:00:00Z'] } as never);
});
it('opens detail, paginates history, and uses version for pause', async () => {
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /每日简报/ }));
    expect(await screen.findByText('忙时跳过')).toBeVisible();
    fireEvent.click(screen.getByText('查看原因'));
    expect(screen.getByText('前一次执行尚未结束')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '下一页执行记录' }));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/schedules/s1/occurrences?limit=20&offset=20'));
    fireEvent.click(screen.getByRole('button', { name: '暂停' }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/schedules/s1/pause', { version: 3 }));
});
it('creates a weekly schedule with server preview and trusted fields absent', async () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: '新建任务' }));
    fireEvent.change(screen.getByLabelText('任务名称'), { target: { value: '周报' } });
    fireEvent.change(screen.getByLabelText('目标'), { target: { value: '整理本周进度' } });
    await screen.findByRole('option', { name: '助手' });
    expect(screen.queryByRole('option', { name: '不可用助手' })).toBeNull();
    fireEvent.change(screen.getByLabelText('执行 Agent'), { target: { value: 'a1' } });
    fireEvent.change(screen.getByLabelText('频率'), { target: { value: 'weekly' } });
    fireEvent.change(screen.getByLabelText('星期'), { target: { value: '4' } });
    fireEvent.change(screen.getByLabelText('时区'), { target: { value: 'Asia/Shanghai' } });
    await screen.findByText(/下一次：/);
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/schedules', expect.objectContaining({ name: '周报', agent_id: 'a1', prompt: '整理本周进度', timezone: 'Asia/Shanghai', model_profile: null, rule: { kind: 'weekly', weekday: 4, time: '09:00' } })));
    const call = vi.mocked(api.post).mock.calls.find(c => c[0] === '/schedules')!;
    expect(call[1]).toHaveProperty('notification', null);
    expect(call[1]).not.toHaveProperty('owner_user_id');
    expect(call[1]).not.toHaveProperty('approval_profile');
});
it('keeps edits on version conflict and provides reload', async () => {
    vi.mocked(api.patch).mockRejectedValue(Object.assign(new Error('conflict'), { status: 409 }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /每日简报/ }));
    fireEvent.click(await screen.findByRole('button', { name: '编辑' }));
    await screen.findByText(/下一次：/);
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('计划已被修改');
    expect(screen.getByLabelText('任务名称')).toHaveValue('每日简报');
    expect(screen.getByRole('button', { name: '重新加载计划' })).toBeVisible();
});
it('resumes via server authorization and displays rejection', async () => {
    vi.mocked(api.get).mockImplementation(async (path) => path === '/schedules/s1' ? { ...schedule, status: 'paused', pause_reason: '模型不可用' } as never : { items: [schedule], total: 1 } as never);
    vi.mocked(api.post).mockRejectedValue(new Error('该 Agent 已无使用权限'));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /每日简报/ }));
    fireEvent.click(await screen.findByRole('button', { name: '开启' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('该 Agent 已无使用权限');
    expect(api.post).toHaveBeenCalledWith('/schedules/s1/resume', { version: 3 });
    expect(screen.getByRole('button', { name: '开启' })).toBeVisible();
});
it('confirms deletion and keeps execution results linked to their conversation', async () => {
    const get = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async (path) => path.includes('/occurrences') ? { items: [{ id: 'o1', scheduled_for: '2027-01-01T01:00:00Z', outcome: 'task_created', task_status: 'completed', task_id: 't1', conversation_id: 'c1' }], total: 1 } as never : get(path));
    vi.mocked(api.delete).mockResolvedValue(undefined);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /每日简报/ }));
    expect(await screen.findByRole('link', { name: '查看结果 →' })).toHaveAttribute('href', '/workspace/c/c1');
    fireEvent.click(screen.getByText('更多', { exact: true }));
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    expect(api.delete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/schedules/s1?version=3'));
    expect(await screen.findByRole('button', { name: '新建任务' })).toBeVisible();
});
it('uses selected timezone for once input and never saves a failed preview', async () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: '新建任务' }));
    fireEvent.change(screen.getByLabelText('频率'), { target: { value: 'once' } });
    expect(screen.queryByLabelText('星期')).toBeNull();
    fireEvent.change(screen.getByLabelText('日期'), { target: { value: '2027-02-01' } });
    fireEvent.change(screen.getByLabelText('时区'), { target: { value: 'Asia/Shanghai' } });
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/schedules/preview', { timezone: 'Asia/Shanghai', rule: { kind: 'once', run_at: '2027-02-01T01:00:00.000Z' } }));
    vi.mocked(api.post).mockRejectedValue(new Error('时间必须在未来'));
    fireEvent.change(screen.getByLabelText('日期'), { target: { value: '2020-02-01' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('时间必须在未来');
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
});
it('shows empty state and retry on list failure', async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error('offline'));
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('计划加载失败');
    vi.mocked(api.get).mockResolvedValue({ items: [], total: 0 } as never);
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByText('还没有定时任务，创建一个计划开始吧。')).toBeVisible();
});
it('edits time and model, then reloads conflicted definition without retrying stale version', async () => {
    vi.mocked(api.patch).mockRejectedValue(Object.assign(new Error('conflict'), { status: 409 }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: /每日简报/ }));
    fireEvent.click(await screen.findByRole('button', { name: '编辑' }));
    fireEvent.change(screen.getByLabelText('时间'), { target: { value: '10:30' } });
    fireEvent.click(screen.getByText('更多设置'));
    await screen.findByRole('option', { name: '模型' });
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'm1' } });
    await screen.findByText(/下一次：/);
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByRole('alert');
    expect(api.patch).toHaveBeenCalledWith('/schedules/s1', expect.objectContaining({ version: 3, model_profile: 'm1', rule: { kind: 'daily', time: '10:30' } }));
    fireEvent.click(screen.getByRole('button', { name: '重新加载计划' }));
    expect(await screen.findByRole('button', { name: '编辑' })).toBeVisible();
});
it('can cancel creating or editing and cancels deletion without mutation', async () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: '新建任务' }));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    fireEvent.click(await screen.findByRole('button', { name: /每日简报/ }));
    fireEvent.click(await screen.findByText('更多', { exact: true }));
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(api.delete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '← 返回定时任务' }));
    expect(await screen.findByRole('button', { name: '新建任务' })).toBeVisible();
});
it('paginates plans and histories backwards and forwards', async () => {
    const get = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async (path) => path.startsWith('/schedules?') ? { items: [schedule], total: 21 } as never : get(path));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '下一页计划' }));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/schedules?limit=20&offset=20'));
    fireEvent.click(await screen.findByRole('button', { name: '上一页计划' }));
    fireEvent.click(await screen.findByRole('button', { name: /每日简报/ }));
    fireEvent.click(await screen.findByRole('button', { name: '下一页执行记录' }));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/schedules/s1/occurrences?limit=20&offset=20'));
    fireEvent.click(await screen.findByRole('button', { name: '上一页执行记录' }));
    expect(screen.getByRole('button', { name: '上一页执行记录' })).toBeDisabled();
});

it('submits chosen notification and rejects both outcome toggles off', async () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: '新建任务' }));
    fireEvent.change(screen.getByLabelText('任务名称'), { target: { value: '通知测试' } });
    fireEvent.change(screen.getByLabelText('目标'), { target: { value: '汇总' } });
    await screen.findByRole('option', { name: '助手' });
    fireEvent.change(screen.getByLabelText('执行 Agent'), { target: { value: 'a1' } });
    await screen.findByRole('option', { name: /办公机器人/ });
    fireEvent.change(screen.getByLabelText('通知到'), { target: { value: 'binding' } });
    fireEvent.click(screen.getByLabelText('执行成功时'));
    fireEvent.click(screen.getByLabelText('执行失败时'));
    await screen.findByText(/下一次：/);
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    expect(vi.mocked(api.post).mock.calls.some(([path]) => path === '/schedules')).toBe(false);
    fireEvent.click(screen.getByLabelText('执行成功时'));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/schedules', expect.objectContaining({ notification: { binding_id: 'binding', on_success: true, on_failure: false } })));
});
