"use client";

import { useQuery } from "@tanstack/react-query";

import { Icon } from "@/components/ui/Icons";
import { api } from "@/lib/api";
import { deliveryWarnings, visibleActivityEvents } from "@/lib/activityEvents";
import type { Task, TaskEvent } from "@/lib/types";

const LABELS: Record<string, string> = {
  task_started: "任务已创建",
  run_started: "开始执行",
  run_resumed: "审批后继续执行",
  tool_call: "调用工具",
  tool_result: "工具返回结果",
  hitl_request: "等待审批",
  hitl_decision: "审批已提交",
  hitl_resume_succeeded: "审批恢复成功",
  hitl_resume_failed: "审批恢复失败",
  permission_mode_changed: "任务权限模式已变更",
  run_finished: "本轮回复结束",
  run_metrics: "执行指标",
  artifact: "生成成果",
  artifact_delivery: "成果交付检查",
  stream_error: "事件流异常",
  task_cancel_requested: "用户请求停止",
  runtime_interrupt_requested: "已通知 Runtime 停止",
  runtime_interrupt_retried: "重试停止请求",
  run_terminal_synthesized: "恢复终态",
  task_cancelled: "任务已停止",
};

function eventSummary(event: TaskEvent): string | null {
  if (event.event_type === "artifact_delivery") return deliveryWarnings(event).join("；") || null;
  const payload = event.payload;
  for (const key of ["tool", "tool_call_name", "toolCallName", "name", "reason", "message"]) {
    const value = payload[key];
    if (typeof value === "string" && value) return value;
  }
  return null;
}

export function TaskTimeline({
  task,
  showAuditDetails = false,
}: {
  task: Task | null;
  showAuditDetails?: boolean;
}) {
  const events = useQuery({
    queryKey: ["task-events", task?.id],
    queryFn: () => api.get<TaskEvent[]>(`/tasks/${task!.id}/events`),
    enabled: Boolean(task),
    refetchInterval: task && ["created", "running", "awaiting_permission", "cancelling"].includes(task.status) ? 3000 : false,
  });

  if (!task) return <div className="grid flex-1 place-items-center p-8 text-center text-[11px] text-neutral-400"><div><Icon name="list" className="mx-auto mb-3 h-6 w-6" />发送任务后可查看执行时间线</div></div>;
  if (events.isLoading) return <div className="space-y-3 p-4">{[1, 2, 3].map((item) => <div key={item} className="h-12 animate-pulse rounded-xl bg-neutral-100 dark:bg-neutral-800" />)}</div>;
  if (events.isError) return <div className="m-4 rounded-xl bg-red-50 p-3 text-[11px] text-red-600 dark:bg-red-950/40 dark:text-red-300">时间线加载失败。<button type="button" className="ml-2 underline" onClick={() => events.refetch()}>重试</button></div>;
  const visibleEvents = visibleActivityEvents(events.data ?? [], showAuditDetails);
  if (!visibleEvents.length) return <div className="p-8 text-center text-[11px] text-neutral-400">暂无活动记录</div>;

  return (
    <ol className="space-y-0 overflow-y-auto px-4 py-3">
      {visibleEvents.map((event, index) => {
        const summary = eventSummary(event);
        return <li key={event.id} className="relative flex gap-3 pb-4"><div className="flex w-4 flex-col items-center"><span className="mt-1.5 h-2 w-2 rounded-full bg-blue-500" />{index < visibleEvents.length - 1 && <span className="mt-1 w-px flex-1 bg-neutral-200 dark:bg-neutral-700" />}</div><div className="min-w-0 flex-1"><div className="flex items-center justify-between gap-2"><p className="text-[11px] font-medium">{LABELS[event.event_type] ?? event.event_type}</p><time className="shrink-0 text-[9px] text-neutral-400">{new Date(event.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time></div>{summary && <p className="mt-1 truncate text-[9px] text-neutral-500" title={summary}>{summary}</p>}{showAuditDetails && <details className="mt-1 text-[9px] text-neutral-400"><summary className="cursor-pointer">审计事件 #{event.seq}</summary><pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-neutral-50 p-2 dark:bg-neutral-900">{JSON.stringify(event.payload, null, 2)}</pre></details>}</div></li>;
      })}
    </ol>
  );
}
