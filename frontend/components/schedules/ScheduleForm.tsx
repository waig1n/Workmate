"use client";
import { useMemo, useState, type FormEvent } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { resourceList } from '@/lib/resourceList';
import type { Agent, ModelProfile } from '@/lib/types';
import { formatScheduleTime, localDateTime, scheduleError, weekdays, zonedInstant, type Schedule, type ScheduleInput, type ScheduleRule } from '@/lib/schedules';
import { NotificationFields } from './ScheduleNotifications';
import { HelpTip } from '@/components/ui/HelpTip';
import type { ScheduleNotification } from '@/lib/schedules';
const field = 'mt-1.5 w-full rounded-xl border border-[var(--line)] bg-[var(--surface-raised)] px-3 py-2.5 text-sm focus:outline-2 focus:outline-[var(--accent)]';
export function ScheduleForm({ schedule, onSaved, onCancel, onReload }: {
    schedule?: Schedule;
    onSaved: (schedule: Schedule) => void;
    onCancel: () => void;
    onReload: () => void;
}) {
    const [name, setName] = useState(schedule?.name ?? '');
    const [agentId, setAgentId] = useState(schedule?.agent_id ?? '');
    const [prompt, setPrompt] = useState(schedule?.prompt ?? '');
    const [timezone, setTimezone] = useState(schedule?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'UTC');
    const [kind, setKind] = useState<ScheduleRule['kind']>(schedule?.rule.kind ?? 'daily');
    const initial = schedule?.rule.kind === 'once' ? localDateTime(schedule.rule.run_at, schedule.timezone) : null;
    const [date, setDate] = useState(initial?.date ?? '');
    const [time, setTime] = useState(initial?.time ?? (schedule?.rule.kind !== 'once' ? schedule?.rule.time : undefined) ?? '09:00');
    const [weekday, setWeekday] = useState(schedule?.rule.kind === 'weekly' ? schedule.rule.weekday : 0);
    const [model, setModel] = useState(schedule?.model_profile ?? '');
    const [notification, setNotification] = useState<ScheduleNotification | null>(schedule?.notification ?? null);
    const [validation, setValidation] = useState('');
    const agents = useQuery({ queryKey: ['agents'], queryFn: () => resourceList<Agent>('/agents') });
    const models = useQuery({ queryKey: ['models'], queryFn: () => api.get<ModelProfile[]>('/models') });
    const usable = (agents.data ?? []).filter(agent => agent.actions.use);
    const timing = useMemo(() => { try {
        new Intl.DateTimeFormat('en', { timeZone: timezone });
        const rule: ScheduleRule = kind === 'once' ? { kind, run_at: zonedInstant(date, time, timezone) } : kind === 'weekly' ? { kind, time, weekday } : { kind, time };
        return { rule, error: null };
    }
    catch (error) {
        return { rule: null, error: error instanceof RangeError ? '请输入有效的 IANA 时区。' : scheduleError(error) };
    } }, [kind, date, time, weekday, timezone]);
    const preview = useQuery({ queryKey: ['schedule-preview', timezone, timing.rule], queryFn: () => api.post<{
            times: string[];
        }>('/schedules/preview', { timezone, rule: timing.rule }), enabled: !!timing.rule, retry: false, staleTime: 0 });
    const save = useMutation({ mutationFn: (body: ScheduleInput) => schedule ? api.patch<Schedule>(`/schedules/${schedule.id}`, { ...body, version: schedule.version }) : api.post<Schedule>('/schedules', body), onSuccess: onSaved });
    function submit(event: FormEvent) { event.preventDefault(); if (!name.trim() || !prompt.trim() || !usable.some(a => a.id === agentId) || !timing.rule) {
        setValidation('请填写名称、目标，并选择可用 Agent 和有效时间。');
        return;
    } if (model && !models.data?.some(m => m.id === model && m.available !== false)) {
        setValidation('所选模型不可用，请重新选择。');
        return;
    } if (notification && !notification.on_success && !notification.on_failure) { setValidation('至少选择一种通知时机。'); return; } setValidation(''); save.mutate({ notification, name: name.trim(), agent_id: agentId, prompt: prompt.trim(), model_profile: model || null, timezone, rule: timing.rule }); }
    return <form onSubmit={submit} className="mx-auto mt-7 max-w-2xl space-y-5">
 <label className="block text-sm font-medium">任务名称<input required maxLength={80} className={field} value={name} onChange={e => setName(e.target.value)}/></label>
 <label className="block text-sm font-medium">执行 Agent<select required className={field} value={agentId} onChange={e => setAgentId(e.target.value)}><option value="">请选择 Agent</option>{schedule && !usable.some(a => a.id === schedule.agent_id) && <option value={schedule.agent_id} disabled>{schedule.agent_name}（不可用）</option>}{usable.map(a => <option key={a.id} value={a.id}>{a.display_name}</option>)}</select></label>
 {agents.isError ? <p role="alert">Agent 加载失败。<button type="button" className="underline" onClick={() => agents.refetch()}>重试</button></p> : !agents.isLoading && !usable.length && <p role="status" className="text-sm text-[var(--ink-soft)]">暂无可用 Agent，请联系管理员开通访问权限。</p>}
 <label className="block text-sm font-medium">目标<textarea required maxLength={16000} rows={4} className={field} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="描述希望定期完成的工作"/></label>
 <div className="grid gap-4 sm:grid-cols-3"><label className="text-sm font-medium">频率<select className={field} value={kind} onChange={e => setKind(e.target.value as ScheduleRule['kind'])}><option value="once">一次性</option><option value="daily">每天</option><option value="weekly">每周</option></select></label>
 {kind === 'once' && <label className="text-sm font-medium">日期<input required type="date" className={field} value={date} onChange={e => setDate(e.target.value)}/></label>}{kind === 'weekly' && <label className="text-sm font-medium">星期<select className={field} value={weekday} onChange={e => setWeekday(Number(e.target.value))}>{weekdays.map((day, i) => <option key={day} value={i}>{day}</option>)}</select></label>}
 <label className="text-sm font-medium">时间<input required type="time" className={field} value={time} onChange={e => setTime(e.target.value)}/></label></div>
 <label className="block text-sm font-medium">时区<input required className={field} value={timezone} onChange={e => setTimezone(e.target.value)} placeholder="Asia/Shanghai" spellCheck={false}/></label>
 <div className="text-sm text-[var(--ink-soft)]" aria-live="polite">{timing.error ? <p role="alert">{timing.error}</p> : preview.isError ? <p role="alert">{scheduleError(preview.error)} <button type="button" className="underline" onClick={() => preview.refetch()}>重试时间预览</button></p> : preview.isFetching ? <p>正在计算执行时间…</p> : preview.data?.times.length ? <div className="flex items-center justify-between gap-2"><p>下一次：{formatScheduleTime(preview.data.times[0], timezone)}（{timezone}）</p>{preview.data.times.length > 1 && <HelpTip label="未来 3 次执行时间"><p className="font-medium text-[var(--ink)]">未来 3 次执行时间</p><ul className="mt-2 space-y-1">{preview.data.times.slice(0, 3).map(t => <li key={t}>{formatScheduleTime(t, timezone)}</li>)}</ul></HelpTip>}</div> : <p>没有可执行的未来时间。</p>}</div>
 <NotificationFields value={notification} onChange={setNotification}/>
 <details><summary className="cursor-pointer text-sm text-[var(--ink-soft)]">更多设置</summary><label className="mt-3 block text-sm">模型<select className={field} value={model} onChange={e => setModel(e.target.value)}><option value="">使用系统默认模型</option>{model && !models.data?.some(m => m.id === model) && <option value={model} disabled>{model}（不可用）</option>}{models.data?.map(m => <option key={m.id} value={m.id} disabled={m.available === false}>{m.display_name}{m.available === false ? '（不可用）' : ''}</option>)}</select></label>{models.isError && <p role="alert">模型加载失败。<button type="button" className="underline" onClick={() => models.refetch()}>重试模型列表</button></p>}</details>
 <p className="rounded-xl bg-[var(--surface-subtle)] p-3 text-xs leading-5 text-[var(--ink-soft)]">需要确认的操作会自动拒绝，并在结果中说明。</p>
 {(validation || save.isError) && <p role="alert" className="text-sm text-[var(--danger)]">{validation || scheduleError(save.error)}{(save.error as {
        status?: number;
    })?.status === 409 && <button type="button" className="ml-2 underline" onClick={onReload}>重新加载计划</button>}</p>}
 <div className="flex justify-end gap-3"><button type="button" onClick={onCancel} disabled={save.isPending} className="rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm">取消</button><button disabled={save.isPending || agents.isLoading || !usable.some(a => a.id === agentId) || !preview.data?.times.length || preview.isFetching || preview.isError || !timing.rule} className="rounded-xl bg-[var(--ink)] px-5 py-2.5 text-sm text-[var(--surface)] disabled:opacity-40">{save.isPending ? '正在保存…' : '保存'}</button></div>
 </form>;
}
