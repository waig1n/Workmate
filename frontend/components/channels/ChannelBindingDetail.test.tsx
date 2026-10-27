import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from '@/lib/api';
import { QRCodeSVG } from 'qrcode.react';
import { renderToStaticMarkup } from 'react-dom/server';
import { ChannelBindingDetail } from './ChannelBindingDetail';
import type { Binding, Channel } from '@/lib/channels';
vi.mock('@/lib/api', () => ({ api: { post: vi.fn(), delete: vi.fn() } }));
const channel: Channel = { id: 'ch', name: '机器人', provider: 'wecom', agent_id: 'a', agent_name: '助手', version: 1, enabled: true, status: 'connected', has_credentials: true, group_enabled: false, can_manage: true };
beforeEach(() => { vi.clearAllMocks(); });
afterEach(() => vi.useRealTimers());
it('discards consumed binding code after binding and subsequent unbinding', async () => {
    vi.mocked(api.post).mockResolvedValue({ code: 'CONSUMED' } as never);
    const client = new QueryClient();
    const view = (bindings: Binding[]) => <QueryClientProvider client={client}><ChannelBindingDetail channel={channel} bindings={bindings} enabled wechatEnabled={false} /></QueryClientProvider>;
    const { rerender } = render(view([]));
    expect(await screen.findByText('/bind CONSUMED')).toBeVisible();
    rerender(view([{ id: 'b', channel_id: 'ch', channel_name: '机器人', provider: 'wecom', external_user_id: 'external' }]));
    await screen.findByText('已绑定');
    rerender(view([]));
    expect(screen.queryByText('/bind CONSUMED')).toBeNull();
    expect(screen.getByRole('button', { name: '生成绑定码' })).toBeVisible();
    expect(api.post).toHaveBeenCalledTimes(1);
});
it.each(['confirmed', 'expired'])('refreshes QR connection version and disables terminal %s checks', async terminal => {
    const client = new QueryClient();
    const invalidations = vi.spyOn(client, 'invalidateQueries');
    vi.mocked(api.post).mockImplementation(async path => path.endsWith('/qr') ? { qrcode: 'one', qrcode_url: 'https://liteapp.weixin.qq.com/login' } as never : { status: terminal } as never);
    render(<QueryClientProvider client={client}><ChannelBindingDetail channel={{ ...channel, provider: 'wechat' }} bindings={[]} enabled wechatEnabled /></QueryClientProvider>);
    fireEvent.click(screen.getByRole('button', { name: '微信扫码登录' }));
    await screen.findByRole('link');
    expect(screen.getByRole('img', { name: '微信登录二维码' })).toBeVisible();
    expect(invalidations).toHaveBeenCalledWith({ queryKey: ['channels'] });
    fireEvent.click(screen.getByRole('button', { name: '检查登录状态' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '检查登录状态' })).toBeDisabled());
    await waitFor(() => expect(screen.queryByRole('img', { name: '微信登录二维码' })).toBeNull());
    expect(screen.queryByRole('link')).toBeNull();
    if (terminal === 'confirmed') await waitFor(() => expect(invalidations).toHaveBeenCalledWith({ queryKey: ['channel-bindings'] }));
});
it('cannot check a superseded QR while acquiring its replacement', async () => {
    vi.mocked(api.post).mockResolvedValueOnce({ qrcode: 'one', qrcode_url: 'https://liteapp.weixin.qq.com/login' } as never).mockImplementation(() => new Promise(() => {}));
    render(<QueryClientProvider client={new QueryClient()}><ChannelBindingDetail channel={{ ...channel, provider: 'wechat' }} bindings={[]} enabled wechatEnabled /></QueryClientProvider>);
    fireEvent.click(screen.getByRole('button', { name: '微信扫码登录' }));
    await screen.findByRole('link');
    expect(screen.getByRole('img', { name: '微信登录二维码' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '微信扫码登录' }));
    await waitFor(() => expect(screen.queryByRole('img', { name: '微信登录二维码' })).toBeNull());
    expect(screen.queryByRole('link')).toBeNull();
    await waitFor(() => { const check = screen.queryByRole('button', { name: '检查登录状态' }); if (check) expect(check).toBeDisabled(); });
});

it('encodes the validated login URL rather than the private polling token', async () => {
    const url = 'https://liteapp.weixin.qq.com/login?ticket=example';
    vi.mocked(api.post).mockResolvedValue({ qrcode: 'PRIVATE-POLLING-TOKEN', qrcode_url: url } as never);
    render(<QueryClientProvider client={new QueryClient()}><ChannelBindingDetail channel={{ ...channel, provider: 'wechat' }} bindings={[]} enabled wechatEnabled /></QueryClientProvider>);
    fireEvent.click(screen.getByRole('button', { name: '微信扫码登录' }));
    const actual = await screen.findByRole('img', { name: '微信登录二维码' });
    const expected = new DOMParser().parseFromString(renderToStaticMarkup(<QRCodeSVG value={url} size={240} marginSize={4} level="M" />), 'text/html').querySelector('svg')!;
    expect(Array.from(actual.querySelectorAll('path'), path => path.getAttribute('d'))).toEqual(Array.from(expected.querySelectorAll('path'), path => path.getAttribute('d')));
    expect(actual).not.toHaveTextContent('PRIVATE-POLLING-TOKEN');
});
it('never renders an untrusted login URL as a QR code or fallback link', async () => {
    vi.mocked(api.post).mockResolvedValue({ qrcode: 'token', qrcode_url: 'https://evil.example/login' } as never);
    render(<QueryClientProvider client={new QueryClient()}><ChannelBindingDetail channel={{ ...channel, provider: 'wechat' }} bindings={[]} enabled wechatEnabled /></QueryClientProvider>);
    fireEvent.click(screen.getByRole('button', { name: '微信扫码登录' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('登录链接不可用');
    expect(screen.queryByRole('img')).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
});

it('automatically checks QR login serially and stops after confirmation', async () => {
    vi.useFakeTimers();
    vi.mocked(api.post).mockImplementation(async path => path.endsWith('/qr') ? { qrcode: 'token', qrcode_url: 'https://liteapp.weixin.qq.com/login' } as never : { status: 'confirmed' } as never);
    const view = render(<QueryClientProvider client={new QueryClient()}><ChannelBindingDetail channel={{ ...channel, provider: 'wechat' }} bindings={[]} enabled wechatEnabled /></QueryClientProvider>);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '微信扫码登录' })); await vi.advanceTimersByTimeAsync(1); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(api.post).toHaveBeenCalledWith('/channels/ch/qr-status', { qrcode: 'token' });
    await act(async () => { await vi.advanceTimersByTimeAsync(20); });
    expect(screen.getByText('登录成功，渠道已启用')).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
    expect(api.post).toHaveBeenCalledTimes(2);
    view.unmount();
});
it('never overlaps automatic checks and cancels polling on dismiss', async () => {
    vi.useFakeTimers();
    let resolve!: (value: never) => void;
    vi.mocked(api.post).mockImplementation(path => path.endsWith('/qr') ? Promise.resolve({ qrcode: 'token', qrcode_url: 'https://liteapp.weixin.qq.com/login' } as never) : new Promise(done => { resolve = done; }));
    const view = render(<QueryClientProvider client={new QueryClient()}><ChannelBindingDetail channel={{ ...channel, provider: 'wechat' }} bindings={[]} enabled wechatEnabled /></QueryClientProvider>);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '微信扫码登录' })); await vi.advanceTimersByTimeAsync(1); });
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(api.post).toHaveBeenCalledTimes(2);
    view.unmount();
    await act(async () => { resolve({ status: 'wait' } as never); await vi.advanceTimersByTimeAsync(30000); });
    expect(api.post).toHaveBeenCalledTimes(2);
});
