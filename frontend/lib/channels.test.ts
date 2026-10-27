import { expect, it } from 'vitest';
import { safeLoginUrl, channelError } from './channels';
it('accepts only official HTTPS login links', () => { expect(safeLoginUrl('https://liteapp.weixin.qq.com/login')).toBe('https://liteapp.weixin.qq.com/login'); for (const link of ['', 'javascript:alert(1)', 'https://liteapp.weixin.qq.com.evil.test', 'http://liteapp.weixin.qq.com'])
    expect(safeLoginUrl(link)).toBeNull(); });
it('provides retry/conflict copy', () => { expect(channelError(null)).toContain('重试'); expect(channelError(new Error('拒绝'))).toBe('拒绝'); expect(channelError({ status: 409 })).toContain('已被修改'); });
it.each(['二维码已过期或已更换，请重新获取', '更换微信账号需新建渠道', '微信身份已绑定', '该微信账号已有连接', '渠道配置已更新，请重新获取二维码'])('preserves fixed safe QR conflict: %s', message => {
    expect(channelError(Object.assign(new Error(message), { status: 409 }))).toBe(message);
});
it('does not disclose unknown conflict details or messages containing a safe substring', () => {
    for (const message of ['private token=secret', '二维码已过期或已更换，请重新获取 secret']) {
        expect(channelError(Object.assign(new Error(message), { status: 409 }))).toBe('渠道已被修改，请重新加载后再保存。');
    }
});
