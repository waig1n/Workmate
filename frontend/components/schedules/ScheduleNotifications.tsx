"use client";
import { useId, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { Dialog } from '@/components/ui/Dialog';
import type { NotificationDelivery, NotificationTarget, ScheduleNotification } from '@/lib/schedules';
const button = 'rounded-xl border border-[var(--line)] px-3 py-2 text-sm disabled:opacity-40';
export function NotificationFields({ value, onChange }: { value: ScheduleNotification | null; onChange: (value: ScheduleNotification | null) => void }) {
 const selectId = useId();
 const targets = useQuery({ queryKey: ['schedule-notification-targets'], queryFn: () => api.get<{ items: NotificationTarget[] }>('/schedules/notification-targets'), refetchInterval: 30000 });
 const items = targets.data?.items ?? [];
 const current = items.find(item => item.binding_id === value?.binding_id);
 return <section className="space-y-3">
  <div><label htmlFor={selectId} className="block text-sm font-medium">通知到</label><select id={selectId} className="mt-1.5 w-full rounded-xl border border-[var(--line)] bg-[var(--surface-raised)] px-3 py-2.5 text-sm" value={value?.binding_id ?? ''} onChange={e => onChange(e.target.value ? { binding_id: e.target.value, on_success: true, on_failure: true } : null)}>
   <option value="">不发送通知</option>
   {value && !current && <option value={value.binding_id} disabled>原通知目标（暂不可用）</option>}
   {items.map(item => <option key={item.binding_id} value={item.binding_id} disabled={!item.available}>{item.channel_name} · {item.external_user_id}{item.available ? '' : '（不可用）'}</option>)}
  </select></div>
  {targets.isLoading && <p className="text-xs text-[var(--ink-soft)]">正在加载通知目标…</p>}
  {targets.isError && <p role="alert" className="text-sm">通知目标加载失败。<button type="button" className="underline" onClick={() => targets.refetch()}>重试通知目标</button></p>}
  {value && <><div className="flex flex-wrap gap-4 text-sm"><label><input type="checkbox" checked={value.on_success} onChange={e => onChange({ ...value, on_success: e.target.checked })}/> 执行成功时</label><label><input type="checkbox" checked={value.on_failure} onChange={e => onChange({ ...value, on_failure: e.target.checked })}/> 执行失败时</label></div>
   {!value.on_success && !value.on_failure && <p role="alert" className="text-sm">至少选择一种通知时机。</p>}
   {current && !current.available && <p role="status" className="text-xs text-[var(--ink-soft)]">{current.reason || '当前通知目标不可用，请重新选择或关闭通知。'}</p>}
   {current?.provider === 'wechat' && <p className="text-xs text-[var(--ink-soft)]">个人微信上下文过期可能导致投递失败，可先向机器人发送消息更新上下文。</p>}
  </>}
 </section>;
}
const labels = { waiting: '等待执行结果', pending: '待发送', sending: '发送中', sent: '已发送', failed: '发送失败', unknown: '回执未知', cancelled: '已取消' };
export function OccurrenceNotification({ scheduleId, occurrenceId, notification }: { scheduleId: string; occurrenceId: string; notification?: NotificationDelivery | null }) {
 const client = useQueryClient();
 const [confirm, setConfirm] = useState(false);
 const retry = useMutation({ mutationFn: (confirmed: boolean) => api.post(`/schedules/${scheduleId}/occurrences/${occurrenceId}/notification/retry`, { confirm_duplicates: confirmed }), onSuccess: async () => { setConfirm(false); await client.invalidateQueries({ queryKey: ['schedule-occurrences', scheduleId] }); } });
 if (!notification) return null;
 return <div className="mt-3 space-y-2 text-xs text-[var(--ink-soft)]"><div className="flex flex-wrap items-center gap-3"><span>通知：{labels[notification.status]}</span>{notification.can_retry && <button className={button} disabled={retry.isPending} onClick={() => notification.status === 'unknown' ? setConfirm(true) : retry.mutate(false)}>重试通知</button>}</div>
 {notification.last_error && <p className="break-words">{notification.last_error}</p>}
 {retry.isError && !confirm && <p role="alert">{retry.error instanceof Error ? retry.error.message : '通知重试失败，请重试。'}</p>}
 <Dialog open={confirm} onClose={() => { if (!retry.isPending) setConfirm(false); }} title="重发通知？" description="上次发送回执未知，重发可能重复送达。仅重发通知，不会重新执行任务。" footer={<><button className={button} disabled={retry.isPending} onClick={() => setConfirm(false)}>取消</button><button className={button} disabled={retry.isPending} onClick={() => retry.mutate(true)}>确认重发通知</button></>}><p className="text-sm">请先检查聊天中是否已经收到结果。</p>{retry.isError && <p role="alert" className="mt-3 text-sm">{retry.error instanceof Error ? retry.error.message : '通知重试失败，请重试。'}</p>}</Dialog>
 </div>;
}
