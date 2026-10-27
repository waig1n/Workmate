"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import type { PendingConfirm } from "@/lib/types";

export function TaskSecurityPanel({
  pending,
  onEnable,
  submitting = false,
  error,
}: {
  pending: PendingConfirm;
  onEnable: (decisions: { tool_call_id: string; approved: boolean }[]) => void;
  submitting?: boolean;
  error?: string | null;
}) {
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirmationOpen, setConfirmationOpen] = useState(false);

  function enable() {
    if (!acknowledged) return;
    setConfirmationOpen(true);
  }

  function confirmEnable() {
    onEnable(pending.toolCalls.map((call) => ({
      tool_call_id: call.id,
      approved: true,
    })));
  }

  if (pending.approvalProfile === "unattended") return <p className="text-xs text-[var(--ink-soft)]">无人值守任务不能开启自动执行。</p>;

  return (
    <>
      <details className="text-xs text-[var(--ink-faint)]">
        <summary className="cursor-pointer font-medium text-neutral-500">审批设置</summary>
        <div className="mt-3 rounded-xl border border-red-200 bg-red-50 p-3 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
          <p className="font-semibold">自动执行（高风险）</p>
          <p className="mt-1 leading-5">仅当前任务生效，会跳过后续普通询问。Agent 创建时设置的工具禁止规则、企业拒绝规则和源系统权限仍然有效。</p>
          <label className="mt-3 flex items-start gap-2">
            <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} className="mt-0.5" />
            <span>我理解该任务后续普通操作将不再逐项询问</span>
          </label>
          <button type="button" disabled={!acknowledged || submitting} onClick={enable} className="mt-3 rounded-lg bg-red-700 px-3 py-2 font-medium text-white disabled:opacity-35">{submitting ? "处理中…" : "为当前任务开启自动执行"}</button>
        </div>
      </details>

      <Dialog
        open={confirmationOpen}
        onClose={() => { if (!submitting) setConfirmationOpen(false); }}
        title="确认开启自动执行？"
        description="这是当前任务的临时高风险授权，不会改变组织策略或后续任务的默认设置。"
        footer={(
          <>
            <Button type="button" variant="ghost" size="sm" disabled={submitting} onClick={() => setConfirmationOpen(false)}>取消</Button>
            <button type="button" disabled={submitting} onClick={confirmEnable} className="h-9 rounded-xl bg-red-700 px-4 text-xs font-semibold text-white transition hover:bg-red-800 disabled:opacity-40">{submitting ? "正在开启…" : "确认，仅为当前任务开启"}</button>
          </>
        )}
      >
        <div className="space-y-3 text-[12px] leading-5 text-[var(--ink-soft)]">
          {error && <p role="alert" className="text-red-600">提交失败：{error}。请重试或取消。</p>}
          <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
            开启后，本任务剩余普通操作不再逐项询问，任务结束、失败或停止后自动恢复默认审批模式。
          </div>
          <ul className="list-disc space-y-1.5 pl-4 marker:text-[var(--danger)]">
            <li>仅在支持该审批模式的隔离工作区中生效。</li>
            <li>Agent 工具禁止规则与企业级显式拒绝规则仍然有效。</li>
            <li>创建或修改智能体、安装技能等操作仍需确认。</li>
            <li>授权决定与执行结果继续写入任务审计。</li>
          </ul>
        </div>
      </Dialog>
    </>
  );
}
