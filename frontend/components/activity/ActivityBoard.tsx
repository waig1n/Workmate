"use client";

import { useState } from "react";

import { RunToolDetails } from "@/components/activity/RunToolDetails";
import { TaskTimeline } from "@/components/activity/TaskTimeline";
import { TaskKnowledgeSources } from "@/components/TaskKnowledgeSources";
import { Icon } from "@/components/ui/Icons";
import type { ChatToolMsg } from "@/lib/chat/model";
import { taskDisplayStatus } from "@/lib/taskExecutionPhase";
import type { Task } from "@/lib/types";

const STATUS_LABELS: Record<Task["status"] | "preparing", string> = {
  created: "工作中",
  preparing: "工作中",
  running: "工作中",
  awaiting_permission: "等待审批",
  cancelling: "正在停止",
  completed: "已完成",
  failed: "失败",
  cancelled: "已停止",
  interrupted: "已中断",
};

export function ActivityLauncher({
  task,
  onOpen,
}: {
  task: Task | null;
  onOpen: () => void;
}) {
  const running = Boolean(task && ["created", "running", "awaiting_permission", "cancelling"].includes(task.status));
  return (
    <button
      type="button"
      aria-label="执行活动"
      title="展开执行动态"
      onClick={onOpen}
      className="absolute right-3 top-[80px] z-20 grid h-11 w-11 place-items-center rounded-full border border-[var(--line-strong)] bg-[color-mix(in_srgb,var(--surface)_94%,transparent)] text-[var(--ink-soft)] shadow-[var(--shadow-md)] backdrop-blur-xl transition-transform hover:-translate-y-0.5 hover:border-[var(--accent)] hover:text-[var(--accent)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] md:right-4 md:top-[84px]"
    >
      <Icon name="list" className="h-4 w-4" />
      {running && <span className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full border-2 border-[var(--surface)] bg-[var(--positive)]" />}
    </button>
  );
}

export function ActivityBoard({
  tasks,
  task,
  toolItems,
  onSelectTask,
  onClose,
  showAuditDetails = false,
}: {
  tasks: Task[];
  task: Task | null;
  toolItems: ChatToolMsg[];
  onSelectTask: (taskId: string) => void;
  onClose: () => void;
  showAuditDetails?: boolean;
}) {
  const latestTask = tasks[0] ?? null;
  const [auditOpen, setAuditOpen] = useState(false);
  const selectedTaskIndex = task ? tasks.findIndex((candidate) => candidate.id === task.id) : -1;

  return (
    <>
      <button type="button" aria-label="关闭执行动态" className="popup-backdrop-enter fixed inset-0 z-40 bg-[#15130f]/20 backdrop-blur-[1px] md:hidden" onClick={onClose} />
      <section role="region" aria-label="执行动态看板" className="popup-panel-enter fixed inset-x-3 bottom-[92px] top-[76px] z-50 flex flex-col overflow-hidden rounded-[20px] border border-[var(--line-strong)] bg-[color-mix(in_srgb,var(--surface)_96%,transparent)] shadow-[var(--shadow-md)] backdrop-blur-xl md:absolute md:inset-auto md:right-4 md:top-[82px] md:z-30 md:h-[min(620px,calc(100%-98px))] md:w-[360px]">
        <header className="flex items-center gap-3 border-b border-[var(--line)] px-3.5 py-3">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[11px] bg-[var(--accent-soft)] text-[var(--accent)]"><Icon name="list" className="h-4 w-4" /></span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2"><h2 className="text-[11px] font-semibold">执行动态</h2>{task && <span className="rounded-full bg-[var(--surface-subtle)] px-2 py-0.5 text-[9px] text-[var(--ink-faint)]">{STATUS_LABELS[taskDisplayStatus(task)]}</span>}</div>
            <p className="mt-0.5 truncate text-[9px] text-[var(--ink-faint)]">{task?.title || "发送任务后查看执行过程"}</p>
          </div>
          <button type="button" aria-label="关闭执行动态" onClick={onClose} className="grid h-8 w-8 place-items-center rounded-lg text-[var(--ink-faint)] hover:bg-[var(--surface-subtle)] hover:text-[var(--ink)]"><Icon name="x" className="h-3.5 w-3.5" /></button>
        </header>

        {tasks.length > 0 && <div className="flex items-center gap-2 border-b border-[var(--line)] bg-[var(--surface-subtle)] px-3 py-2">
          <select aria-label="选择执行记录" value={task?.id ?? ""} onChange={(event) => onSelectTask(event.target.value)} className="h-8 min-w-0 flex-1 rounded-lg border border-[var(--line)] bg-[var(--surface-raised)] px-2 text-[10px] outline-none">
            {tasks.map((candidate, index) => <option key={candidate.id} value={candidate.id}>第 {tasks.length - index}/{tasks.length} 次 · {candidate.title || "未命名任务"}</option>)}
          </select>
          {task && latestTask && task.id !== latestTask.id && <button type="button" onClick={() => onSelectTask(latestTask.id)} className="shrink-0 rounded-lg px-2 py-1.5 text-[9px] font-medium text-[var(--accent)] hover:bg-[var(--accent-soft)]">回到最新</button>}
          {selectedTaskIndex >= 0 && <span className="shrink-0 text-[9px] text-[var(--ink-faint)]">{tasks.length - selectedTaskIndex}/{tasks.length}</span>}
        </div>}

        <div className="min-h-0 flex-1 overflow-y-auto">
          {task && <details key={task.id} className="mx-3 mt-3 rounded-xl border border-[var(--line)] bg-[var(--surface-subtle)] p-3 text-xs">
            <summary className="cursor-pointer font-medium text-[var(--ink-soft)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]">用量与执行信息</summary>
            <dl className="mt-3 space-y-2 text-[var(--ink-soft)]">
              {[
                ["模型", task.model_name || "未记录"],
                ["输入 Token", task.token_usage?.input_tokens?.toLocaleString("zh-CN") ?? "未记录"],
                ["输出 Token", task.token_usage?.output_tokens?.toLocaleString("zh-CN") ?? "未记录"],
                ["执行时长", task.duration_ms != null ? `${(task.duration_ms / 1000).toFixed(1)} 秒` : "未记录"],
              ].map(([label, value]) => <div key={label} className="flex items-start justify-between gap-4"><dt className="shrink-0 text-[var(--ink-faint)]">{label}</dt><dd className="min-w-0 break-words text-right">{value}</dd></div>)}
            </dl>
            {task.token_usage?.complete === false && <p className="mt-3 text-xs text-[var(--ink-soft)]">用量记录不完整，以上仅为已知部分。</p>}
            <p className="mt-3 text-[11px] leading-5 text-[var(--ink-faint)]">仅显示所选执行记录；用量通常在执行结束后记录，未记录不代表没有消耗。</p>
          </details>}
          {showAuditDetails && <label className="flex items-center gap-2 px-4 pt-3 text-[10px] text-[var(--ink-faint)]"><input type="checkbox" checked={auditOpen} onChange={(event) => setAuditOpen(event.target.checked)} />显示审计记录</label>}
          <RunToolDetails tools={toolItems} />
          {task && <div className="px-4 py-3"><TaskKnowledgeSources key={task.id} taskId={task.id} complete={["completed", "failed", "cancelled", "interrupted"].includes(task.status)} live={[]} /></div>}
          <TaskTimeline task={task} showAuditDetails={showAuditDetails && auditOpen} />
        </div>
      </section>
    </>
  );
}
