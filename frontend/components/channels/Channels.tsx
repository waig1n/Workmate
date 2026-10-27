"use client";
import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { HelpTip } from '@/components/ui/HelpTip';
import { Dialog } from '@/components/ui/Dialog';
import { ChannelForm } from './ChannelForm';
import { ChannelBindingDetail } from './ChannelBindingDetail';
import { channelButton as button, providerNames, type Binding, type Channel, type ChannelList, type Provider } from '@/lib/channels';

export function Channels() {
    const client = useQueryClient();
    const formGeneration = useRef(0);
    const generation = formGeneration.current;
    const [selected, setSelected] = useState<Provider | null>(null);
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [form, setForm] = useState<Channel | 'new' | null>(null);
    const [initialProvider, setInitialProvider] = useState<Provider>('wecom');
    const list = useQuery({ queryKey: ['channels'], queryFn: () => api.get<ChannelList>('/channels'), refetchInterval: 30000 });
    const bindings = useQuery({ queryKey: ['channel-bindings'], queryFn: () => api.get<{ items: Binding[] }>('/channels/bindings'), refetchInterval: 10000 });
    const items = list.data?.items ?? [];
    const bound = bindings.data?.items ?? [];
    const enabled = list.data?.enabled !== false;
    const knownBindings = !!bindings.data && !bindings.isError;
    const providers: Provider[] = ['wecom', 'feishu', 'dingtalk'];
    if (list.data?.wechat_enabled || items.some(c => c.provider === 'wechat')) providers.push('wechat');
    const options = items.filter(c => c.provider === selected);
    const eligible = options.filter(c => c.enabled || c.provider === 'wechat' || bound.some(b => b.channel_id === c.id));
    const current = options.find(c => c.id === selectedId) ?? (eligible.length === 1 ? eligible[0] : null);
    function close() { setSelected(null); setSelectedId(null); }
    function closeForm() { formGeneration.current++; setForm(null); }
    function edit(channel: Channel) { close(); formGeneration.current++; setForm(channel); }
    function create(provider: Provider) { close(); formGeneration.current++; setInitialProvider(provider); setForm('new'); }
    async function refresh(token: number) { await client.invalidateQueries({ queryKey: ['channels'] }); if (formGeneration.current === token) closeForm(); }
    return <div className="h-full overflow-y-auto bg-[var(--surface)]"><div className="mx-auto max-w-[960px] px-5 pb-28 pt-8 sm:px-8">
        <header className="flex flex-wrap items-center justify-between gap-4"><div className="min-w-0 basis-full sm:flex-1 sm:basis-auto"><div className="flex items-center justify-between gap-2"><h1 className="text-3xl font-semibold">IM 渠道</h1><HelpTip label="会话与记忆说明">长期记忆按 Workmate 用户与 Agent 隔离，同一账号跨渠道共用；聊天上下文和工作文件按会话隔离。群聊 @ 仅显示状态和鉴权结果入口，完整回复和审批在本人工作台处理。</HelpTip></div><p className="mt-2 text-sm text-[var(--ink-soft)]">选择平台，连接你的工作账号。</p></div></header>
        {list.isLoading && <p role="status" className="mt-6">正在加载…</p>}
        {list.isError && <p role="alert" className="mt-6">渠道加载失败。<button className="underline" onClick={() => list.refetch()}>重试</button></p>}
        {!enabled && <p role="status" className="mt-6">管理员尚未启用 IM 渠道服务。</p>}
        {bindings.isLoading && <p role="status" className="mt-4">正在加载绑定…</p>}
        {bindings.isError && <p role="alert" className="mt-4">绑定加载失败。<button className="underline" onClick={() => bindings.refetch()}>重试绑定</button></p>}
        <section aria-label="选择平台" className="mt-8 grid gap-4 sm:grid-cols-3">{providers.map(provider => {
            const count = bound.filter(b => b.provider === provider).length;
            const connections = items.filter(c => c.provider === provider);
            const canCreate = !!list.data && !list.isError && enabled && (provider === 'wechat' ? list.data.wechat_enabled : list.data.can_manage);
            const directCreate = !connections.length && canCreate;
            return <article key={provider} className="rounded-2xl border border-[var(--line)] bg-[var(--surface-raised)]">
                <button disabled={!list.data || list.isError || (!directCreate && !knownBindings)} onClick={() => { if (directCreate) create(provider); else { setSelectedId(null); setSelected(provider); } }} className="w-full rounded-2xl p-6 text-left transition-colors hover:bg-[var(--surface-subtle)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)] disabled:opacity-40"><span className="block text-lg font-semibold">{providerNames[provider]}</span><span className="mt-3 block text-sm text-[var(--ink-soft)]">{count ? `已绑定 ${count} 个账号` : connections.some(c => c.enabled) ? '连接我的账号' : directCreate ? '添加连接' : '尚未配置'}</span></button>
                {((canCreate && connections.length > 0) || connections.some(c => c.can_manage ?? (provider !== 'wechat' && list.data?.can_manage))) && <div className="space-y-3 border-t border-[var(--line)] p-4">
                    {connections.filter(c => c.can_manage ?? (provider !== 'wechat' && list.data?.can_manage)).map(c => <div key={c.id} className="flex items-start justify-between gap-2"><div className="min-w-0"><p className="break-words text-sm">{c.name}</p><p className="text-xs text-[var(--ink-soft)]">{c.enabled ? c.status === 'connected' ? '已连接' : '未连接' : '已停用'}</p>{c.last_error && <p role="status" className="break-words text-xs text-[var(--danger)]">{c.last_error}</p>}</div><button className="shrink-0 text-sm text-[var(--accent)] underline" aria-label={`编辑 ${c.name}`} onClick={() => edit(c)}>编辑</button></div>)}
                    {canCreate && connections.length > 0 && <button className={`${button} w-full`} onClick={() => create(provider)}>新增{providerNames[provider]}</button>}
                </div>}
            </article>;
        })}</section>
        <Dialog open={form !== null} onClose={closeForm} title={typeof form === 'object' && form ? `编辑 ${form.name}` : `新增${providerNames[initialProvider]}`}><div className="max-h-[calc(100dvh-12rem)] overflow-y-auto">
            {form && <ChannelForm key={typeof form === 'string' ? initialProvider : `${form.id}-${form.version}`} channel={typeof form === 'string' ? undefined : form} personal={typeof form === 'string' ? initialProvider === 'wechat' : form.provider === 'wechat'} initialProvider={initialProvider} onSaved={() => void refresh(generation)} onReload={() => void refresh(generation)} onCancel={closeForm} />}
        </div></Dialog>
        <Dialog open={selected !== null && knownBindings} onClose={close} title={current?.name ?? (selected ? providerNames[selected] : '')}><div className="max-h-[calc(100dvh-12rem)] overflow-y-auto">
            {current ? <ChannelBindingDetail key={current.id} channel={current} bindings={bound.filter(b => b.channel_id === current.id)} enabled={enabled} wechatEnabled={!!list.data?.wechat_enabled} onEdit={current.can_manage ? () => edit(current) : undefined} /> : <div className="space-y-3">
                {eligible.length > 1 && <p className="text-sm text-[var(--ink-soft)]">选择要连接的机器人：</p>}
                {options.map(c => <button key={c.id} disabled={!c.enabled && !bound.some(b => b.channel_id === c.id) && c.provider !== 'wechat'} className={`${button} block w-full text-left`} onClick={() => setSelectedId(c.id)}><span className="block">{c.name}</span><span className="text-xs text-[var(--ink-soft)]">{c.agent_name} · {bound.some(b => b.channel_id === c.id) ? '已绑定' : !c.enabled ? '已停用' : '未绑定'}</span></button>)}
                {!eligible.length && <p>暂无可用连接，请联系管理员配置。</p>}
                {enabled && selected && (selected === 'wechat' ? list.data?.wechat_enabled : list.data?.can_manage) && <button className={button} onClick={() => create(selected)}>新建{selected === 'wechat' ? '个人微信连接' : '企业渠道'}</button>}
            </div>}
        </div></Dialog>
    </div></div>;
}
