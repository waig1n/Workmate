"use client";

import type { useConversationModel } from "@/lib/useConversationModel";

export function ModelSelector({ selection }: { selection: ReturnType<typeof useConversationModel> }) {
  return <div className="flex min-w-0 max-w-full flex-col items-end gap-1">
    {selection.loading ? <span className="text-xs text-[var(--ink-faint)]">模型加载中…</span> : <select
      aria-label="对话模型" value={selection.value} disabled={selection.disabled}
      title={selection.hint} onChange={(event) => selection.onChange(event.target.value)}
      className="h-9 min-w-0 max-w-[180px] cursor-pointer rounded-lg bg-transparent px-2 text-xs font-medium text-[var(--ink-soft)] outline-none hover:bg-[var(--surface-subtle)] disabled:cursor-not-allowed disabled:opacity-60 sm:max-w-[240px]"
    >
      {!selection.profiles.length && <option value="">暂无可用模型</option>}
      {selection.value && !selection.profiles.some((profile) => profile.id === selection.value) && <option value={selection.value}>{selection.value}（不可用）</option>}
      {selection.profiles.map((profile) => <option key={profile.id} value={profile.id} disabled={profile.available === false}>{profile.display_name || profile.id}{profile.available === false ? "（不可用）" : ""}</option>)}
    </select>}
    {selection.pending && <span role="status" className="text-xs text-[var(--ink-faint)]">正在切换模型…</span>}
    {selection.error && <span role="alert" className="max-w-[240px] text-right text-xs text-[var(--danger)]">{selection.error} <button type="button" onClick={selection.retry} className="underline">刷新模型列表</button></span>}
  </div>;
}
