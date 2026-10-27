"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";
import { Icon } from "@/components/ui/Icons";
import { useWorkspaceDraft } from "@/components/workspace/WorkspaceDraftProvider";
import { ThinkingStatus } from "@/components/chat/ThinkingStatus";

export default function PendingConversationPage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const { pendingSubmission, retrySubmission, selectAgent } = useWorkspaceDraft();
  const pending = pendingSubmission?.token === token ? pendingSubmission : null;
  useEffect(() => {
    if (pending?.status === "sent" && pending.conversationId) router.replace(`/workspace/c/${pending.conversationId}`);
  }, [pending?.status, pending?.conversationId, router]);
  if (!pending) return <div className="grid h-full place-items-center p-6"><div className="max-w-md space-y-4 text-center text-sm text-[var(--ink-soft)]"><p>请先查看历史对话，确认刚才的消息是否已发送。文字草稿已保留，刷新后附件需重新添加。</p><div className="flex justify-center gap-4"><Link className="underline" href="/workspace/tasks">查看历史对话</Link><Link className="underline" href="/workspace">返回对话</Link></div></div></div>;
  return <div className="flex h-full flex-col bg-[var(--surface)]">
    <header className="border-b border-[var(--line)] px-6 py-5 text-sm font-medium">{pending.agent.display_name}</header>
    <div className="min-h-0 flex-1 overflow-y-auto px-5 py-8"><div className="mx-auto max-w-[880px] space-y-6">
      <div className="flex justify-end"><div className="max-w-[82%] whitespace-pre-wrap break-words rounded-[20px] rounded-br-[6px] bg-[var(--ink)] px-4 py-3 text-sm leading-6 text-[var(--surface)]">{pending.draft.input}{pending.draft.attachments.map((file, index) => <div key={`${file.name}-${index}`} className="mt-2 flex items-center gap-2"><Icon name="file" className="h-4 w-4 shrink-0" />{file.name}</div>)}</div></div>
      {pending.status === "failed" ? <div role="alert" className="space-y-3 text-sm"><p className="text-[var(--danger)]">发送失败，内容已保留。</p><p className="text-[var(--ink-soft)]">{pending.error}</p><div className="flex flex-wrap gap-4"><button type="button" className="underline" onClick={() => void retrySubmission(token)}>重试发送</button>{pending.conversationId && <Link className="underline" href={`/workspace/c/${pending.conversationId}`}>查看对话</Link>}<Link className="underline" href="/workspace" onClick={() => selectAgent(pending.agent.id)}>返回编辑</Link></div></div> : <ThinkingStatus />}
    </div></div>
    <div className="px-5 pb-24 pt-3 lg:pb-5"><div className="mx-auto max-w-[880px] rounded-[22px] border border-[var(--line)] bg-[var(--surface-raised)] px-5 py-6 text-sm text-[var(--ink-faint)]"><textarea aria-label="任务描述" disabled rows={1} placeholder="输入消息…" className="w-full resize-none bg-transparent outline-none" /></div></div>
  </div>;
}
