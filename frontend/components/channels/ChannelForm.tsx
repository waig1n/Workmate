"use client";
import { useState, type FormEvent } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { resourceList } from '@/lib/resourceList';
import type { Agent } from '@/lib/types';
import { channelButton as button, channelField as field, channelError, credentialFields, providerNames, type Channel, type Provider } from '@/lib/channels';
export function ChannelForm({ channel, personal, initialProvider, onSaved, onCancel, onReload }: {
    channel?: Channel;
    personal: boolean;
    initialProvider?: Provider;
    onSaved: () => void;
    onCancel: () => void;
    onReload: () => void;
}) {
    const [provider, setProvider] = useState<Provider>(channel?.provider ?? initialProvider ?? (personal ? 'wechat' : 'wecom'));
    const [name, setName] = useState(channel?.name ?? '');
    const [agent, setAgent] = useState(channel?.agent_id ?? '');
    const [enabled, setEnabled] = useState(channel?.enabled ?? true);
    const [group, setGroup] = useState(channel?.group_enabled ?? false);
    const [groups, setGroups] = useState(channel?.group_allowlist?.join('\n') ?? '');
    const [credentials, setCredentials] = useState<Record<string, string>>({});
    const [validation, setValidation] = useState('');
    const agents = useQuery({ queryKey: ['channel-agents'], queryFn: () => resourceList<Agent>('/agents') });
    const usable = (agents.data ?? []).filter(a => personal ? a.actions.use : a.actions.use || a.actions.update);
    const save = useMutation({ mutationFn: (body: Record<string, unknown>) => channel ? api.patch(`/channels/${channel.id}`, { ...body, version: channel.version }) : api.post('/channels', body), onSuccess: onSaved });
    function submit(e: FormEvent) {
        e.preventDefault();
        const allowlist = groups.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
        if (!name.trim() || !agent || (group && !allowlist.length)) {
            setValidation('请填写名称、默认 Agent 和允许接入的群 ID。');
            return;
        }
        const replacing = Object.values(credentials).some(v => v.trim());
        if (provider !== 'wechat' && (!channel || replacing) && credentialFields[provider].some(([key, , optional]) => !optional && !credentials[key]?.trim())) {
            setValidation('更换凭据时请完整填写所有必填凭据字段。');
            return;
        }
        setValidation('');
        save.mutate({ name: name.trim(), agent_id: agent, group_enabled: group, group_allowlist: allowlist, ...(channel ? { enabled } : { provider }), ...(!channel || replacing ? { credentials: Object.fromEntries(Object.entries(credentials).filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()])) } : {}) });
    }
    return <form className="mt-6 max-w-xl space-y-5" onSubmit={submit}>
 <label className="block text-sm">渠道名称<input className={field} required maxLength={100} value={name} onChange={e => setName(e.target.value)}/></label>
 <label className="block text-sm">平台<select className={field} disabled={!!channel || personal} value={provider} onChange={e => { setProvider(e.target.value as Provider); setCredentials({}); }}>{Object.entries(providerNames).filter(([key]) => personal ? key === 'wechat' : key !== 'wechat').map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
 <label className="block text-sm">默认 Agent<select className={field} required value={agent} onChange={e => setAgent(e.target.value)}><option value="">请选择 Agent</option>{channel && !usable.some(a => a.id === agent) && <option value={agent}>{channel.agent_name}（当前不可用）</option>}{usable.map(a => <option key={a.id} value={a.id}>{a.display_name}</option>)}</select></label>
 {agents.isError && <p role="alert">Agent 加载失败。<button type="button" onClick={() => agents.refetch()}>重试</button></p>}
 {credentialFields[provider].filter(([, , optional]) => !optional).map(([key, label]) => <label className="block text-sm" key={key}>{label}<input className={field} type="password" autoComplete="new-password" placeholder={channel?.has_credentials ? '********' : undefined} value={credentials[key] ?? ''} onChange={e => setCredentials({ ...credentials, [key]: e.target.value })}/></label>)}
 {channel && provider !== 'wechat' && <p className="text-xs text-[var(--ink-soft)]">修改其他设置无需重新填写凭据。凭据仅写入，全部留空保留现有凭据；更换时需完整填写。</p>}
 {channel && provider === 'dingtalk' && (credentials.client_id?.trim() || credentials.client_secret?.trim()) && <p role="status" className="text-xs leading-5 text-[var(--ink-soft)]">更换 Client 凭据时，如需保留流式回复，请展开下方配置并一并填写原模板 ID；模板留空将改为整段回复。</p>}
 {provider === 'dingtalk' && <details className="rounded-xl border border-[var(--line)] p-3 text-sm"><summary className="cursor-pointer text-[var(--ink-soft)]">流式回复配置（可选）</summary><div className="mt-3 space-y-3">
 {credentialFields[provider].filter(([, , optional]) => optional).map(([key, label]) => <label className="block text-sm" key={key}>{label}<input className={field} type="password" autoComplete="new-password" value={credentials[key] ?? ''} onChange={e => setCredentials({ ...credentials, [key]: e.target.value })}/></label>)}
 <p className="text-xs leading-5 text-[var(--ink-soft)]">配置已发布的 AI 卡片模板以启用流式回复，模板需包含 content 字符串流式字段；新建时留空使用整段回复，编辑时所有凭据留空保留现有配置。修改模板时需重新填写 Client ID 和 Client Secret。</p>
 </div></details>}
 {provider !== 'wechat' && <><label className="flex gap-2 text-sm"><input type="checkbox" checked={group} onChange={e => setGroup(e.target.checked)}/>允许群聊 @ 触发</label>{group && <label className="block text-sm">允许接入的群 ID<textarea className={field} required rows={3} value={groups} onChange={e => setGroups(e.target.value)}/><span className="text-xs text-[var(--ink-soft)]">每行一个群 ID。群内仅发送状态，完整结果在本人工作台查看。</span></label>}</>}
 {channel && provider === 'wechat' && !channel.has_credentials && <p className="text-xs text-[var(--ink-soft)]">请先扫码登录，登录成功后渠道将自动启用。</p>}
 {channel && <label className="flex gap-2 text-sm"><input type="checkbox" disabled={provider === 'wechat' && !channel.has_credentials} checked={enabled} onChange={e => setEnabled(e.target.checked)}/>启用渠道</label>}
 {(validation || save.isError) && <p role="alert" className="text-sm text-[var(--danger)]">{validation || channelError(save.error)}{(save.error as {
        status?: number;
    })?.status === 409 && <button type="button" className="ml-2 underline" onClick={onReload}>重新加载</button>}</p>}
 <div className="flex gap-3"><button className={button} disabled={save.isPending || agents.isLoading}>保存</button><button className={button} type="button" disabled={save.isPending} onClick={onCancel}>取消</button></div>
 </form>;
}
