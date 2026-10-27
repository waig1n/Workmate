"use client";

import { useState } from "react";

import { ToolCallDetail } from "@/components/chat/ToolCallDetail";
import { Icon } from "@/components/ui/Icons";
import type { ActivityStep, ChatTextMsg, ChatToolMsg, RunEntry } from "@/lib/chat/model";
import { classifyTool, failureSummary, failedTools, summarizeTools } from "@/lib/chat/toolCategories";
import { taskDisplayStatus } from "@/lib/taskExecutionPhase";
import type { Task, TaskStatus } from "@/lib/types";

type Tone = {
  marker: string;
  label: string;
  toneClass: string;
  pulse?: boolean;
};

const RUN_STATUS: Record<string, Tone> = {
  queued: { marker: "●", label: "工作中", toneClass: "text-neutral-400" },
  starting: { marker: "●", label: "工作中", toneClass: "text-blue-600", pulse: true },
  running: { marker: "●", label: "工作中", toneClass: "text-blue-600", pulse: true },
  reconnecting: { marker: "●", label: "连接中断，正在恢复", toneClass: "text-amber-600", pulse: true },
  RUN_FINISHED: { marker: "✓", label: "已完成", toneClass: "text-emerald-600" },
  RUN_CANCELLED: { marker: "⏸", label: "已中断", toneClass: "text-neutral-400" },
  RUN_ERROR: { marker: "✕", label: "执行失败", toneClass: "text-red-500" },
};

const TASK_STATUS: Record<string, Tone> = {
  preparing: { marker: "●", label: "工作中", toneClass: "text-blue-600", pulse: true },
  created: { marker: "●", label: "工作中", toneClass: "text-neutral-400" },
  running: { marker: "●", label: "工作中", toneClass: "text-blue-600", pulse: true },
  awaiting_permission: { marker: "●", label: "等待你的审批", toneClass: "text-amber-600", pulse: true },
  cancelling: { marker: "●", label: "正在停止", toneClass: "text-amber-600", pulse: true },
  completed: { marker: "✓", label: "已完成", toneClass: "text-emerald-600" },
  cancelled: { marker: "⏸", label: "已停止", toneClass: "text-neutral-400" },
  interrupted: { marker: "⏸", label: "已中断", toneClass: "text-amber-600" },
  failed: { marker: "✕", label: "执行失败", toneClass: "text-red-500" },
};

function toolActionLabel(item: ChatToolMsg) {
  const labels = {
    read: "读取文件",
    command: "执行命令",
    write: "修改文件",
    skill: "调用 Skill",
    mcp: "调用连接器",
    other: `调用 ${item.call.toolCallName || "工具"}`,
  };
  return labels[classifyTool(item.call.toolCallName)];
}

/**
 * One aggregated status line per run, replacing the old stream of
 * per-tool cards. Clicking it opens the Activity side panel where the
 * full Read/Bash/Skill/MCP call details live. Intermediate failed calls
 * remain in Activity; the main stream surfaces them only when the Task
 * itself ultimately fails.
 */
export function RunSummary({
  run,
  tools,
  progress = [],
  process = [],
  artifactCount = 0,
  taskStatus = null,
  executionPhase = null,
  onOpenActivity,
  onOpenArtifacts,
}: {
  run: RunEntry | null;
  tools: ChatToolMsg[];
  steps?: ActivityStep[];
  progress?: ChatTextMsg[];
  process?: Array<ChatTextMsg | ChatToolMsg>;
  artifactCount?: number;
  /** Live task status, authoritative for the latest run. */
  taskStatus?: TaskStatus | null;
  executionPhase?: Task["execution_phase"];
  onOpenActivity: () => void;
  onOpenArtifacts?: () => void;
}) {
  const [openFailure, setOpenFailure] = useState<string | null>(null);
  const [processExpanded, setProcessExpanded] = useState(false);
  const terminal = taskStatus
    ? ["completed", "failed", "cancelled", "interrupted"].includes(taskStatus)
    : Boolean(run && ["RUN_FINISHED", "RUN_CANCELLED", "RUN_ERROR"].includes(run.status));

  const groupFinished = tools.length > 0 && tools.every((item) => ["success", "error"].includes(item.call.state));
  const terminalTone = terminal
    ? (taskStatus && TASK_STATUS[taskDisplayStatus({ status: taskStatus, execution_phase: executionPhase })]) || (run && RUN_STATUS[run.status])
    : null;
  const tone = terminalTone || (taskStatus && TASK_STATUS[taskDisplayStatus({ status: taskStatus, execution_phase: executionPhase })]) || (groupFinished
    ? { marker: "✓", label: "步骤已结束", toneClass: "text-neutral-400", pulse: false }
    : (taskStatus && TASK_STATUS[taskDisplayStatus({ status: taskStatus, execution_phase: executionPhase })])
      || (run && RUN_STATUS[run.status])
      || { marker: "·", label: "执行记录", toneClass: "text-neutral-400" });

  const label = tone.label;
  // Snapshots provide a cumulative prefix, not sentence-to-tool chronology.
  const processNotes = progress.length ? progress : process.filter((item): item is ChatTextMsg => item.kind === "text");
  const processTools = tools.length ? tools : process.filter((item): item is ChatToolMsg => item.kind === "tool");

  const counts = summarizeTools(tools);
  const failures = failedTools(tools);
  const activeTool = tools.findLast(item => item.call.state === "running");
  const activeDetail = tone.pulse && activeTool ? toolActionLabel(activeTool) : null;
  // The Product Task snapshot is authoritative when available. A failed
  // Bash attempt can be a normal part of an Agent retry and should not leave
  // a red warning behind once the overall Task completed successfully.
  const taskFailed = taskStatus
    ? taskStatus === "failed"
    : run?.status === "RUN_ERROR";

  if (!tools.length && !process.length && !progress.length && (taskStatus ? taskStatus === "completed" : run?.status === "RUN_FINISHED")) return null;

  return (
    <section aria-live="polite" aria-busy={tone.pulse || undefined} className="ml-[52px] space-y-1">
      <div className="flex w-full items-center gap-2 border-l-2 border-[var(--line-strong)] py-1.5 pl-3 text-xs leading-5 text-[var(--ink-faint)]">
        <button
          type="button"
          aria-expanded={processExpanded}
          className="group flex min-w-0 items-center gap-2 rounded-md py-1 text-left hover:text-[var(--ink)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
          onClick={() => setProcessExpanded((value) => !value)}
        >
          {tone.pulse ? (
            <span className={`relative flex h-3 w-3 shrink-0 items-center justify-center ${tone.toneClass}`} aria-hidden><span className="absolute h-3 w-3 animate-ping rounded-full bg-current opacity-20" /><span className="relative h-1.5 w-1.5 rounded-full bg-current" /></span>
          ) : <span className={`shrink-0 text-[10px] ${tone.toneClass}`} aria-hidden>{tone.marker}</span>}
          <span className="shrink-0">执行过程</span>
          <span className={`shrink-0 text-[11px] ${tone.toneClass}`}>{label}</span>
          {activeDetail
            ? <span className="truncate">· {activeDetail}</span>
            : counts.length > 0 && <span className="truncate">· {counts.join(" · ")}</span>}
          {activeDetail && tools.length > 0 && <span className="shrink-0 text-[10px]">· {tools.length} 次工具调用</span>}
          {tone.pulse && <span role="status" className="run-thinking-dots shrink-0" aria-label="仍在运行"><i /><i /><i /></span>}
          <Icon name="chevron" className={`h-3 w-3 shrink-0 text-[var(--ink-faint)] transition-transform ${processExpanded ? "rotate-90" : ""}`} />
        </button>
        {artifactCount > 0 && onOpenArtifacts && (
          <button
            type="button"
            className="inline-flex shrink-0 items-center gap-1 rounded-lg px-1.5 py-0.5 hover:bg-[var(--surface-raised)] hover:text-[var(--ink)]"
            onClick={onOpenArtifacts}
          >
            <Icon name="archive" className="h-3 w-3" />
            生成 {artifactCount} 个成果
          </button>
        )}
      </div>
      {processExpanded && (
        <div className="run-process-enter ml-3 max-h-72 space-y-4 overflow-y-auto border-l border-[var(--line-strong)] py-2 pl-4 pr-2">
          {processNotes.length > 0 && (
            <section aria-label="过程说明">
              <h4 className="mb-2 text-[10px] font-medium text-[var(--ink-faint)]">过程说明</h4>
              <div className="space-y-2 text-[11px] leading-5 text-[var(--ink-soft)]">
                {processNotes.map(item => <p key={item.id} className="whitespace-pre-wrap break-words">{item.text}</p>)}
              </div>
            </section>
          )}
          {processTools.length > 0 && (
            <section aria-label="工具调用">
              <h4 className="mb-2 text-[10px] font-medium text-[var(--ink-faint)]">工具调用</h4>
              <ul className="space-y-2">
                {processTools.map(item => (
                  <li key={item.id} className="flex items-baseline gap-2 text-[11px] leading-5 text-[var(--ink-soft)]">
                    <span className="font-medium text-[var(--ink)]">{toolActionLabel(item)}</span>
                    <span className="truncate text-[10px] text-[var(--ink-faint)]">{item.call.toolCallName}</span>
                    <span className="ml-auto shrink-0 text-[9px] text-[var(--ink-faint)]">{item.call.state === "running" ? terminal ? "任务已结束，结束记录缺失" : "进行中" : item.call.state === "error" ? "失败" : item.call.state === "unknown" ? "结果未知" : item.call.state === "interrupted" ? "已中断" : "完成"}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {!processNotes.length && !processTools.length && (
            <p className="text-[11px] leading-5 text-[var(--ink-faint)]">{terminal ? "本轮执行已结束" : tone.pulse ? label : "暂无工具记录"}</p>
          )}
          <button type="button" onClick={onOpenActivity} className="mt-3 inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-[10px] font-medium text-[var(--accent)] hover:bg-[var(--accent-soft)]"><Icon name="list" className="h-3 w-3" />打开完整执行动态</button>
        </div>
      )}
      {taskFailed && failures.length > 0 && (
        <div className="ml-3 space-y-0.5 border-l border-red-200 pl-3 dark:border-red-900/60">
          {failures.map((item) => (
            <div key={item.id}>
              <button
                type="button"
                className="flex w-full items-baseline gap-2 py-0.5 text-left text-[11px] text-red-500 hover:text-red-600"
                onClick={() => setOpenFailure(openFailure === item.id ? null : item.id)}
              >
                <span aria-hidden>✕</span>
                <span className="shrink-0 font-medium">{item.call.toolCallName || "工具调用"}</span>
                <span className="min-w-0 truncate text-[10px] text-neutral-400" title={failureSummary(item)}>
                  {failureSummary(item)}
                </span>
              </button>
              {openFailure === item.id && <ToolCallDetail item={item} />}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
