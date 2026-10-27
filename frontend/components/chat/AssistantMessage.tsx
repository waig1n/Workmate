"use client";

import { memo, useState } from "react";

import { Icon } from "@/components/ui/Icons";
import type { ChatTextMsg } from "@/lib/chat/model";
import { MarkdownMessage } from "./MarkdownMessage";

export const AssistantMessage = memo(function AssistantMessage({ item }: { item: ChatTextMsg }) {
  const [copied, setCopied] = useState(false);
  return (
    <article className="group relative flex justify-start gap-4">
      <div className="mt-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-[13px] bg-[var(--accent-soft)] text-[var(--accent)]"><Icon name="spark" className="h-4 w-4" /></div>
      <div className="min-w-0 max-w-[94%] flex-1 py-1 pr-9">
        {item.text ? <MarkdownMessage content={item.text} /> : !item.incomplete && <span className="inline-flex items-center gap-2 text-[13px] text-[var(--ink-faint)]"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--accent)]" />工作中…</span>}
        {!item.incomplete && !item.done && item.text && <span className="ml-0.5 animate-pulse text-neutral-400">▍</span>}
        {item.incomplete && <p role="status" className="mt-2 text-xs text-[var(--ink-soft)]">这条回复内容不完整</p>}
        {item.done && item.text && (
          <button type="button" aria-label={copied ? "已复制" : "复制回答"} title={copied ? "已复制" : "复制"} onClick={async () => { await navigator.clipboard.writeText(item.text); setCopied(true); window.setTimeout(() => setCopied(false), 1500); }} className="absolute right-0 top-0 grid h-8 w-8 place-items-center rounded-lg text-[var(--ink-faint)] opacity-0 transition-opacity hover:bg-[var(--surface-subtle)] hover:text-[var(--ink)] focus:opacity-100 group-focus-within:opacity-100 group-hover:opacity-100"><Icon name={copied ? "check" : "copy"} className="h-3.5 w-3.5" /></button>
        )}
      </div>
    </article>
  );
});
