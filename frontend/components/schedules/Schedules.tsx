"use client";
import Link from 'next/link';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { Dialog } from '@/components/ui/Dialog';
import { OccurrenceNotification } from './ScheduleNotifications';
import { ScheduleForm } from './ScheduleForm';
import { formatScheduleTime, occurrenceLabel, ruleLabel, scheduleError, scheduleStatuses, type Schedule, type Occurrence, type SchedulePage } from '@/lib/schedules';
const button = 'rounded-xl border border-[var(--line)] px-4 py-2.5 text-sm hover:bg-[var(--surface-subtle)] disabled:opacity-40';
function Pagination({ offset, total, onChange, label }: {
    offset: number;
    total: number;
    onChange: (offset: number) => void;
    label: string;
}) { return total > 20 ? <div className="mt-5 flex items-center justify-end gap-3 text-sm"><button className={button} disabled={!offset} onClick={() => onChange(Math.max(0, offset - 20))} aria-label={`上一页${label}`}>上一页</button><span>{Math.floor(offset / 20) + 1} / {Math.ceil(total / 20)}</span><button className={button} disabled={offset + 20 >= total} onClick={() => onChange(offset + 20)} aria-label={`下一页${label}`}>下一页</button></div> : null; }
export function Schedules() {
    const [selected, setSelected] = useState<string | null>(null);
    const [form, setForm] = useState(false);
    const [offset, setOffset] = useState(0);
    const [historyOffset, setHistoryOffset] = useState(0);
    const [deleteOpen, setDeleteOpen] = useState(false);
    const client = useQueryClient();
    const list = useQuery({ queryKey: ['schedules', offset], queryFn: () => api.get<SchedulePage<Schedule>>(`/schedules?limit=20&offset=${offset}`), enabled: !selected && !form, refetchInterval: 30000 });
    const detail = useQuery({ queryKey: ['schedule', selected], queryFn: () => api.get<Schedule>(`/schedules/${selected}`), enabled: !!selected, refetchInterval: form ? false : 30000 });
    const history = useQuery({ queryKey: ['schedule-occurrences', selected, historyOffset], queryFn: () => api.get<SchedulePage<Occurrence>>(`/schedules/${selected}/occurrences?limit=20&offset=${historyOffset}`), enabled: !!selected && !form, refetchInterval: 30000 });
    async function refresh() { await Promise.all([client.invalidateQueries({ queryKey: ['schedules'] }), client.invalidateQueries({ queryKey: ['schedule', selected] })]); }
    const action = useMutation({ mutationFn: ({ kind, schedule }: {
            kind: 'pause' | 'resume' | 'delete';
            schedule: Schedule;
        }) => kind === 'delete' ? api.delete(`/schedules/${schedule.id}?version=${schedule.version}`) : api.post(`/schedules/${schedule.id}/${kind}`, { version: schedule.version }), onSuccess: async (_, variables) => { if (variables.kind === 'delete') {
            setSelected(null);
            setDeleteOpen(false);
            setOffset(0);
        } await refresh(); } });
    function back() { setSelected(null); setForm(false); setHistoryOffset(0); action.reset(); }
    function open(id: string) { setSelected(id); setHistoryOffset(0); action.reset(); }
    function saved(schedule: Schedule) { client.setQueryData(['schedule', schedule.id], schedule); setSelected(schedule.id); setForm(false); setHistoryOffset(0); void client.invalidateQueries({ queryKey: ['schedules'] }); }
    const schedule = detail.data;
    return <div className="h-full overflow-y-auto bg-[var(--surface)]"><div className="mx-auto max-w-[960px] px-5 pb-28 pt-8 sm:px-8 lg:pb-12 lg:pt-11">
 {(selected || form) && <button type="button" onClick={back} className="mb-5 text-sm text-[var(--ink-soft)] hover:underline">← 返回定时任务</button>}
 <header className="flex flex-wrap items-start justify-between gap-4"><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-3"><h1 className="break-words text-[30px] font-semibold tracking-[-.04em]">{form ? selected ? '编辑任务' : '新建定时任务' : selected ? schedule?.name ?? '定时任务' : '定时任务'}</h1>{!form && selected && schedule && <span className="inline-flex items-center gap-2 rounded-full bg-[var(--surface-subtle)] px-3 py-1 text-xs text-[var(--ink-soft)]"><span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${schedule.status === 'active' ? 'bg-[var(--accent)]' : 'bg-[var(--ink-faint)]'}`}/>{scheduleStatuses[schedule.status]}</span>}</div>{!selected && !form && <p className="mt-2 text-sm text-[var(--ink-soft)]">安排周期工作，查看每次执行的结果。</p>}</div>{!selected && !form && <button className="shrink-0 rounded-xl bg-[var(--ink)] px-4 py-2.5 text-sm text-[var(--surface)]" onClick={() => setForm(true)}>新建任务</button>}{!form && selected && schedule && <div className="flex w-full shrink-0 items-center gap-2 sm:w-auto"><button className={button} disabled={action.isPending} onClick={() => { action.reset(); setForm(true); }}>编辑</button>{schedule.status !== 'completed' && <button className={button} disabled={action.isPending} onClick={() => action.mutate({ kind: schedule.status === 'active' ? 'pause' : 'resume', schedule })}>{schedule.status === 'active' ? '暂停' : '开启'}</button>}<details className="relative" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false; }} onKeyDown={event => { if (event.key === 'Escape') { event.currentTarget.open = false; event.currentTarget.querySelector('summary')?.focus(); } }}><summary className={`${button} cursor-pointer list-none text-[var(--ink-soft)] [&::-webkit-details-marker]:hidden`}>更多</summary><div className="absolute right-0 top-full z-10 mt-2 min-w-32 rounded-xl border border-[var(--line)] bg-[var(--surface-raised)] p-1 shadow-sm"><button className="w-full rounded-lg px-3 py-2 text-left text-sm text-[var(--danger)] hover:bg-[var(--surface-subtle)] disabled:opacity-40" disabled={action.isPending} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); setDeleteOpen(true); }}>删除</button></div></details></div>}</header>
 {(selected ? detail.isLoading : !form && list.isLoading) && <p role="status" className="mt-8">正在加载…</p>}
 {(selected ? detail.isError : !form && list.isError) && <p role="alert" className="mt-6 text-sm text-[var(--danger)]">计划加载失败。<button className="ml-2 underline" onClick={() => selected ? detail.refetch() : list.refetch()}>重试</button></p>}
 {form && (!selected || schedule) && <ScheduleForm key={`${selected ?? 'new'}-${schedule?.version ?? 0}`} schedule={selected ? schedule : undefined} onSaved={saved} onCancel={() => setForm(false)} onReload={async () => { await detail.refetch(); setForm(false); }}/>}
 {!form && !selected && list.data && <><div className="mt-7 hidden grid-cols-[2fr_1fr_1fr_5rem] gap-4 border-b border-[var(--line)] px-3 pb-3 text-xs text-[var(--ink-soft)] sm:grid"><span>任务名称</span><span>执行时间</span><span>下次执行</span><span>状态</span></div><ul className="mt-3 divide-y divide-[var(--line)]">{list.data.items.map(item => <li key={item.id}><button className="grid w-full gap-3 rounded-xl px-3 py-5 text-left hover:bg-[var(--surface-subtle)] focus-visible:outline-2 focus-visible:outline-[var(--accent)] sm:grid-cols-[2fr_1fr_1fr_5rem] sm:gap-4" onClick={() => open(item.id)}><span className="min-w-0"><span className="block break-words text-sm font-medium">{item.name}</span><span className="mt-1 block break-words text-xs text-[var(--ink-soft)]">{item.pause_reason ?? (item.latest_occurrence ? `${occurrenceLabel(item.latest_occurrence)}${item.latest_occurrence.reason ? ` · ${item.latest_occurrence.reason}` : ''}` : '尚未执行')}</span></span><span className="text-xs leading-5">{ruleLabel(item.rule, item.timezone)}<span className="block text-[var(--ink-faint)]">{item.timezone}</span></span><span className="text-xs leading-5"><span className="sm:hidden">下次执行：</span>{formatScheduleTime(item.next_fire_at, item.timezone)}</span><span className="text-xs leading-5">{scheduleStatuses[item.status]}</span></button></li>)}</ul>{!list.data.items.length && <p className="py-16 text-center text-sm text-[var(--ink-soft)]">还没有定时任务，创建一个计划开始吧。</p>}<Pagination offset={offset} total={list.data.total} onChange={setOffset} label="计划"/></>}
 {!form && selected && schedule && <>
 <section aria-label="计划信息" className="mt-8">
 <dl className="grid gap-5 text-sm sm:grid-cols-3"><div className="min-w-0"><dt className="text-xs text-[var(--ink-faint)]">执行 Agent</dt><dd className="mt-2 break-words font-medium">{schedule.agent_name}</dd></div><div className="min-w-0"><dt className="text-xs text-[var(--ink-faint)]">执行时间</dt><dd className="mt-2 break-words leading-6">{ruleLabel(schedule.rule, schedule.timezone)}<span className="block text-xs text-[var(--ink-soft)]">{schedule.timezone}</span></dd></div><div className="min-w-0"><dt className="text-xs text-[var(--ink-faint)]">下次执行</dt><dd className="mt-2 break-words font-medium leading-6">{formatScheduleTime(schedule.next_fire_at, schedule.timezone)}</dd></div></dl>
 {schedule.pause_reason && <p role="status" className="mt-4 text-sm text-[var(--danger)]">{schedule.pause_reason}</p>}
 <p className="mt-5 text-xs leading-5 text-[var(--ink-faint)]">暂停仅影响后续触发；正在执行的任务可在会话中停止。</p>
 </section>
 {action.isError && !deleteOpen && <p role="alert" className="mt-4 text-sm text-[var(--danger)]">{scheduleError(action.error)} <button className="underline" onClick={() => { action.reset(); void detail.refetch(); }}>重新加载计划</button></p>}
 <section className="mt-8 border-t border-[var(--line)] pt-8"><h2 className="text-sm font-semibold">任务目标</h2><p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7 text-[var(--ink-soft)]">{schedule.prompt}</p></section>
 <section className="mt-8 border-t border-[var(--line)] pt-8"><div className="flex items-center justify-between gap-3"><h2 className="text-sm font-semibold">执行记录</h2>{history.data && <span className="text-xs text-[var(--ink-faint)]">共 {history.data.total} 条</span>}</div>{history.isLoading && <p role="status" className="mt-4 text-sm">正在加载执行记录…</p>}{history.isError && <p role="alert" className="mt-4 text-sm text-[var(--danger)]">执行记录加载失败。<button className="ml-2 underline" onClick={() => history.refetch()}>重试</button></p>}<ul className="mt-3 divide-y divide-[var(--line)]">{history.data?.items.map(item => <li key={item.id} className="py-4 text-sm"><div className="flex flex-wrap items-center justify-between gap-3"><time dateTime={item.scheduled_for}>{formatScheduleTime(item.scheduled_for, schedule.timezone)}</time><span>{occurrenceLabel(item)}{item.missed_count > 1 ? `（${item.missed_count} 次）` : ''}</span>{item.task_id && item.conversation_id && <Link href={`/workspace/c/${item.conversation_id}`} className="text-[var(--accent)] hover:underline">查看结果 →</Link>}</div>{item.reason && <details className="mt-2 text-xs leading-6 text-[var(--ink-soft)]"><summary className="cursor-pointer">查看原因</summary><p className="break-words">{item.reason}</p>{item.outcome === 'blocked' && <p>请检查账号、Agent 和模型的可用性，修复后重新开启计划。</p>}</details>}<OccurrenceNotification scheduleId={schedule.id} occurrenceId={item.id} notification={item.notification}/></li>)}</ul>{history.data && !history.data.items.length && <p className="py-10 text-center text-sm text-[var(--ink-faint)]">尚无执行记录。</p>}<Pagination offset={historyOffset} total={history.data?.total ?? 0} onChange={setHistoryOffset} label="执行记录"/></section>
 <Dialog open={deleteOpen} onClose={() => { if (!action.isPending)
            setDeleteOpen(false); }} title="删除定时任务？" description="删除后不再触发；已有会话、任务和成果会保留。" footer={<><button className={button} disabled={action.isPending} onClick={() => setDeleteOpen(false)}>取消</button><button className={`${button} text-[var(--danger)]`} disabled={action.isPending} onClick={() => action.mutate({ kind: 'delete', schedule })}>确认删除</button></>}>{action.isError ? <p role="alert" className="text-sm text-[var(--danger)]">{scheduleError(action.error)}</p> : <p className="break-words text-sm">{schedule.name}</p>}</Dialog>
 </>}
 </div></div>;
}
