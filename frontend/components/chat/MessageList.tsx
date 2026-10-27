"use client";

import { TaskProgress, type ChatActivity } from "./TaskProgress";
import { TaskKnowledgeSources } from "@/components/TaskKnowledgeSources";
import type { TaskKnowledgeSource, KnowledgeProgress } from "@/lib/knowledge";
import { AssistantMessage } from "@/components/chat/AssistantMessage";
import { ArtifactReferences } from "@/components/chat/ArtifactReferences";
import { RunSummary } from "@/components/chat/RunSummary";
import { ImageSearchResults } from "@/components/chat/ImageSearchResults";
import { Icon } from "@/components/ui/Icons";
import { groupChatItems } from "@/lib/chat/grouping";
import type { ActivityStep, ChatItem, RunEntry } from "@/lib/chat/model";
import type { Artifact, Task } from "@/lib/types";

/**
 * Main conversation stream: user messages, assistant answers, one
 * aggregated status line per run, and failure rows. Plain tool-call
 * cards are deliberately absent - their details live in the Activity
 * side panel (progressive disclosure).
 */
export function MessageList({
  activity,
  approval,
  knowledgeSources = [],
  knowledgeProgress = {},
  items,
  runs,
  historyLoaded,
  historyIncomplete = false,
  agentName,
  steps,
  artifacts,
  tasks,
  onPrompt,
  onOpenActivity,
  onOpenArtifact,
}: {
  activity?: ChatActivity | null;
  approval?: React.ReactNode;
  knowledgeSources?: TaskKnowledgeSource[];
  knowledgeProgress?: Record<string, KnowledgeProgress>;
  items: ChatItem[];
  runs: RunEntry[];
  historyLoaded: boolean;
  historyIncomplete?: boolean;
  agentName: string;
  steps: ActivityStep[];
  artifacts: Artifact[];
  tasks: Task[];
  onPrompt: (value: string) => void;
  onOpenActivity: (runId: string | null) => void;
  onOpenArtifact: (artifactId: string) => void;
}) {
  if (historyLoaded && items.length === 0 && !activity && !historyIncomplete) {
    return (
      <div className="flex min-h-[54vh] flex-col items-center justify-center text-center">
        <div className="mb-6 grid h-14 w-14 place-items-center rounded-[19px] bg-[var(--ink)] text-[var(--surface)] shadow-[var(--shadow-md)]"><Icon name="spark" className="h-6 w-6" /></div>
        <p className="product-eyebrow">Ready to work</p>
        <h2 className="mt-2 text-[28px] font-semibold tracking-[-.04em]">交给我一项工作</h2>
        <p className="mt-3 max-w-lg text-[14px] leading-6 text-[var(--ink-soft)]">{agentName || "智能体"} 已就绪。它会在授权边界内使用 Skills 与连接器，关键操作由你审批。</p>
        <div className="mt-7 grid w-full max-w-2xl gap-3 sm:grid-cols-3">{["整理资料并总结", "分析数据与风险", "生成可交付成果"].map((text, index) => <button key={text} type="button" onClick={() => onPrompt(text)} className="product-card product-card-interactive rounded-[16px] px-4 py-4 text-left text-[12px] font-medium text-[var(--ink-soft)]"><span className="mb-3 block text-[10px] font-bold text-[var(--accent)]">0{index + 1}</span>{text}</button>)}</div>
      </div>
    );
  }

  const nodes = groupChatItems(items, runs, tasks);
  // Attach results to the last visible answer in the same work segment,
  // never to the execution controls or across a user/Reply boundary.
  const answerImages = new Map<number, string[]>();
  let answerIndex: number | null = null;
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    const node = nodes[index];
    if (node.type === "summary") {
      if (answerIndex !== null) {
        const answer = nodes[answerIndex];
        if (answer.type === "item" && answer.item.kind === "text") {
          const message = answer.item;
          const task = tasks.find((candidate) => candidate.id === message.taskId);
          const replyId = message.runId ?? task?.run_id;
          if (!replyId || replyId === (node.run?.id ?? node.key)) {
            answerImages.set(answerIndex, [...node.tools.filter((tool) => tool.call.toolCallName === "web_search")
              .map((tool) => tool.call.result), ...(answerImages.get(answerIndex) ?? [])]);
          } else answerIndex = null;
        }
      }
    } else if (node.item.kind === "text") {
      if (node.item.role === "user") answerIndex = null;
      else if (node.item.role === "assistant" && node.item.text.trim() && answerIndex === null) answerIndex = index;
    }
  }
  const taskFor = (item: { taskId?: string; runId?: string | null }) => tasks.find((task) => task.id === item.taskId || (item.runId != null && task.run_id === item.runId));
  const answerKey = (item: { taskId?: string; runId?: string | null; id: string }) => taskFor(item)?.id ?? item.taskId ?? item.runId ?? item.id;
  const shownKnowledge = new Set<string>();
  const lastAnswer = new Map<string, string>();
  for (const item of items) {
    if (item.kind === "text" && item.role === "assistant") {
      lastAnswer.set(answerKey(item), item.id);
    }
  }
  const activityIndex = activity ? nodes.findLastIndex((node) => {
    if (node.type === "summary") return (activity.runId != null && (node.run?.id ?? node.key) === activity.runId) || (activity.taskId != null && node.run?.taskId === activity.taskId);
    return node.item.kind === "text" && ((activity.taskId != null && taskFor(node.item)?.id === activity.taskId) || (activity.runId != null && node.item.runId === activity.runId));
  }) : -1;
  const progressAt = (index: number) => activity && index === activityIndex ? approval ? <div className="ml-[52px]">{approval}</div> : <TaskProgress activity={activity} standalone={nodes[index].type === "item" && nodes[index].item.kind === "text" && nodes[index].item.role === "user"} /> : null;
  return (
    <div className="space-y-6">
      {historyIncomplete && !items.some(item => item.kind === "text" && item.incomplete) && <p role="status" className="text-sm text-[var(--ink-soft)]">部分回复内容不完整</p>}
      {nodes.map((node, index) => {
        if (node.type === "summary") {
          const runTask = tasks.find((candidate) => (
                candidate.id === node.run?.taskId
                || (candidate.run_id != null && candidate.run_id === (node.run?.id ?? node.key))
              )) ?? null;
          const replyId = node.run?.id ?? node.key;
          const retrieval = shownKnowledge.has(replyId) ? undefined : knowledgeProgress[replyId];
          shownKnowledge.add(replyId);
          return (
            <div key={`summary-${runTask?.id ?? node.run?.taskId ?? node.key}`}>
            {!(retrieval?.status === "running" && !node.tools.length) && <RunSummary
              key={`summary-${runTask?.id ?? node.run?.taskId ?? node.key}`}
              run={node.run}
              tools={node.tools}
              steps={node.isLast ? steps : []}
              progress={node.progress}
              process={node.process}
              taskStatus={runTask?.status ?? null}
              executionPhase={runTask?.execution_phase}
              onOpenActivity={() => onOpenActivity(node.run?.id ?? null)}
            />}
            {progressAt(index)}
            {retrieval && <p role="status" className="mt-2 text-xs text-[var(--ink-soft)]">{retrieval.failure_count ? "知识库检索未全部成功，请查看检索详情。" : retrieval.status === "running" && (!runTask || ["created", "running", "awaiting_permission"].includes(runTask.status)) ? "正在查阅知识库…" : retrieval.status === "completed" ? `已查阅知识库${retrieval.search_count ? ` · ${retrieval.search_count} 次检索` : ''}` : "知识库检索已停止"}</p>}
            </div>
          );
        }
        const item = node.item;
        // Raw model thinking is intentionally not rendered. Public progress is
        // represented by the aggregated run status line and Task Timeline.
        if (item.kind === "reasoning") return null;
        if (item.kind === "tool") return null;
        if (item.role === "user") {
          return <div key={item.id} className="space-y-3">{taskFor(item)?.origin === "channel" && <p className="text-right text-xs text-[var(--ink-faint)]">IM 渠道发起</p>}{taskFor(item)?.origin === "schedule" && <p className="text-right text-xs text-[var(--ink-faint)]">定时任务触发 · 无人值守</p>}<div className="flex justify-end"><div className="max-w-[82%] whitespace-pre-wrap break-words rounded-[20px] rounded-br-[6px] bg-[var(--ink)] px-4 py-3 text-[14px] leading-6 text-[var(--surface)] shadow-sm">{item.text}</div></div>{progressAt(index)}</div>;
        }
        const task = taskFor(item);
        const finished = task ? ["completed", "failed", "cancelled", "interrupted"].includes(task.status) : runs.some((run) => run.id === item.runId && ["RUN_FINISHED", "RUN_ERROR", "RUN_CANCELLED"].includes(run.status));
        const itemArtifacts = finished && lastAnswer.get(answerKey(item)) === item.id
          ? artifacts.filter((artifact) => artifact.task_id === (task?.id ?? item.taskId)) : [];
        const gallery = item.done ? <div className="ml-[52px]">
          <ImageSearchResults result={answerImages.get(index) ?? []} answer={item.text} />
        </div> : null;
        return <div key={item.id} className="space-y-3">{(item.incomplete || (!node.hideText && (item.text.trim() || !item.presentation?.process_end))) && <AssistantMessage item={item} />}{gallery}{lastAnswer.get(answerKey(item)) === item.id && <TaskKnowledgeSources taskId={item.taskId ?? task?.id} runId={item.runId} complete={task ? ["completed", "failed", "cancelled", "interrupted"].includes(task.status) : item.done} live={knowledgeSources} />}<ArtifactReferences artifacts={itemArtifacts} onOpen={onOpenArtifact} />{progressAt(index)}</div>;
      })}
      {activity && activityIndex === -1 && (approval ? <div className="ml-[52px]">{approval}</div> : <TaskProgress activity={activity} standalone />)}
    </div>
  );
}
