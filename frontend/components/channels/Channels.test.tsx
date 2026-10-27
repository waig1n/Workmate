import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { api } from '@/lib/api';
import { Channels } from './Channels';
vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }));
const channel = { id: 'ch', name: '办公机器人', provider: 'wecom', agent_id: 'a', agent_name: '助手', version: 2, enabled: true, status: 'connected', has_credentials: true, group_enabled: false };
let manager = false;
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><Channels /></QueryClientProvider>); }
beforeEach(() => {
    vi.clearAllMocks();
    manager = false;
    vi.mocked(api.get).mockImplementation(async (path) => {
        if (path === '/channels')
            return { items: [channel], can_manage: manager, wechat_enabled: false } as never;
        if (path === '/channels/bindings')
            return { items: [{ id: 'b', channel_id: 'ch', channel_name: '办公机器人', provider: 'wecom', external_user_id: 'external' }] } as never;
        if (path.startsWith('/agents'))
            return { items: [{ id: 'a', display_name: '助手', actions: { use: true } }], total: 1 } as never;
        return { items: [] } as never;
    });
    vi.mocked(api.post).mockResolvedValue({ code: 'ONETIME', expires_at: '2026-12-01T10:00:00Z' } as never);
});
it('platform cards show bound status and all identities without regenerating code', async () => { mount(); await waitFor(() => expect(screen.getByRole('button', { name: /^企业微信/ })).toBeEnabled()); fireEvent.click(screen.getByRole('button', { name: /^企业微信/ })); fireEvent.click(await screen.findByRole('button', { name: '解除绑定' })); await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/channels/bindings/b')); expect(api.post).not.toHaveBeenCalled(); });
it('edits using version and omits unchanged write-only credentials', async () => { manager = true; mount(); fireEvent.click(await screen.findByRole('button', { name: '编辑 办公机器人' })); expect(screen.getByLabelText('Bot Secret')).toHaveValue(''); expect(screen.getByLabelText('Bot Secret')).toHaveAttribute('placeholder', '********'); expect(screen.getByLabelText('Bot ID')).toHaveAttribute('placeholder', '********'); expect(screen.getByText(/修改其他设置无需重新填写/)).toBeVisible(); fireEvent.change(screen.getByLabelText('渠道名称'), { target: { value: '新名称' } }); await waitFor(() => expect(screen.getByRole('button', { name: '保存' })).toBeEnabled()); fireEvent.click(screen.getByRole('button', { name: '保存' })); await waitFor(() => expect(api.patch).toHaveBeenCalledWith('/channels/ch', { version: 2, name: '新名称', agent_id: 'a', enabled: true, group_enabled: false, group_allowlist: [] })); });
it('preserves form on conflict and reloads current version', async () => { manager = true; vi.mocked(api.patch).mockRejectedValue(Object.assign(new Error('conflict'), { status: 409 })); mount(); fireEvent.click(await screen.findByRole('button', { name: '编辑 办公机器人' })); await waitFor(() => expect(screen.getByRole('button', { name: '保存' })).toBeEnabled()); fireEvent.click(screen.getByRole('button', { name: '保存' })); expect(await screen.findByRole('alert')).toHaveTextContent('已被修改'); expect(screen.getByLabelText('渠道名称')).toHaveValue('办公机器人'); fireEvent.click(screen.getByRole('button', { name: '重新加载' })); expect(await screen.findByRole('button', { name: '编辑 办公机器人' })).toBeVisible(); });
it('creates a platform connection with group allowlist and no caller identity', async () => { manager = true; mount(); fireEvent.click(await screen.findByRole('button', { name: '新增企业微信' })); fireEvent.change(screen.getByLabelText('渠道名称'), { target: { value: '飞书办公' } }); fireEvent.change(screen.getByLabelText('平台'), { target: { value: 'feishu' } }); await screen.findByRole('option', { name: '助手' }); fireEvent.change(screen.getByLabelText('默认 Agent'), { target: { value: 'a' } }); fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'app' } }); fireEvent.change(screen.getByLabelText('App Secret'), { target: { value: 'secret' } }); fireEvent.click(screen.getByLabelText('允许群聊 @ 触发')); fireEvent.change(screen.getByLabelText(/允许接入的群 ID/), { target: { value: 'group1\ngroup2' } }); fireEvent.click(screen.getByRole('button', { name: '保存' })); await waitFor(() => expect(api.post).toHaveBeenCalledWith('/channels', { name: '飞书办公', provider: 'feishu', agent_id: 'a', group_enabled: true, group_allowlist: ['group1', 'group2'], credentials: { app_id: 'app', app_secret: 'secret' } })); });
it('rejects partial replacement credentials and allows cancel', async () => { manager = true; mount(); fireEvent.click(await screen.findByRole('button', { name: '编辑 办公机器人' })); fireEvent.change(screen.getByLabelText('Bot Secret'), { target: { value: 'new' } }); await waitFor(() => expect(screen.getByRole('button', { name: '保存' })).toBeEnabled()); fireEvent.click(screen.getByRole('button', { name: '保存' })); expect(await screen.findByRole('alert')).toHaveTextContent('完整填写'); expect(api.patch).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole('button', { name: '取消' })); expect(await screen.findByRole('button', { name: '编辑 办公机器人' })).toBeVisible(); });
it('hides closed personal experiment and keeps memory help collapsed', async () => { mount(); await screen.findByRole('button', { name: /^企业微信/ }); expect(screen.queryByRole('button', { name: /^个人微信/ })).toBeNull(); expect(screen.queryByText(/长期记忆按/)).toBeNull(); fireEvent.click(screen.getByRole('button', { name: '会话与记忆说明' })); expect(screen.getByRole('tooltip')).toHaveTextContent('长期记忆'); });
it('single unbound bot generates code directly from platform card', async () => { const original = vi.mocked(api.get).getMockImplementation()!; vi.mocked(api.get).mockImplementation(async path => path === '/channels/bindings' ? { items: [] } as never : original(path)); mount(); await waitFor(() => expect(screen.getByRole('button', { name: /^企业微信/ })).toBeEnabled()); fireEvent.click(screen.getByRole('button', { name: /^企业微信/ })); expect(await screen.findByText('/bind ONETIME')).toBeVisible(); expect(api.post).toHaveBeenCalledWith('/channels/ch/binding-code', {}); });
it('multiple bots require choosing the actual connection', async () => { const original = vi.mocked(api.get).getMockImplementation()!; vi.mocked(api.get).mockImplementation(async path => path === '/channels' ? { items: [channel, { ...channel, id: 'two', name: '研发机器人' }], can_manage: false, wechat_enabled: false } as never : original(path)); mount(); await waitFor(() => expect(screen.getByRole('button', { name: /^企业微信/ })).toBeEnabled()); fireEvent.click(screen.getByRole('button', { name: /^企业微信/ })); expect(api.post).not.toHaveBeenCalled(); fireEvent.click(await screen.findByRole('button', { name: /研发机器人/ })); expect(await screen.findByText('/bind ONETIME')).toBeVisible(); expect(api.post).toHaveBeenCalledWith('/channels/two/binding-code', {}); });
it.each([false, true])('creates DingTalk with optional streaming template: %s', async (withTemplate) => {
    manager = true;
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '新增企业微信' }));
    fireEvent.change(screen.getByLabelText('渠道名称'), { target: { value: '钉钉办公' } });
    fireEvent.change(screen.getByLabelText('平台'), { target: { value: 'dingtalk' } });
    await screen.findByRole('option', { name: '助手' });
    fireEvent.change(screen.getByLabelText('默认 Agent'), { target: { value: 'a' } });
    fireEvent.change(screen.getByLabelText('Client ID'), { target: { value: 'client' } });
    fireEvent.change(screen.getByLabelText('Client Secret'), { target: { value: 'secret' } });
    const template = screen.getByLabelText('流式卡片模板 ID（可选）');
    const advanced = screen.getByText('流式回复配置（可选）');
    expect(advanced.tagName).toBe('SUMMARY');
    expect(advanced.parentElement).not.toHaveAttribute('open');
    expect(template).not.toBeVisible();
    fireEvent.click(advanced);
    expect(template).toBeVisible();
    expect(template).not.toBeRequired();
    expect(screen.getByText(/已发布.*content/)).toBeVisible();
    if (withTemplate) fireEvent.change(template, { target: { value: 'template-id' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/channels', {
        name: '钉钉办公', provider: 'dingtalk', agent_id: 'a', group_enabled: false,
        group_allowlist: [], credentials: { client_id: 'client', client_secret: 'secret',
            ...(withTemplate ? { card_template_id: 'template-id' } : {}) },
    }));
});

it('shows every identity bound to the same bot and unbinds only the chosen identity', async () => {
    const original = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async path => path === '/channels/bindings' ? { items: ['first', 'second'].map(id => ({ id, channel_id: 'ch', provider: 'wecom', channel_name: channel.name, external_user_id: id })) } as never : original(path));
    mount();
    await waitFor(() => expect(screen.getByRole('button', { name: /^企业微信/ })).toBeEnabled());
    expect(screen.getByRole('button', { name: /^企业微信/ })).toHaveTextContent('已绑定 2 个账号');
    fireEvent.click(screen.getByRole('button', { name: /^企业微信/ }));
    expect(await screen.findByText('second')).toBeVisible();
    fireEvent.click(screen.getAllByRole('button', { name: '解除绑定' })[1]);
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/channels/bindings/second'));
    expect(api.post).not.toHaveBeenCalled();
});

it('fails closed when binding state is unknown and retries without issuing codes', async () => {
    const original = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async path => { if (path === '/channels/bindings') throw new Error('offline'); return original(path); });
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('绑定加载失败');
    expect(screen.getByRole('button', { name: /^企业微信/ })).toBeDisabled();
    expect(api.post).not.toHaveBeenCalled();
});

it('global disable prevents new codes and QR but preserves existing identity removal', async () => {
    const original = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async path => path === '/channels' ? { items: [channel], can_manage: false, wechat_enabled: false, enabled: false } as never : original(path));
    mount();
    await waitFor(() => expect(screen.getByRole('button', { name: /^企业微信/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /^企业微信/ }));
    expect(await screen.findByRole('button', { name: '解除绑定' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '生成绑定码' })).toBeNull();
    expect(api.post).not.toHaveBeenCalled();
});

it('a disabled bound bot remains selectable beside an enabled bot', async () => {
    const original = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async path => path === '/channels' ? { items: [{ ...channel, enabled: false }, { ...channel, id: 'two', name: '新机器人' }], can_manage: false, wechat_enabled: false } as never : original(path));
    mount();
    await waitFor(() => expect(screen.getByRole('button', { name: /^企业微信/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /^企业微信/ }));
    fireEvent.click(await screen.findByRole('button', { name: /办公机器人/ }));
    expect(await screen.findByRole('button', { name: '解除绑定' })).toBeVisible();
    expect(api.post).not.toHaveBeenCalled();
});

it('late binding code cannot appear after closing and opening another platform', async () => {
    const original = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async path => path === '/channels/bindings' ? { items: [] } as never : original(path));
    let resolve!: (value: never) => void;
    vi.mocked(api.post).mockImplementation(() => new Promise(done => { resolve = done; }));
    mount();
    await waitFor(() => expect(screen.getByRole('button', { name: /^企业微信/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /^企业微信/ }));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    fireEvent.click(screen.getByRole('button', { name: /^飞书/ }));
    resolve({ code: 'STALE' } as never);
    await waitFor(() => expect(screen.getByRole('dialog')).toHaveTextContent('暂无可用连接'));
    expect(screen.queryByText('/bind STALE')).toBeNull();
});

it('personal connection uses isolated QR login and no enterprise binding code', async () => {
    const original = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async path => path === '/channels' ? { items: [{ ...channel, provider: 'wechat', can_manage: true }], can_manage: false, wechat_enabled: true } as never : path === '/channels/bindings' ? { items: [] } as never : original(path));
    vi.mocked(api.post).mockImplementation(async path => path.endsWith('/qr') ? { qrcode: 'opaque', qrcode_url: 'https://liteapp.weixin.qq.com/login' } as never : { status: 'confirmed' } as never);
    mount();
    await waitFor(() => expect(screen.getByRole('button', { name: /^个人微信/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /^个人微信/ }));
    fireEvent.click(await screen.findByRole('button', { name: '微信扫码登录' }));
    expect(await screen.findByRole('link', { name: '打开微信扫码登录页面' })).toHaveAttribute('href', 'https://liteapp.weixin.qq.com/login');
    fireEvent.click(screen.getByRole('button', { name: '检查登录状态' }));
    expect(await screen.findByText('登录成功，渠道已启用')).toBeVisible();
    expect(api.post).toHaveBeenCalledWith('/channels/ch/qr-status', { qrcode: 'opaque' });
    expect(api.post).not.toHaveBeenCalledWith('/channels/ch/binding-code', {});
});

it('unconfigured platform offers managers a form preselected for that platform', async () => {
    manager = true;
    mount();
    await waitFor(() => expect(screen.getByRole('button', { name: /^飞书/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /^飞书/ }));
    expect(await screen.findByLabelText('平台')).toHaveValue('feishu');
});

it('management actions stay directly available when identity query fails', async () => {
    manager = true;
    const original = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async path => { if (path === '/channels/bindings') throw new Error('offline'); return original(path); });
    mount();
    await waitFor(() => expect(screen.getByRole('button', { name: /^钉钉/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /^钉钉/ }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('新增钉钉');
    expect(screen.getByLabelText('平台')).toHaveValue('dingtalk');
    expect(screen.queryByRole('button', { name: '管理渠道' })).toBeNull();
});

it('late save cannot close a newer configuration dialog', async () => {
    manager = true;
    let resolve!: (value: never) => void;
    vi.mocked(api.patch).mockImplementation(() => new Promise(done => { resolve = done; }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '编辑 办公机器人' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '保存' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    fireEvent.click(screen.getByRole('button', { name: /^飞书/ }));
    fireEvent.change(screen.getByLabelText('渠道名称'), { target: { value: '保留我的新表单' } });
    resolve({} as never);
    await new Promise(done => setTimeout(done, 30));
    expect(screen.getByLabelText('渠道名称')).toHaveValue('保留我的新表单');
});

it('warns that replacing DingTalk credentials also replaces its optional template', async () => {
    manager = true;
    const original = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async path => path === '/channels' ? { items: [{ ...channel, provider: 'dingtalk' }], can_manage: true, wechat_enabled: false } as never : original(path));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '编辑 办公机器人' }));
    expect(screen.getByLabelText('Client Secret')).toHaveAttribute('placeholder', '********');
    expect(screen.getByLabelText('流式卡片模板 ID（可选）')).not.toHaveAttribute('placeholder');
    fireEvent.change(screen.getByLabelText('Client Secret'), { target: { value: 'replacement' } });
    expect(screen.getByText(/模板留空将改为整段回复/)).toBeVisible();
});

it('personal connection cannot be enabled manually before first QR login', async () => {
    const original = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async path => path === '/channels' ? { items: [{ ...channel, provider: 'wechat', can_manage: true, enabled: false, has_credentials: false }], can_manage: false, wechat_enabled: true } as never : original(path));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '编辑 办公机器人' }));
    expect(screen.getByLabelText('启用渠道')).toBeDisabled();
    expect(screen.getByText(/请先扫码登录/)).toBeVisible();
});
