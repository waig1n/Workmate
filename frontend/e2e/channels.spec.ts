import { expect, test } from '@playwright/test';
for (const mobile of [false, true])
    test(`channel management and staff binding ${mobile ? 'mobile' : 'desktop'} (mock API)`, async ({ page }, testInfo) => {
        if (mobile)
            await page.setViewportSize({ width: 390, height: 844 });
        const user = { id: 'u', email: 'test@example.com', display_name: '测试', status: 'active', must_change_password: false };
        let channel: Record<string, unknown> | null = null;
        let bound = false;
        let personal = false;
        let wxLoggedIn = false;
        await page.addInitScript(u => { localStorage.setItem('workmate_token', 'test'); localStorage.setItem('workmate_user', JSON.stringify(u)); }, user);
        await page.route('**/api/**', async (route) => {
            const path = new URL(route.request().url()).pathname;
            const method = route.request().method();
            const json = (data: unknown) => route.fulfill({ json: data });
            if (path === '/api/auth/me')
                return json(user);
            if (path === '/api/auth/me/permissions')
                return json({ permissions: [], personal_scope_id: 'p' });
            if (path === '/api/agents')
                return json({ items: [{ id: 'a', display_name: '助手', actions: { use: true } }], total: 1, page: 1, page_size: 100 });
            if (path === '/api/channels' && method === 'POST') {
                const body = route.request().postDataJSON();
                expect(body.credentials).toEqual({ bot_id: 'bot', secret: 'secret' });
                expect(body).not.toHaveProperty('owner_user_id');
                channel = { ...body, credentials: undefined, id: 'ch', version: 1, agent_name: '助手', enabled: true, status: 'connected', has_credentials: true, can_manage: true };
                return json(channel);
            }
            if (path === '/api/channels')
                return json({ items: [...(channel ? [channel] : []), ...(personal ? [{ id: 'wx', name: '我的微信', provider: 'wechat', agent_id: 'a', agent_name: '助手', enabled: wxLoggedIn, version: wxLoggedIn ? 2 : 1, status: 'disconnected', has_credentials: wxLoggedIn, can_manage: true }] : [])], can_manage: true, wechat_enabled: personal, enabled: true });
            if (path === '/api/channels/ch' && method === 'PATCH') {
                const body = route.request().postDataJSON();
                expect(body).not.toHaveProperty('credentials');
                expect(body.version).toBe(1);
                channel = { ...channel, ...body, version: 2 };
                return json(channel);
            }
            if (path === '/api/channels/wx/qr-status') { wxLoggedIn = true; return json({ status: 'confirmed' }); }
            if (path === '/api/channels/wx/qr')
                return json({ qrcode: 'test-session', qrcode_url: 'https://liteapp.weixin.qq.com/test-login' });
            if (path === '/api/channels/ch/binding-code')
                return json({ code: 'ONE-TIME', expires_at: '2026-12-01T01:00:00Z' });
            if (path === '/api/channels/bindings/b' && method === 'DELETE') { bound = false; return json({}); }
            if (path === '/api/channels/bindings')
                return json({ items: bound ? [{ id: 'b', channel_id: 'ch', channel_name: '办公机器人', provider: 'wecom', external_user_id: 'staff' }] : [] });
            return json([]);
        });
        await page.goto('/workspace/channels');
        await page.getByRole('button', { name: /^企业微信/ }).click();
        await expect(page.getByRole('dialog')).toBeVisible();
        await expect(page.getByRole('dialog').getByLabel('平台')).toHaveValue('wecom');
        await page.getByLabel('渠道名称').fill('办公机器人');
        await page.getByLabel('默认 Agent').selectOption('a');
        await page.getByLabel('Bot ID', { exact: true }).fill('bot');
        await page.getByLabel('Bot Secret').fill('secret');
        await page.getByLabel('允许群聊 @ 触发').check();
        await page.getByLabel(/允许接入的群 ID/).fill('group1');
        await page.getByRole('button', { name: '保存', exact: true }).click();
        await expect(page.getByRole('dialog')).not.toBeVisible();
        await page.getByRole('button', { name: /编辑.*办公机器人/ }).click();
        await expect(page.getByLabel('Bot Secret')).toHaveValue('');
        await expect(page.getByLabel('Bot Secret')).toHaveAttribute('placeholder', '********');
        await page.getByRole('button', { name: '保存', exact: true }).click();
        await expect(page.getByRole('dialog')).not.toBeVisible();
        await page.getByRole('button', { name: '新增企业微信', exact: true }).click();
        await expect(page.getByRole('dialog').getByLabel('平台')).toHaveValue('wecom');
        await expect(page.getByLabel('渠道名称')).toHaveValue('');
        await page.getByRole('button', { name: '取消', exact: true }).click();
        await page.getByRole('button', { name: /^钉钉/ }).click();
        await expect(page.getByLabel('流式卡片模板 ID（可选）')).not.toBeVisible();
        await page.getByText('流式回复配置（可选）', { exact: true }).click();
        await expect(page.getByLabel('流式卡片模板 ID（可选）')).toBeVisible();
        await page.getByRole('button', { name: '取消', exact: true }).click();
        await page.screenshot({ path: testInfo.outputPath('channels-platforms.png'), fullPage: true, animations: 'disabled' });
        await page.getByRole('button', { name: '会话与记忆说明' }).click();
        await expect(page.getByRole('tooltip')).toBeVisible();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.keyboard.press('Escape');
        await page.getByRole('button', { name: /^企业微信/ }).click();
        await expect(page.getByText('/bind ONE-TIME')).toBeVisible();
        await page.screenshot({ path: testInfo.outputPath('channels-binding.png'), fullPage: true, animations: 'disabled' });
        bound = true;
        await page.reload();
        await page.getByRole('button', { name: /^企业微信/ }).click();
        await expect(page.getByText('staff', { exact: true })).toBeVisible();
        await expect(page.getByText('/bind ONE-TIME')).not.toBeVisible();
        await page.getByRole('button', { name: '解除绑定' }).click();
        await expect(page.getByText('staff', { exact: true })).not.toBeVisible();
        personal = true;
        await page.reload();
        await page.getByRole('button', { name: /^个人微信/ }).click();
        await page.getByRole('button', { name: '微信扫码登录', exact: true }).click();
        await expect(page.getByRole('img', { name: '微信登录二维码' })).toBeVisible();
        await page.screenshot({ path: testInfo.outputPath('wechat-qr.png'), fullPage: true, animations: 'disabled' });
        await expect(page.getByText('登录成功，渠道已启用')).toBeVisible({ timeout: 10000 });
        await expect(page.getByRole('img', { name: '微信登录二维码' })).not.toBeVisible();
        await page.getByRole('button', { name: '编辑连接', exact: true }).click();
        await expect(page.getByLabel('启用渠道')).toBeChecked();
        await expect(page.getByLabel('启用渠道')).toBeEnabled();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });
