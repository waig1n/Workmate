import { Icon } from "@/components/ui/Icons";

export type ChatActivity = { taskId?: string; runId?: string | null; label: string; animated?: boolean };

export function TaskProgress({ activity, standalone = false }: { activity: ChatActivity; standalone?: boolean }) {
  return <div role="status" aria-label="智能体执行状态" className={`flex items-center gap-3 text-sm text-[var(--ink-soft)] ${standalone ? "py-2" : "ml-[52px] py-1"}`}>
    {standalone && <span className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl bg-[var(--accent-soft)]"><Icon name="spark" className="h-5 w-5 text-[var(--accent)]" /></span>}
    <span>{activity.label}</span>
    {activity.animated !== false && <span className="run-thinking-dots" aria-hidden><i /><i /><i /></span>}
  </div>;
}
