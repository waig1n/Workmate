"use client";
import { useEffect, useRef } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { channelButton as button, channelError, safeLoginUrl, type Binding, type Channel } from '@/lib/channels';

export function ChannelBindingDetail({ channel, bindings, enabled, wechatEnabled, onEdit }: {
    channel: Channel; bindings: Binding[]; enabled: boolean; wechatEnabled: boolean; onEdit?: () => void;
}) {
    const client = useQueryClient();
    const started = useRef(false);
    const pollDeadline = useRef(0);
    const bind = useMutation({ mutationFn: () => api.post<{ code: string }>(`/channels/${channel.id}/binding-code`, {}) });
    const remove = useMutation({ mutationFn: (id: string) => api.delete(`/channels/bindings/${id}`), onSuccess: () => client.invalidateQueries({ queryKey: ['channel-bindings'] }) });
    const qr = useMutation({ mutationFn: () => api.post<{ qrcode: string; qrcode_url: string }>(`/channels/${channel.id}/qr`, {}), onSuccess: () => { pollDeadline.current = Date.now() + 5 * 60 * 1000; return client.invalidateQueries({ queryKey: ['channels'] }); } });
    const status = useMutation({ mutationFn: () => api.post<{ status: string }>(`/channels/${channel.id}/qr-status`, { qrcode: qr.data?.qrcode }), onSuccess: async result => {
        await client.invalidateQueries({ queryKey: ['channels'] });
        if (result.status === 'confirmed') await client.invalidateQueries({ queryKey: ['channel-bindings'] });
    } });
    useEffect(() => {
        if (!started.current) {
            started.current = true;
            if (channel.provider !== 'wechat' && enabled && channel.enabled && bindings.length === 0) bind.mutate();
        }
    }, [channel.provider, channel.enabled, enabled, bindings.length, bind]);
    useEffect(() => {
        if (bindings.length && (bind.data || bind.isPending || bind.error)) bind.reset();
    }, [bindings.length, bind]);
    const terminalQr = status.data?.status === 'confirmed' || status.data?.status === 'expired';
    useEffect(() => {
        if (!qr.data || qr.isPending || terminalQr || !enabled || !wechatEnabled || status.isPending || status.isError || Date.now() >= pollDeadline.current) return;
        const timer = window.setTimeout(() => { if (Date.now() < pollDeadline.current) status.mutate(); }, 3000);
        return () => window.clearTimeout(timer);
    }, [qr.data, qr.isPending, terminalQr, enabled, wechatEnabled, status]);
    const login = qr.data ? safeLoginUrl(qr.data.qrcode_url) : null;
    return <div className="space-y-4">
        <p className="text-sm text-[var(--ink-soft)]">{channel.agent_name} · {channel.enabled ? channel.status === 'connected' ? '已连接' : '尚未连接，机器人恢复连接后可接收消息' : '已停用'}</p>
        {!enabled && <p role="status">管理员尚未启用 IM 渠道服务。</p>}
        {bindings.map(b => <div key={b.id} className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--line)] p-3"><div><p>已绑定</p><p className="break-all text-xs text-[var(--ink-soft)]">{b.external_user_id}</p></div><button className={button} disabled={remove.isPending} onClick={() => remove.mutate(b.id)}>解除绑定</button></div>)}
        {channel.provider !== 'wechat' && bindings.length === 0 && <>
            {bind.isPending && <p role="status">正在生成绑定码…</p>}
            {bind.data && enabled && channel.enabled && <section aria-label="一次性绑定码"><p className="text-sm">请在“{channel.name}”的私聊中发送：</p><code className="my-3 block select-all break-all">/bind {bind.data.code}</code><p className="text-xs text-[var(--ink-soft)]">仅供本人使用，有效期 5 分钟。</p></section>}
            {enabled && channel.enabled && !bind.isPending && <button className={button} onClick={() => bind.mutate()}>{bind.data ? '重新生成绑定码' : '生成绑定码'}</button>}
        </>}
        {channel.provider === 'wechat' && <>
            {!wechatEnabled && <p>个人微信试验尚未开放。</p>}
            {enabled && wechatEnabled && channel.can_manage && <button className={button} disabled={qr.isPending || status.isPending} onClick={() => { status.reset(); qr.mutate(); }}>微信扫码登录</button>}
            {qr.data && enabled && wechatEnabled && <section aria-label="微信登录">{!qr.isPending && !terminalQr && (login ? <div className="space-y-3"><p className="text-sm">请使用微信扫描二维码，并在手机上确认登录。</p><QRCodeSVG value={login} size={240} marginSize={4} level="M" bgColor="#FFFFFF" fgColor="#000000" role="img" aria-label="微信登录二维码" className="mx-auto h-auto w-full max-w-[240px]" /><a href={login} target="_blank" rel="noopener noreferrer" className="block text-sm underline">打开微信扫码登录页面</a></div> : <p role="alert">登录链接不可用，请重新获取。</p>)}<button className={`${button} mt-3`} disabled={status.isPending || qr.isPending || terminalQr || !enabled || !wechatEnabled} onClick={() => status.mutate()}>检查登录状态</button></section>}
            {status.data && <p role="status">{({ confirmed: '登录成功，渠道已启用', wait: '等待扫码', waiting: '等待扫码', scaned: '已扫码，请在微信确认', scanned: '已扫码，请在微信确认', expired: '二维码已过期，请重新获取' }[status.data.status] ?? status.data.status)}</p>}
            {onEdit && <button className={button} onClick={onEdit}>编辑连接</button>}
        </>}
        {[bind.error, remove.error, qr.error, status.error].filter(Boolean).map((error, i) => <p role="alert" key={i}>{channelError(error)}</p>)}
    </div>;
}
