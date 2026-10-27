"use client";

import { mergeRealtimeTasks } from "@/lib/chat/realtime-tasks";
import { taskRefreshInterval } from "@/lib/taskExecutionPhase";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { ActivityBoard, ActivityLauncher } from "@/components/activity/ActivityBoard";
import { TaskRecovery } from "@/components/chat/TaskRecovery";
import { ApprovalCard } from "@/components/chat/ApprovalCard";
import { MessageList } from "@/components/chat/MessageList";
import { TaskSecurityPanel } from "@/components/chat/TaskSecurityPanel";
import { useWorkspaceDraft } from "@/components/workspace/WorkspaceDraftProvider";
import { ModelSelector } from "@/components/composer/ModelSelector";
import { useConversationModel } from "@/lib/useConversationModel";
import { Composer } from "@/components/composer/Composer";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { ConversationHeader } from "@/components/workbench/ConversationHeader";
import {
  WorkbenchSidecar,
} from "@/components/workbench/WorkbenchSidecar";
import { api } from "@/lib/api";
import { historyWithReplyIds, mergeHistoryWithReplay } from "@/lib/messageMerge";
import { conversationBlockState } from "@/lib/permissions";
import type {
  ApprovalProfile,
  Conversation,
  Message,
  PendingConfirm,
  Task,
} from "@/lib/types";
import { usePermissions } from "@/lib/usePermissions";
import { useArtifactPanelWidth } from "@/lib/useArtifactPanelWidth";
import { useConversationApproval } from "@/lib/useConversationApproval";
import { useChatScroll } from "@/lib/useChatScroll";
import { useTaskStream } from "@/lib/useTaskStream";

const EXECUTING_STATUSES = new Set([
  "running", "awaiting_permission", "cancelling",
]);

export default function ConversationPage() {
  const params = useParams<{ conversationId: string }>();
  const conversationId = params.conversationId;
  const { getConversationFiles, setConversationFiles, pendingSubmission } = useWorkspaceDraft();
  const initialSend = pendingSubmission?.status === "sent" && pendingSubmission.conversationId === conversationId ? pendingSubmission : null;
  const {
    items,
    realtime,
    run,
    runs,
    pendingConfirm,
    approvalSubmitting,
    approvalError,
    knowledgeSources,
    knowledgeProgress,
    artifacts,
    artifactsHydratedFor,
    steps,
    sendMessage,
    confirm,
    enableFullAccess,
    stop,
    hydratePendingConfirm,
    reconcileTasks,
    recoveryError,
    retryRecovery,
    refreshConversation,
  } = useTaskStream(conversationId);
  const permissions = usePermissions();

  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const [currentTask, setCurrentTask] = useState<Task | null>(null);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [historySeed, setHistorySeed] = useState<
    { kind: "text"; id: string; role: string; text: string; done: boolean; taskId: string; presentation?: { version: number; process_end: number } }[]
  >([]);
  const [artifactPanelOpen, setArtifactPanelOpen] = useState(false);
  const [focusedArtifactId, setFocusedArtifactId] = useState<string | null>(null);
  const [artifactFocusRequest, setArtifactFocusRequest] = useState(0);
  const [activityBoardOpen, setActivityBoardOpen] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const [stopPending, setStopPending] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [sendNotice, setSendNotice] = useState<string | null>(null);
  const [fullAccessDialogOpen, setFullAccessDialogOpen] = useState(false);
  const [activityTaskId, setActivityTaskId] = useState<string | null>(null);
  const [draftConversationId, setDraftConversationId] = useState<string | null>(null);
  const chatScroll = useChatScroll(conversationId);
  const previousArtifactCount = useRef(0);
  const artifactBaselineReady = useRef(false);
  const artifactPanel = useArtifactPanelWidth();

  const conversation = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => api.get<Conversation>(`/conversations/${conversationId}`),
  });
  const tasks = useQuery({
    queryKey: ["tasks", conversationId],
    queryFn: () => api.get<Task[]>(`/tasks?conversation_id=${conversationId}`),
    // SSE can precede the Product terminal transaction. Poll only while
    // outstanding work exists, not for completed conversation history.
    refetchInterval: (query) => taskRefreshInterval(query.state.data),
  });

  useEffect(() => {
    if (tasks.data) reconcileTasks(tasks.data);
  }, [tasks.data, tasks.dataUpdatedAt, realtime?.generation, reconcileTasks]);

  const effectiveTasks = useMemo(() => realtime
    ? mergeRealtimeTasks(realtime.tasks, tasks.data ?? [])
    : tasks.data, [realtime, tasks.data]);

  const [approvalProfile, setApprovalProfile] = useConversationApproval(
    conversationId, initialSend?.draft.approvalProfile, effectiveTasks?.[0]?.approval_profile,
  );

  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const requestedPanel = query.get("panel");
    const requestedActivity = requestedPanel === "activity" || query.get("activity") === "1";
    const draftKey = `workmate_draft:${conversationId}`;
    let savedInput = "";
    try { savedInput = window.localStorage.getItem(draftKey) ?? ""; } catch { /* Restoring attachments must not depend on browser storage. */ }
    setInput(savedInput);
    setDraftConversationId(conversationId);
    setAttachments(getConversationFiles(conversationId));
    setSendError(null);
    setStopError(null);
    setSendNotice(null);
    setHistoryLoaded(false);
    setHistorySeed([]);
    setCurrentTask(null);
    setArtifactPanelOpen(requestedPanel === "artifacts" || query.get("artifacts") === "1");
    setFocusedArtifactId(query.get("artifact"));
    setArtifactFocusRequest((value) => value + 1);
    setActivityBoardOpen(requestedActivity);
    setActivityTaskId(requestedActivity ? query.get("task") : null);
    previousArtifactCount.current = 0;
    artifactBaselineReady.current = false;
  }, [conversationId]);

  useEffect(() => {
    if (draftConversationId !== conversationId) return;
    const draftKey = `workmate_draft:${conversationId}`;
    try {
      if (input) window.localStorage.setItem(draftKey, input);
      else window.localStorage.removeItem(draftKey);
    } catch { setSendNotice("浏览器无法保存文字草稿，离开前请复制未发送内容。"); }
  }, [conversationId, draftConversationId, input]);

  useEffect(() => {
    const taskData = effectiveTasks;
    if (!taskData) return;
    const executing = taskData.find((task) => EXECUTING_STATUSES.has(task.status));
    setCurrentTask(executing ?? taskData[0] ?? null);
  }, [effectiveTasks]);

  useEffect(() => {
    if (run.status && ["RUN_FINISHED", "RUN_CANCELLED", "RUN_ERROR"].includes(run.status)) {
      void tasks.refetch();
    }
  }, [run.status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (
      realtime
      || !currentTask
      || currentTask.status !== "awaiting_permission"
      || pendingConfirm
      || approvalSubmitting
    ) return;
    let cancelled = false;
    api.get<PendingConfirm>(`/tasks/${currentTask.id}/pending-confirm`)
      .then((pending) => {
        if (!cancelled) hydratePendingConfirm(pending);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [currentTask, pendingConfirm, approvalSubmitting, hydratePendingConfirm, realtime]);

  useEffect(() => {
    if (pendingConfirm) void tasks.refetch();
  }, [pendingConfirm?.decisionVersion]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (realtime) { setHistoryLoaded(true); return; }
    if (historyLoaded) return;
    let cancelled = false;
    api.get<Message[]>(`/conversations/${conversationId}/messages`)
      .then((messages) => {
        if (cancelled) return;
        setHistorySeed(messages.map((message) => ({
          kind: "text" as const,
          id: message.id,
          role: message.role,
          text: [
            message.content?.text ?? "",
            ...(message.content?.attachments ?? []).map((file) => `📎 ${file.name}`),
          ].filter(Boolean).join("\n\n"),
          done: true,
          taskId: message.task_id,
          presentation: message.content.presentation,
        })));
        setHistoryLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setHistoryLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, historyLoaded, realtime]);


  useEffect(() => {
    if (artifactsHydratedFor !== conversationId) return;
    if (!artifactBaselineReady.current) {
      previousArtifactCount.current = artifacts.length;
      artifactBaselineReady.current = true;
      return;
    }
    if (artifacts.length > previousArtifactCount.current
      && window.matchMedia("(min-width: 1280px)").matches) {
      setArtifactPanelOpen(true);
    }
    previousArtifactCount.current = artifacts.length;
  }, [artifacts.length, artifactsHydratedFor, conversationId]);

  const blockState = conversation.data
    ? conversationBlockState(conversation.data)
    : { blocked: true, kind: null, unavailableIds: [] as string[] };
  const composerDisabled = conversation.isLoading || conversation.isError || blockState.blocked;
  const taskBusy = currentTask && currentTask.run_id === run.runId
    ? EXECUTING_STATUSES.has(currentTask.status)
    : Boolean(currentTask && EXECUTING_STATUSES.has(currentTask.status)) || run.active;
  const modelSelection = useConversationModel(conversation.data, tasks.isPending || sending || run.active || Boolean(effectiveTasks?.some((task) => ["created", "running", "awaiting_permission", "cancelling"].includes(task.status))));
  const mergedItems = realtime ? items : historyLoaded
    ? mergeHistoryWithReplay(historyWithReplyIds(historySeed, effectiveTasks ?? []), items)
    : items;
  const allItems = initialSend && !mergedItems.some((item) => item.kind === "text" && item.role === "user" && item.taskId === initialSend.task?.id)
    ? [{ kind: "text" as const, id: `pending-${initialSend.token}`, role: "user", text: [initialSend.draft.input, ...initialSend.draft.attachments.map((file) => `📎 ${file.name}`)].filter(Boolean).join("\n\n"), done: true, taskId: initialSend.task?.id ?? "" }, ...mergedItems]
    : mergedItems;
  const artifactSeriesCount = new Set(artifacts.map((artifact) => artifact.name)).size;
  const activityTask = effectiveTasks?.find((task) => task.id === activityTaskId)
    ?? currentTask
    ?? effectiveTasks?.[0]
    ?? null;
  const selectedRunId = activityTask?.run_id
    ?? (activityTask?.id === currentTask?.id ? run.runId : null);
  const toolItems = items.filter(
    (item): item is (typeof items)[number] & { kind: "tool" } =>
      item.kind === "tool" && Boolean(selectedRunId) && item.runId === selectedRunId,
  );
  const runningTaskProfile = taskBusy && currentTask
    ? currentTask.approval_profile
    : null;

  function chooseApprovalProfile(profile: ApprovalProfile) {
    if (profile === "full_access") {
      setFullAccessDialogOpen(true);
      return;
    }
    applyApprovalProfile(profile);
  }

  function applyApprovalProfile(profile: ApprovalProfile) {
    setApprovalProfile(profile);
  }

  async function submit() {
    const text = input.trim();
    if ((!text && !attachments.length) || composerDisabled || sending || modelSelection.pending || modelSelection.isSwitching()) return;
    setSending(true);
    setSendError(null);
    setSendNotice(null);
    try {
      await sendMessage(
        text,
        attachments,
        approvalProfile,
        (task, queued) => {
          if (!queued) setCurrentTask(task);
          setSendNotice(queued ? "已收到，将在当前回复结束后处理。" : null);
        },
        taskBusy,
      );
      setInput("");
      setAttachments([]);
      setConversationFiles(conversationId, []);
      await tasks.refetch();
    } catch (reason) {
      setSendError(reason instanceof Error ? reason.message : "发送失败");
    } finally {
      setSending(false);
    }
  }

  const blockedMessage = blockState.kind === "capability_revoked"
    ? "组织能力访问已撤销，修复智能体配置后才能发起新任务。"
    : blockState.kind === "access_revoked"
      ? conversation.data?.read_only_reason ?? "该会话为只读"
      : null;

  function syncPanelLocation(artifactsOpen: boolean, activityOpen: boolean, taskId?: string | null, artifactId?: string | null) {
    const url = new URL(window.location.href);
    url.searchParams.delete("panel");
    if (artifactsOpen) url.searchParams.set("artifacts", "1");
    else url.searchParams.delete("artifacts");
    if (artifactsOpen && artifactId) url.searchParams.set("artifact", artifactId);
    else url.searchParams.delete("artifact");
    if (activityOpen) url.searchParams.set("activity", "1");
    else url.searchParams.delete("activity");
    if (activityOpen && taskId) url.searchParams.set("task", taskId);
    else url.searchParams.delete("task");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }

  function openActivity(taskId: string | null) {
    setActivityTaskId(taskId);
    setActivityBoardOpen(true);
    syncPanelLocation(artifactPanelOpen, true, taskId ?? currentTask?.id ?? effectiveTasks?.[0]?.id ?? null, focusedArtifactId);
  }

  function openArtifacts(artifactId?: string) {
    if (artifactId) {
      setFocusedArtifactId(artifactId);
      setArtifactFocusRequest((value) => value + 1);
    }
    setArtifactPanelOpen(true);
    syncPanelLocation(true, activityBoardOpen, activityTask?.id, artifactId ?? focusedArtifactId);
  }

  function closeArtifacts() {
    setArtifactPanelOpen(false);
    syncPanelLocation(false, activityBoardOpen, activityTask?.id, null);
  }

  function closeActivity() {
    setActivityBoardOpen(false);
    syncPanelLocation(artifactPanelOpen, false, undefined, focusedArtifactId);
  }

  async function stopCurrentTask() {
    if (!currentTask || stopPending || !["running", "awaiting_permission"].includes(currentTask.status)) return;
    setStopPending(true);
    setStopError(null);
    try {
      await stop(currentTask.id);
      await tasks.refetch();
    } catch (reason) {
      setStopError(reason instanceof Error ? reason.message : "请求未成功");
    } finally {
      setStopPending(false);
    }
  }

  const placeholder = blockState.kind === "capability_revoked"
    ? "能力访问已撤销，请先修复智能体配置"
    : blockState.kind === "access_revoked"
      ? "你已失去该智能体的使用权限，历史内容仍可查看"
      : taskBusy
        ? currentTask?.status === "cancelling"
          ? "正在停止任务，可继续输入下一项工作…"
          : "工作中，可继续发送…"
        : conversation.isLoading
          ? "正在加载会话…"
          : conversation.isError
            ? "会话加载失败，请刷新重试"
            : attachments.length
              ? "补充对附件的处理要求（可选）"
              : "输入任务描述，或拖入文件";
  const approvalVisible = Boolean(
    pendingConfirm
    && currentTask?.status === "awaiting_permission"
    && (!pendingConfirm.taskId || pendingConfirm.taskId === currentTask.id),
  );

  const emptyConversation = realtime?.sync !== "incomplete" && historyLoaded && tasks.isSuccess && !effectiveTasks?.length && !allItems.length && !taskBusy && !approvalVisible && !blockState.blocked;

  return (
    <div className="flex h-full bg-[var(--surface)]">
      <div className="relative flex min-w-0 flex-1 flex-col">
        <ConversationHeader
          conversation={conversation.data}
          task={currentTask}
          artifactCount={artifactSeriesCount}
          blockedMessage={blockedMessage}
          onOpenArtifacts={() => openArtifacts()}
        />

        {!activityBoardOpen && <ActivityLauncher task={currentTask} onOpen={() => openActivity(null)} />}

        <div className={`flex min-h-0 flex-1 flex-col ${emptyConversation ? "justify-center overflow-y-auto pb-24 lg:pb-8" : ""}`}>
        <div aria-label="对话消息" ref={chatScroll.viewportRef} onScroll={chatScroll.onScroll} className={emptyConversation ? "px-4 pb-4 pt-8 sm:px-6 lg:px-8" : "flex-1 overflow-y-auto px-4 py-8 sm:px-6 lg:px-8"}>
          <div ref={chatScroll.contentRef} className="mx-auto max-w-[880px] space-y-6">
            {recoveryError && <div role="status" className="rounded-lg border border-[var(--line)] px-4 py-3 text-sm text-[var(--ink-soft)]">
              <p>{recoveryError}</p><button type="button" className="mt-2 text-[var(--accent)] underline" onClick={retryRecovery}>重新同步</button>
            </div>}
            {emptyConversation ? <div className="text-center">
              <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">今天，我能帮你做些什么？</h2>
              <p className="mt-3 text-sm text-[var(--ink-soft)]">{conversation.data?.agent?.display_name ?? "智能体"}，为你的工作提供帮助</p>
              <div className="mt-8 flex flex-wrap justify-center gap-2">{["整理资料并总结", "分析数据与风险", "生成可交付成果"].map((prompt) => <button key={prompt} type="button" onClick={() => setInput(prompt)} className="rounded-full border border-[var(--line)] px-4 py-2 text-xs text-[var(--ink-soft)] hover:bg-[var(--surface-subtle)]">{prompt}</button>)}</div>
            </div> : <MessageList
              approval={approvalVisible && pendingConfirm && currentTask ? <ApprovalCard
                key={`${currentTask.id}:${pendingConfirm.decisionVersion}`}
                pending={pendingConfirm} submitting={approvalSubmitting} error={approvalError}
                onConfirm={(decisions) => { void confirm(currentTask.id, decisions).catch(() => undefined).finally(() => tasks.refetch()); }}
                settings={<TaskSecurityPanel pending={pendingConfirm} submitting={approvalSubmitting} error={approvalError} onEnable={(decisions) => { void enableFullAccess(currentTask.id, decisions).catch(() => undefined).finally(() => tasks.refetch()); }} />}
              /> : undefined}
              activity={taskBusy || currentTask?.status === "created" ? {
                taskId: currentTask?.id, runId: currentTask?.run_id ?? run.runId,
                label: stopPending || currentTask?.status === "cancelling" ? "正在停止…" : currentTask?.status === "awaiting_permission" ? "等待你的审批" : run.status === "reconnecting" ? "连接中断，正在恢复…" : "工作中",
                animated: currentTask?.status !== "awaiting_permission",
              } : undefined}
              items={allItems}
              runs={runs}
              historyLoaded={historyLoaded}
              historyIncomplete={realtime?.sync === "incomplete"}
              agentName={conversation.data?.agent?.display_name ?? ""}
              steps={steps}
              knowledgeSources={knowledgeSources}
              knowledgeProgress={knowledgeProgress}
              artifacts={artifacts}
              tasks={effectiveTasks ?? []}
              onPrompt={setInput}
              onOpenActivity={(runId) => { const matched = effectiveTasks?.find((task) => task.run_id === runId); openActivity(matched?.id ?? null); }}
              onOpenArtifact={openArtifacts}
            />}

            <TaskRecovery key={conversationId + (effectiveTasks?.[0]?.id ?? "") + (effectiveTasks?.[0]?.status ?? "")} conversationId={conversationId} onRecovered={async () => {
              await Promise.all([conversation.refetch(), tasks.refetch()]);
              refreshConversation();
            }} />
            {run.status === "RUN_ERROR" && run.error && <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-[11px] text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300" role="alert"><p className="font-medium">任务执行失败</p><p className="mt-1 break-words">{run.error}</p>{!conversation.data?.read_only && <button type="button" className="mt-2 underline" onClick={() => setInput("请检查刚才失败的原因并继续完成任务")}>继续对话处理</button>}</div>}
            {run.status === "RUN_CANCELLED" && <p className="rounded-lg bg-neutral-100 px-3 py-2 text-[11px] text-neutral-500 dark:bg-neutral-800">任务已中断，你可以在下方继续对话。</p>}
          </div>
        </div>

        {blockState.kind === "capability_revoked" && <div className="mx-5 mb-1 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"><p className="font-medium">此会话绑定的智能体有 {blockState.unavailableIds.length} 个能力不可用</p><p className="mt-1 text-xs">历史消息、运行事件和成果仍可查看。请前往智能体管理解绑失效安装：</p><p className="mt-2 break-all font-mono text-[11px]">{blockState.unavailableIds.join(", ")}</p></div>}

        <div className="relative z-30 shrink-0">
          {chatScroll.showLatest && <div className="absolute inset-x-0 bottom-full flex justify-center pb-3"><button type="button" onClick={chatScroll.scrollToLatest} className="rounded-full border border-[var(--line)] bg-[var(--surface-raised)] px-4 py-2 text-xs text-[var(--ink-soft)] shadow-sm">{approvalVisible ? "回到最新 · 有操作待确认" : taskBusy ? "回到最新 · 工作中" : "回到最新"}</button></div>}
          <Composer
            centered={emptyConversation}
            modelSelector={<ModelSelector selection={modelSelection} />}
            input={input}
            onInputChange={(value) => { setInput(value); if (sendError) setSendError(null); if (sendNotice) setSendNotice(null); }}
            attachments={attachments}
            onAttachmentsChange={(files) => { setAttachments(files); setConversationFiles(conversationId, files); }}
            approvalProfile={approvalProfile}
            onApprovalProfileChange={chooseApprovalProfile}
            runningTaskProfile={runningTaskProfile}
            disabled={composerDisabled || modelSelection.pending}
            busy={taskBusy}
            onStop={currentTask && EXECUTING_STATUSES.has(currentTask.status) ? stopCurrentTask : undefined}
            stopping={stopPending || currentTask?.status === "cancelling"}
            stopError={stopError}
            sending={sending}
            error={sendError}
            notice={sendNotice}
            placeholder={placeholder}
            onSubmit={submit}
          />
        </div>

        </div>

        {activityBoardOpen && <ActivityBoard tasks={effectiveTasks ?? []} task={activityTask} toolItems={toolItems} onSelectTask={openActivity} onClose={closeActivity} showAuditDetails={permissions.data?.is_root_admin ?? false} />}
      </div>

      {artifactPanelOpen && <WorkbenchSidecar artifacts={artifacts} width={artifactPanel.width} maxWidth={artifactPanel.maxWidth} onResize={artifactPanel.resizeBy} onResetWidth={artifactPanel.reset} onClose={closeArtifacts} focusArtifactId={focusedArtifactId} focusRequest={artifactFocusRequest} />}

      <Dialog
        open={fullAccessDialogOpen}
        onClose={() => setFullAccessDialogOpen(false)}
        title="确认开启自动执行？"
        description="常规操作将自动执行；创建或修改智能体、安装技能等操作仍需确认，禁止操作仍会被拦截。"
        footer={<><Button type="button" variant="ghost" size="sm" onClick={() => setFullAccessDialogOpen(false)}>取消</Button><button type="button" className="h-9 rounded-xl bg-red-700 px-4 text-xs font-semibold text-white hover:bg-red-800" onClick={() => { applyApprovalProfile("full_access"); setFullAccessDialogOpen(false); }}>确认选择自动执行</button></>}
      >
        <div className="space-y-3 text-[12px] leading-5 text-[var(--ink-soft)]">
          <p className="rounded-xl border border-red-200 bg-red-50 p-3 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">仅在受支持的隔离环境中可用，智能体、企业和外部系统的权限限制仍然生效。</p>
          <p>该选择仅作用于下一次发送的任务，不会改变已经开始的任务。</p>
        </div>
      </Dialog>
    </div>
  );
}
