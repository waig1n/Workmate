import type { TaskStatus } from './types';
export type ScheduleRule = {
    kind: 'once';
    run_at: string;
} | {
    kind: 'daily';
    time: string;
} | {
    kind: 'weekly';
    time: string;
    weekday: number;
};
export interface ScheduleNotification { binding_id: string; on_success: boolean; on_failure: boolean; }
export interface NotificationTarget { binding_id: string; channel_id: string; channel_name: string; provider: string; external_user_id: string; available: boolean; reason: string | null; }
export interface NotificationDelivery { id: string | null; status: 'waiting' | 'pending' | 'sending' | 'sent' | 'failed' | 'unknown' | 'cancelled'; last_error: string | null; sent_at: string | null; can_retry: boolean; }
export interface ScheduleInput {
    notification: ScheduleNotification | null;
    name: string;
    agent_id: string;
    prompt: string;
    model_profile: string | null;
    timezone: string;
    rule: ScheduleRule;
}
export interface Occurrence {
    notification?: NotificationDelivery | null;
    id: string;
    scheduled_for: string;
    triggered_at: string;
    outcome: 'task_created' | 'skipped_busy' | 'missed' | 'blocked';
    reason: string | null;
    task_id: string | null;
    conversation_id: string | null;
    task_status: TaskStatus | null;
    missed_count: number;
}
export interface Schedule extends ScheduleInput {
    id: string;
    version: number;
    status: 'active' | 'paused' | 'completed';
    next_fire_at: string | null;
    pause_reason: string | null;
    agent_name: string;
    created_at: string;
    updated_at: string;
    latest_occurrence: Occurrence | null;
}
export interface SchedulePage<T> {
    items: T[];
    total: number;
}
export const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
export const scheduleStatuses = { active: '开启', paused: '暂停', completed: '已结束' };
export function formatScheduleTime(instant: string | null, timezone: string) { return instant ? new Date(instant).toLocaleString('zh-CN', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '—'; }
export function ruleLabel(rule: ScheduleRule, timezone: string) { return rule.kind === 'once' ? `一次性 · ${formatScheduleTime(rule.run_at, timezone)}` : `${rule.kind === 'daily' ? '每天' : `每${weekdays[rule.weekday]}`} ${rule.time}`; }
export function occurrenceLabel(item: Occurrence) { if (item.outcome !== 'task_created')
    return { skipped_busy: '忙时跳过', missed: '错过执行', blocked: '权限或资源阻断' }[item.outcome]; return ({ completed: '已完成', failed: '执行失败', cancelled: '已停止', interrupted: '已中断', created: '排队中', running: '执行中', awaiting_permission: '等待确认', cancelling: '正在停止' } as Record<string, string>)[item.task_status ?? ''] ?? '已创建任务'; }
export function localDateTime(instant: string, timezone: string) {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(instant));
    const p = Object.fromEntries(parts.map(x => [x.type, x.value]));
    return { date: `${p.year}-${p.month}-${p.day}`, time: `${p.hour}:${p.minute}` };
}
/** Resolve a wall-clock input in its explicit IANA zone. Reject gaps, take the first fold. */
export function zonedInstant(date: string, time: string, timezone: string): string {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(time))
        throw new Error('请填写有效日期和时间。');
    const wall = Date.parse(`${date}T${time}:00Z`);
    if (!Number.isFinite(wall))
        throw new Error('日期和时间无效。');
    const offsets = new Set<number>();
    for (const hours of [-36, 0, 36]) {
        const sample = wall + hours * 3600000;
        const local = localDateTime(new Date(sample).toISOString(), timezone);
        offsets.add(Date.parse(`${local.date}T${local.time}:00Z`) - sample);
    }
    const candidates = [...offsets].map(offset => wall - offset).filter(value => { const local = localDateTime(new Date(value).toISOString(), timezone); return local.date === date && local.time === time; }).sort((a, b) => a - b);
    if (!candidates.length)
        throw new Error('该时区不存在此本地时间，请调整日期或时间。');
    return new Date(candidates[0]).toISOString();
}
export function scheduleError(error: unknown) { return (error as {
    status?: number;
})?.status === 409 ? '计划已被修改，请重新加载计划后再保存。' : error instanceof Error ? error.message : '操作失败，请重试。'; }
