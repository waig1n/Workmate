import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { NotificationFields, OccurrenceNotification } from './ScheduleNotifications';
import type { ScheduleNotification } from '@/lib/schedules';
import { api } from '@/lib/api';
vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }));
const target = { binding_id: 'b', channel_id: 'c', channel_name: '我的微信', provider: 'wechat', external_user_id: 'me', available: true, reason: null };
function Form({ initial = null }: { initial?: ScheduleNotification | null }) {
 const [value, setValue] = useState(initial);
 return <><NotificationFields value={value} onChange={setValue}/><output>{JSON.stringify(value)}</output></>;
}
function mount(child: React.ReactNode) { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{child}</QueryClientProvider>); }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.get).mockResolvedValue({ items: [target] } as never); vi.mocked(api.post).mockResolvedValue({} as never); });
it('defaults off and explicitly selects an identity with outcome preferences', async () => {
 mount(<Form/>);
 expect(screen.getByLabelText('通知到')).toHaveValue('');
 await screen.findByRole('option', { name: /我的微信/ });
 fireEvent.change(screen.getByLabelText('通知到'), { target: { value: 'b' } });
 expect(screen.getByLabelText('执行成功时')).toBeChecked();
 expect(screen.getByText(/上下文过期/)).toBeVisible();
 fireEvent.click(screen.getByLabelText('执行失败时'));
 expect(screen.getByRole('status')).toHaveTextContent('"on_failure":false');
 fireEvent.change(screen.getByLabelText('通知到'), { target: { value: '' } });
 expect(screen.getByRole('status')).toHaveTextContent('null');
});
it('retains a missing selected binding instead of silently selecting another', async () => {
 mount(<Form initial={{ binding_id: 'gone', on_success: true, on_failure: true }}/>);
 await screen.findByRole('option', { name: /我的微信/ });
 expect(screen.getByLabelText('通知到')).toHaveValue('gone');
 expect(screen.getByRole('option', { name: /原通知目标/ })).toBeDisabled();
});
it('confirms unknown delivery and retries only the notification', async () => {
 mount(<OccurrenceNotification scheduleId="s" occurrenceId="o" notification={{ id: 'n', status: 'unknown', last_error: '回执未知', sent_at: null, can_retry: true }}/>);
 fireEvent.click(screen.getByRole('button', { name: '重试通知' }));
 expect(api.post).not.toHaveBeenCalled();
 expect(screen.getByRole('dialog')).toHaveTextContent('可能重复');
 fireEvent.click(screen.getByRole('button', { name: '确认重发通知' }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith('/schedules/s/occurrences/o/notification/retry', { confirm_duplicates: true }));
});

it('shows unavailable target reason without changing selection', async () => {
 vi.mocked(api.get).mockResolvedValue({ items: [{ ...target, available: false, reason: '连接已停用' }] } as never);
 mount(<Form initial={{ binding_id: 'b', on_success: true, on_failure: false }}/>);
 expect(await screen.findByText('连接已停用')).toBeVisible();
 expect(screen.getByLabelText('通知到')).toHaveValue('b');
 expect(screen.getByRole('option', { name: /我的微信/ })).toBeDisabled();
});
it('keeps notification off when loading targets fails', async () => {
 vi.mocked(api.get).mockRejectedValue(new Error('offline'));
 mount(<Form/>);
 expect(await screen.findByRole('alert')).toHaveTextContent('通知目标加载失败');
 expect(screen.getByRole('status')).toHaveTextContent('null');
 expect(screen.getByRole('button', { name: '重试通知目标' })).toBeVisible();
});
it('retries failed notification without duplicate confirmation and refreshes history', async () => {
 const client = new QueryClient();
 const invalidate = vi.spyOn(client, 'invalidateQueries');
 render(<QueryClientProvider client={client}><OccurrenceNotification scheduleId="s" occurrenceId="o" notification={{ id: 'n', status: 'failed', last_error: null, sent_at: null, can_retry: true }}/></QueryClientProvider>);
 fireEvent.click(screen.getByRole('button', { name: '重试通知' }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith('/schedules/s/occurrences/o/notification/retry', { confirm_duplicates: false }));
 await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['schedule-occurrences', 's'] }));
 expect(screen.queryByRole('dialog')).toBeNull();
});
it('does not offer a retry without server permission', () => {
 mount(<OccurrenceNotification scheduleId="s" occurrenceId="o" notification={{ id: 'n', status: 'cancelled', last_error: null, sent_at: null, can_retry: false }}/>);
 expect(screen.getByText('通知：已取消')).toBeVisible();
 expect(screen.queryByRole('button')).toBeNull();
});

it('can cancel unknown retry and keeps retry errors inside confirmation dialog', async () => {
 vi.mocked(api.post).mockRejectedValue(new Error('通知目标已失效'));
 mount(<OccurrenceNotification scheduleId="s" occurrenceId="o" notification={{ id: 'n', status: 'unknown', last_error: null, sent_at: null, can_retry: true }}/>);
 fireEvent.click(screen.getByRole('button', { name: '重试通知' }));
 fireEvent.click(screen.getByRole('button', { name: '取消' }));
 expect(api.post).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button', { name: '重试通知' }));
 fireEvent.click(screen.getByRole('button', { name: '确认重发通知' }));
 expect(await screen.findByRole('alert')).toHaveTextContent('通知目标已失效');
 expect(screen.getByRole('dialog')).toContainElement(screen.getByRole('alert'));
});

it('shows waiting separately before a delivery exists and offers no retry', () => {
 mount(<OccurrenceNotification scheduleId="s" occurrenceId="o" notification={{ id: null, status: 'waiting', last_error: null, sent_at: null, can_retry: false }}/>);
 expect(screen.getByText('通知：等待执行结果')).toBeVisible();
 expect(screen.queryByRole('button')).toBeNull();
});

it('associates the notification select with a standalone visible label', async () => {
 mount(<Form/>);
 await screen.findByRole('option', { name: /我的微信/ });
 const select = screen.getByRole('combobox', { name: '通知到' });
 expect(select).toHaveAttribute('id');
 const label = document.querySelector(`label[for="${select.id}"]`);
 expect(label).toHaveTextContent(/^通知到$/);
 expect(label).not.toContainElement(select);
});
