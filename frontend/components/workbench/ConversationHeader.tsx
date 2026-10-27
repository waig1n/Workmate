"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Icon } from "@/components/ui/Icons";
import { api } from "@/lib/api";
import { approvalProfileLabel } from "@/lib/approvalProfiles";
import { taskDisplayStatus } from "@/lib/taskExecutionPhase";
import type { Conversation, Task } from "@/lib/types";

const ACTIVE_TASK_LABELS: Record<string, string> = {
  created: "工作中",
  preparing: "工作中",
  running: "工作中",
  awaiting_permission: "等待审批",
  cancelling: "正在停止",
};

export function ConversationHeader({
  conversation,
  task,
  artifactCount,
  blockedMessage,
  onOpenArtifacts,
}: {
  conversation: Conversation | undefined;
  task: Task | null;
  artifactCount: number;
  blockedMessage: string | null;
  onOpenArtifacts: () => void;
}) {
  const queryClient = useQueryClient();
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const titleInputRef = useRef<HTMLInputElement>(null);
  const rename = useMutation({
    mutationFn: (title: string) => api.patch<Conversation>(`/conversations/${conversation?.id}`, { title }),
    onSuccess: async () => {
      setEditingTitle(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        queryClient.invalidateQueries({ queryKey: ["conversation", conversation?.id] }),
      ]);
    },
  });
  useEffect(() => {
    if (editingTitle) titleInputRef.current?.focus();
  }, [editingTitle]);
  // Only transient work state deserves header space; terminal states are
  // already summarized inline in the message stream.
  const activeLabel = task ? ACTIVE_TASK_LABELS[taskDisplayStatus(task)] : null;

  return (
    <header className="flex min-h-[68px] items-center gap-3 border-b border-[var(--line)] bg-[color-mix(in_srgb,var(--surface)_88%,transparent)] px-4 py-2.5 backdrop-blur-xl lg:px-6">
        <span aria-label="当前智能体" title="本对话使用的智能体；使用其他智能体请返回工作台新建对话" className="inline-flex h-10 shrink-0 max-w-[120px] sm:max-w-[180px] items-center gap-1.5 rounded-xl border border-[var(--line)] px-2.5 text-xs text-[var(--ink-soft)]"><Icon name="agents" className="h-4 w-4 shrink-0" /><span className="truncate">{conversation?.agent?.display_name ?? "智能体不可用"}</span></span>
      <div className="min-w-0 flex-1">

        {editingTitle ? (
          <form className="flex max-w-[520px] items-center gap-2" onSubmit={(event) => { event.preventDefault(); const next = titleDraft.trim(); if (next && next !== conversation?.title) rename.mutate(next); else setEditingTitle(false); }}>
            <input ref={titleInputRef} aria-label="会话标题" value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") setEditingTitle(false); }} className="h-8 min-w-0 flex-1 rounded-lg border border-[var(--accent)] bg-[var(--surface-raised)] px-2.5 text-[14px] font-semibold outline-none" maxLength={120} />
            <button type="submit" disabled={rename.isPending || !titleDraft.trim()} className="rounded-lg bg-[var(--ink)] px-2.5 py-1.5 text-[11px] font-semibold text-white disabled:opacity-40">保存</button>
          </form>
        ) : (
          <button type="button" className="group flex max-w-full items-center gap-2 text-left" onClick={() => { setTitleDraft(conversation?.title ?? ""); setEditingTitle(true); }} title="编辑会话标题">
            <h1 className="truncate text-[14px] font-semibold tracking-[-.01em]">{conversation?.title ?? "会话"}</h1><span className="shrink-0 text-[11px] text-[var(--ink-faint)] opacity-0 transition-opacity group-hover:opacity-100">编辑</span>
          </button>
        )}
        <p className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--ink-faint)]">{task && <><span>审批：{approvalProfileLabel(task.approval_profile)}</span></>}{activeLabel && <><span>·</span><span className={task?.status === "awaiting_permission" ? "text-amber-600" : "text-[var(--accent)]"}>{activeLabel}</span></>}</p>{(blockedMessage || rename.isError) && <p className="mt-1 truncate text-[10px] text-amber-600 dark:text-amber-400" title={String(blockedMessage || rename.error)}>{blockedMessage ?? (rename.error as Error)?.message}</p>}
      </div>
      <div className="flex flex-wrap items-center justify-end gap-1.5">
        {artifactCount > 0 && <button type="button" aria-label={`打开成果（${artifactCount}）`} onClick={onOpenArtifacts} className="inline-flex h-9 items-center gap-1.5 rounded-xl border border-neutral-200 bg-white px-2.5 text-[10px] text-neutral-500 hover:text-neutral-800 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-300"><Icon name="archive" className="h-3.5 w-3.5" /><span className="hidden sm:inline">成果</span>{artifactCount}</button>}
      </div>
    </header>
  );
}
