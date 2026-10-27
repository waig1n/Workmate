export type Provider = 'wecom' | 'feishu' | 'dingtalk' | 'wechat';
export interface Channel {
    id: string;
    name: string;
    provider: Provider;
    agent_id: string;
    agent_name: string;
    enabled: boolean;
    version: number;
    status: string;
    last_error?: string | null;
    group_enabled: boolean;
    group_allowlist?: string[];
    has_credentials: boolean;
    can_manage?: boolean;
}
export interface ChannelList {
    items: Channel[];
    can_manage: boolean;
    wechat_enabled: boolean;
    enabled?: boolean;
}
export interface Binding {
    id: string;
    channel_id: string;
    channel_name: string;
    provider: Provider;
    external_user_id: string;
}
export const providerNames: Record<Provider, string> = { wecom: '企业微信', feishu: '飞书', dingtalk: '钉钉', wechat: '个人微信（试验）' };
export const credentialFields: Record<Provider, [
    string,
    string,
    boolean?
][]> = { wecom: [['bot_id', 'Bot ID'], ['secret', 'Bot Secret']], feishu: [['app_id', 'App ID'], ['app_secret', 'App Secret']], dingtalk: [['client_id', 'Client ID'], ['client_secret', 'Client Secret'], ['card_template_id', '流式卡片模板 ID（可选）', true]], wechat: [] };
const safeQrConflicts = new Set([
    '二维码已过期或已更换，请重新获取', '更换微信账号需新建渠道', '微信身份已绑定',
    '该微信账号已有连接', '渠道配置已更新，请重新获取二维码',
]);
export function channelError(error: unknown): string {
    if ((error as {
        status?: number;
    })?.status === 409) {
        if (error instanceof Error && safeQrConflicts.has(error.message)) return error.message;
        return '渠道已被修改，请重新加载后再保存。';
    }
    return error instanceof Error ? error.message : '操作失败，请重试。';
}
export function safeLoginUrl(value: string): string | null {
    try {
        const url = new URL(value);
        return url.protocol === 'https:' && url.hostname === 'liteapp.weixin.qq.com' ? url.href : null;
    }
    catch {
        return null;
    }
}
export const channelButton = 'rounded-xl border border-[var(--line)] px-4 py-2 text-sm hover:bg-[var(--surface-subtle)] disabled:opacity-40';
export const channelField = 'mt-1.5 w-full rounded-xl border border-[var(--line)] bg-[var(--surface-raised)] px-3 py-2.5 text-sm';
