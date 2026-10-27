"use client";

import { useEffect, useId, useRef, useState } from "react";

import { Icon } from "@/components/ui/Icons";
import { APPROVAL_PROFILES, approvalProfileLabel } from "@/lib/approvalProfiles";
import type { ApprovalProfile, TaskApprovalProfile } from "@/lib/types";

const MAX_ATTACHMENT_FILES = 10;
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;

function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function Composer({
  input,
  onInputChange,
  attachments,
  onAttachmentsChange,
  approvalProfile,
  onApprovalProfileChange,
  runningTaskProfile,
  disabled,
  busy,
  sending,
  error,
  notice,
  placeholder,
  onSubmit,
  modelSelector,
  centered = false,
  onStop,
  stopping = false,
  stopError,
}: {
  onStop?: () => void;
  stopping?: boolean;
  stopError?: string | null;
  modelSelector?: React.ReactNode;
  centered?: boolean;
  input: string;
  onInputChange: (value: string) => void;
  attachments: File[];
  onAttachmentsChange: (files: File[]) => void;
  approvalProfile: ApprovalProfile;
  onApprovalProfileChange: (profile: ApprovalProfile) => void;
  /** Frozen profile of the currently executing task; the selector then
   * shows the next-task preference alongside the task's real level. */
  runningTaskProfile: TaskApprovalProfile | null;
  disabled: boolean;
  busy: boolean;
  sending: boolean;
  error: string | null;
  notice?: string | null;
  placeholder: string;
  onSubmit: () => void;
}) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [approvalHelpOpen, setApprovalHelpOpen] = useState(false);
  const approvalHelpId = useId();

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
  }, [input]);

  function addFiles(incoming: File[]) {
    setAttachmentError(null);
    const next = [...attachments];
    const seen = new Set(next.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
    for (const file of incoming) {
      if (next.length >= MAX_ATTACHMENT_FILES) {
        setAttachmentError(`每个任务最多上传 ${MAX_ATTACHMENT_FILES} 个文件`);
        break;
      }
      if (file.size > MAX_ATTACHMENT_BYTES) {
        setAttachmentError(`${file.name} 超过 25 MB 限制`);
        continue;
      }
      const key = `${file.name}:${file.size}:${file.lastModified}`;
      if (!seen.has(key)) {
        next.push(file);
        seen.add(key);
      }
    }
    onAttachmentsChange(next);
  }

  // An executing Task does not lock the Composer: the Product layer queues
  // the next Task for this conversation. Only access/loading and the current
  // HTTP submission are hard locks.
  const locked = disabled || sending;
  const hasContent = Boolean(input.trim() || attachments.length);
  const showStop = busy && Boolean(onStop) && !hasContent;
  return (
    <form onSubmit={(event) => { event.preventDefault(); if (!locked && hasContent) onSubmit(); }} className={`px-4 pt-3 lg:px-8 ${centered ? "pb-4" : "bg-gradient-to-t from-[var(--surface)] via-[var(--surface)]/95 pb-24 lg:pb-5"}`}>
      {stopError && <p role="alert" className="mx-auto mb-2 max-w-[880px] px-3 text-xs text-red-500">停止失败：{stopError}，请重试。</p>}
      <div className={`mx-auto max-w-[880px] composer-surface rounded-[22px] border bg-[var(--surface-raised)] p-2.5 ${dragActive ? "border-[var(--accent)]" : "border-[var(--line-strong)]"}`} onDragEnter={(event) => { event.preventDefault(); if (!locked) setDragActive(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragActive(false); }} onDrop={(event) => { event.preventDefault(); setDragActive(false); if (!locked) addFiles(Array.from(event.dataTransfer.files)); }}>
        {attachments.length > 0 && <div className="flex flex-wrap gap-1.5 px-2 pb-2 pt-1">{attachments.map((file, index) => <span key={`${file.name}-${file.lastModified}`} className="flex max-w-[240px] items-center gap-1.5 rounded-lg border border-neutral-200 bg-neutral-50 px-2 py-1.5 text-[9px] text-neutral-600 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-300"><Icon name="file" className="h-3 w-3 shrink-0 text-blue-500" /><span className="truncate">{file.name}</span><span className="shrink-0 text-neutral-400">{sending ? "正在上传…" : formatFileSize(file.size)}</span><button type="button" disabled={sending} aria-label={`移除 ${file.name}`} className="ml-0.5 text-neutral-400 hover:text-red-500 disabled:opacity-30" onClick={() => onAttachmentsChange(attachments.filter((_, itemIndex) => itemIndex !== index))}><Icon name="x" className="h-3 w-3" /></button></span>)}</div>}
        <div className="flex items-end gap-2 px-2">
          <input ref={fileInputRef} type="file" multiple className="hidden" onChange={(event) => { addFiles(Array.from(event.target.files ?? [])); event.currentTarget.value = ""; }} />
          <textarea ref={textareaRef} rows={1} className={`max-h-40 min-w-0 flex-1 resize-none overflow-y-auto bg-transparent px-1 py-2.5 text-[15px] leading-7 outline-none placeholder:text-[var(--ink-faint)] disabled:opacity-60 ${centered ? "min-h-28" : "min-h-16"}`} value={input} onChange={(event) => onInputChange(event.target.value)} onPaste={(event) => { const files = Array.from(event.clipboardData.files); if (files.length && !locked) addFiles(files); }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder={placeholder} disabled={locked} aria-label="任务描述" />

        </div>
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-1 pt-2 text-xs text-[var(--ink-faint)]">
          <div className="flex min-w-0 max-w-full flex-wrap items-center gap-1">
          <button type="button" disabled={locked || attachments.length >= MAX_ATTACHMENT_FILES} onClick={() => fileInputRef.current?.click()} className="mb-0.5 grid h-10 w-10 shrink-0 place-items-center rounded-[13px] text-[var(--ink-faint)] hover:bg-[var(--surface-subtle)] hover:text-[var(--ink)] disabled:opacity-30" aria-label="上传文件" title="上传文件（单个不超过 25 MB）"><Icon name="paperclip" className="h-[18px] w-[18px]" /></button>

            <label className="flex items-center gap-1 rounded-lg px-1.5 py-1 hover:bg-[var(--surface-subtle)]" title={APPROVAL_PROFILES.find((profile) => profile.value === approvalProfile)?.description}><Icon name="shield" className="h-3 w-3" /><span>{busy ? "下次执行：" : "执行方式："}</span><select value={approvalProfile} disabled={locked} onChange={(event) => onApprovalProfileChange(event.target.value as ApprovalProfile)} className="cursor-pointer bg-transparent font-semibold text-[var(--ink-soft)] outline-none disabled:opacity-50" aria-label="下一任务审批等级">{APPROVAL_PROFILES.map((profile) => <option key={profile.value} value={profile.value}>{profile.label}{profile.value === "standard" ? "（推荐）" : ""}</option>)}</select></label>
            <button type="button" aria-label="执行方式说明" aria-expanded={approvalHelpOpen} aria-controls={approvalHelpId} onClick={() => setApprovalHelpOpen((open) => !open)} className="grid h-8 w-8 shrink-0 place-items-center rounded-full text-xs font-semibold hover:bg-[var(--surface-subtle)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]">?</button>
            {runningTaskProfile && <span className="hidden truncate text-[var(--ink-faint)] md:inline" title="当前任务的审批等级在启动时已固化">当前任务：{approvalProfileLabel(runningTaskProfile)}</span>}
          </div>
          <div className="ml-auto flex min-w-0 max-w-full items-center gap-2">{modelSelector}
          {busy && onStop && hasContent && <button type="button" aria-label={stopping ? "正在停止" : "停止执行"} disabled={stopping} onClick={onStop} className="shrink-0 rounded-lg px-2 py-2 text-xs hover:bg-[var(--surface-subtle)] disabled:opacity-40">{stopping ? "正在停止" : "停止执行"}</button>}
          <button type={showStop ? "button" : "submit"} aria-label={showStop ? stopping ? "正在停止" : "停止执行" : error ? "重试发送" : "发送任务"} title={showStop ? "停止当前执行" : busy ? "继续发送" : "发送任务"} onClick={showStop ? onStop : undefined} disabled={showStop ? stopping : locked || !hasContent} className="grid h-10 w-10 shrink-0 place-items-center rounded-[13px] bg-[var(--ink)] text-[var(--surface)] shadow-sm hover:-translate-y-0.5 hover:shadow-md disabled:opacity-25">{showStop ? <span aria-hidden className="h-3.5 w-3.5 rounded-[3px] bg-current" /> : <Icon name="arrow" className="h-[18px] w-[18px] -rotate-90" />}</button>
          </div>
        </div>
        {approvalHelpOpen && <div id={approvalHelpId} className="mt-2 rounded-xl bg-[var(--surface-subtle)] p-3 text-xs leading-5 text-[var(--ink-soft)]">
          <p className="font-medium">仅影响下一次发送，不会更改正在执行的任务。</p>
          <dl className="mt-2 space-y-2">{APPROVAL_PROFILES.map((profile) => <div key={profile.value}><dt className="font-medium">{profile.label}</dt><dd className="text-[var(--ink-faint)]">{profile.description}</dd></div>)}</dl>
          <p className="mt-2">自动执行仍须在隔离沙箱内运行；智能体和企业的禁止规则始终有效，创建或修改智能体、安装技能等操作仍需确认。</p>
        </div>}
      </div>
      {(attachmentError || error) && <p className="mx-auto mt-2 max-w-[880px] px-2 text-[11px] text-red-500" role="alert">{attachmentError || `发送失败：${error}。内容和附件已保留，可直接重试。`}</p>}
      {!attachmentError && !error && notice && <p className="mx-auto mt-2 max-w-[880px] px-2 text-[11px] text-[var(--ink-faint)]" role="status">{notice}</p>}
    </form>
  );
}
