import { expect, test } from '@playwright/test';
import type { Schedule } from '../lib/schedules';
for (const mobile of [false, true])
    test(`schedule creation and history ${mobile ? 'mobile' : 'desktop'} (mock API)`, async ({ page }, testInfo) => {
        if (mobile)
            await page.setViewportSize({ width: 390, height: 844 });
        const user = { id: 'u', email: 'test@example.com', display_name: '测试', status: 'active', must_change_password: false };
        const agent = { id: 'a', display_name: '助手', slug: 'assistant', is_default: true, actions: { use: true }, unavailable_capability_ids: [] };
        let schedule: Schedule | null = null;
        let retried = false;
        await page.addInitScript(u => { localStorage.setItem('workmate_token', 'test'); localStorage.setItem('workmate_user', JSON.stringify(u)); }, user);
        await page.route('**/api/**', async (route) => {
            const path = new URL(route.request().url()).pathname;
            const method = route.request().method();
            const json = (data: unknown) => route.fulfill({ json: data });
            if (path === '/api/auth/me')
                return json(user);
            if (path === '/api/auth/me/permissions')
                return json({ permissions: [], personal_scope_id: 'personal' });
            if (path === '/api/agents')
                return json({ items: [agent], total: 1, page: 1, page_size: 100 });
            if (path === '/api/models')
                return json([{ id: 'default', display_name: '默认模型', available: true, is_default: true }]);
            if (path === '/api/schedules/notification-targets') return json({ items: [{ binding_id: 'binding', channel_id: 'ch', channel_name: '我的微信', provider: 'wechat', external_user_id: 'employee', available: true, reason: null }] });
            if (path === '/api/schedules/s/occurrences/o/notification/retry') { expect(route.request().postDataJSON()).toEqual({ confirm_duplicates: true }); retried = true; return json({ status: 'pending' }); }
            if (path === '/api/schedules/preview')
                return json({ times: ['2027-01-01T01:00:00Z', '2027-01-02T01:00:00Z', '2027-01-03T01:00:00Z'] });
            if (path === '/api/schedules' && method === 'POST') {
                const body = route.request().postDataJSON();
                expect(body).not.toHaveProperty('owner_user_id');
                expect(body.notification).toEqual({ binding_id: 'binding', on_success: true, on_failure: true });
                schedule = { ...body, id: 's', version: 1, status: 'active', agent_name: '助手', next_fire_at: '2027-01-01T01:00:00Z', pause_reason: null, latest_occurrence: null };
                return json(schedule);
            }
            if (path === '/api/schedules')
                return json({ items: schedule ? [schedule] : [], total: schedule ? 1 : 0 });
            if (path === '/api/schedules/s/pause') {
                expect(route.request().postDataJSON()).toEqual({ version: 1 });
                schedule = { ...schedule!, status: 'paused', version: 2 };
                return json(schedule);
            }
            if (path === '/api/schedules/s')
                return json(schedule);
            if (path.endsWith('/occurrences'))
                return json({ items: [{ id: 'o', scheduled_for: '2027-01-01T01:00:00Z', outcome: 'blocked', reason: 'Agent 使用权限已撤销', task_id: null, conversation_id: null, missed_count: 0, notification: { id: 'notice', status: retried ? 'pending' : 'unknown', last_error: null, sent_at: null, can_retry: !retried } }], total: 1 });
            return json([]);
        });
        await page.goto('/workspace/schedules');
        await expect(page.getByText('还没有定时任务，创建一个计划开始吧。')).toBeVisible();
        await page.getByRole('button', { name: '新建任务', exact: true }).click();
        await page.getByLabel('任务名称', { exact: true }).fill('每日简报');
        await page.getByLabel('执行 Agent').selectOption('a');
        await page.getByLabel('目标', { exact: true }).fill('汇总业务进展');
        await page.getByLabel('时区', { exact: true }).fill('Asia/Shanghai');
        await expect(page.getByText(/下一次：/)).toContainText('09:00');
        await page.getByRole('button', { name: '未来 3 次执行时间', exact: true }).click();
        await expect(page.getByText('2027/01/03 09:00')).toBeVisible();
        await page.getByLabel('通知到', { exact: true }).selectOption('binding');
        await expect(page.getByText(/上下文过期/)).toBeVisible();
        await page.screenshot({ path: testInfo.outputPath('schedule-form.png'), fullPage: true });
        await page.getByRole('button', { name: '保存', exact: true }).click();
        await expect(page.getByRole('heading', { name: '每日简报' })).toBeVisible();
        await page.getByRole('button', { name: '重试通知', exact: true }).click();
        await expect(page.getByRole('dialog')).toContainText('可能重复');
        await page.getByRole('button', { name: '确认重发通知', exact: true }).click();
        await expect(page.getByText('通知：待发送')).toBeVisible();
        await page.getByText('查看原因', { exact: true }).click();
        await expect(page.getByText('Agent 使用权限已撤销')).toBeVisible();
        await page.getByRole('button', { name: '暂停', exact: true }).click();
        await expect(page.getByRole('button', { name: '开启', exact: true })).toBeVisible();
        await page.getByRole('button', { name: '← 返回定时任务' }).click();
        const row = page.getByRole('button', { name: /每日简报/ });
        await expect(row).toBeVisible();
        await row.focus();
        await page.keyboard.press('Enter');
        await expect(page.getByRole('heading', { name: '每日简报' })).toBeVisible();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.getByText('更多', { exact: true }).click();
        await page.getByRole('button', { name: '删除', exact: true }).click();
        await expect(page.getByRole('dialog')).toBeVisible();
        await page.getByRole('button', { name: '取消', exact: true }).click();
        await expect(page.getByRole('dialog')).not.toBeVisible();
        await page.screenshot({ path: testInfo.outputPath('schedule-detail.png'), fullPage: true });
    });
