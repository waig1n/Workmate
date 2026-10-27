"use client";

import { useState } from "react";
import { Icon } from "@/components/ui/Icons";
import {
  approvalProfileLabel,
  approvalRequestExplanation,
} from "@/lib/approvalProfiles";
import type { TaskApprovalProfile, PendingConfirm } from "@/lib/types";

type Decision = { tool_call_id: string; approved: boolean };

const ACTION_LABELS: Record<string, string> = {
  agent_create: "确认创建智能体",
  agent_update: "确认保存智能体变更",
  skill_install: "确认安装技能",
  bash: "运行工作区命令",
  execute: "执行工作区操作",
  write: "修改工作区文件",
  edit: "编辑工作区文件",
  delete: "删除资源",
  send_email: "发送邮件",
  email: "发送邮件",
  http_request: "访问外部服务",
};

function parsedInput(raw: string): Record<string, unknown> {
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object"
      ? value as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function actionSummary(name: string, raw: string) {
  const input = parsedInput(raw);
  const label = ACTION_LABELS[name.toLowerCase()]
    ?? `执行 ${name || "工具操作"}`;
  const target = [
    "display_name", "agent_id", "slug", "skill_directory",
    "path", "file_path", "url", "recipient", "to", "command", "query",
  ].map((key) => input[key]).find(
    (value) => typeof value === "string" && value.length > 0,
  );
  return { label, target: typeof target === "string" ? target : null };
}

function submissionDetails(name: string, raw: string): string[] {
  if (!["agent_create", "agent_update", "skill_install"].includes(name)) return [];
  const input = parsedInput(raw);
  const details: string[] = [];
  if (typeof input.description === "string") details.push(`简介说明：${input.description || "清空"}`);
  if (name !== "agent_update") details.push(`所属范围：${input.scope_id ?? "个人范围"}`);
  if (name === "skill_install") {
    details.push("仅安装技能，不绑定任何智能体");
    details.push(`内容摘要：${input.expected_sha256 ?? "未提供"}`);
  } else {
    if (input.visibility != null || name === "agent_create") {
      details.push(`可见性：${input.visibility === "public" ? "公开" : "私有"}`);
    }
    for (const [key, label] of [["skill_binding_ids", "技能绑定"], ["mcp_binding_ids", "MCP 绑定"]]) {
      if (Array.isArray(input[key])) details.push(`${label}：${(input[key] as unknown[]).join("、") || "无（清空）"}`);
    }
    if (input.system_prompt != null) details.push("包含提示词变更，可展开详情核对全文");
    if (input.is_default != null) details.push(`默认智能体：${input.is_default ? "是" : "否"}`);
    if (input.include_descendants != null) details.push(`覆盖下级范围：${input.include_descendants ? "是" : "否"}`);
  }
  details.push("本次批准仅适用于当前提交内容；自动执行也需确认");
  return details;
}

export function ApprovalCard({
  pending,
  onConfirm,
  submitting = false,
  error,
  settings,
}: {
  pending: PendingConfirm;
  onConfirm: (decisions: Decision[]) => void;
  submitting?: boolean;
  error?: string | null;
  settings?: React.ReactNode;
}) {
  const [decisions, setDecisions] = useState<Record<string, boolean>>({});
  const toolCalls = pending.toolCalls ?? [];
  const isSingle = toolCalls.length === 1;
  const allSelected = toolCalls.every((call) => call.id in decisions);
  const profile: TaskApprovalProfile = pending.approvalProfile ?? "standard";
  const explanation = approvalRequestExplanation(
    profile,
    toolCalls.map((call) => call.name),
  );

  function submitAll(approved: boolean) {
    onConfirm(toolCalls.map((call) => ({
      tool_call_id: call.id,
      approved,
    })));
  }

  const singleName = toolCalls[0]?.name.toLowerCase();
  const approveLabel = singleName === "delete" ? "确认删除" : ["send_email", "email"].includes(singleName) ? "确认发送" : "允许本次";
  return (
    <section aria-label="待审批操作" aria-busy={submitting || undefined} className="max-w-[680px] rounded-xl border border-[var(--line)] bg-[var(--surface-subtle)] px-4 py-4 sm:px-5">
      <div className="mb-4 flex items-center gap-2 text-sm font-medium text-[var(--ink)]">
        <Icon name="shield" className="h-4 w-4 text-amber-600" />
        <h3>需要你确认</h3>
        {!isSingle && <span className="text-xs font-normal text-[var(--ink-faint)]">{toolCalls.length} 项操作</span>}
      </div>
      <div className="space-y-4">
        {toolCalls.map((call) => {
          const summary = actionSummary(call.name, call.input);
          return <article key={call.id} className="min-w-0 text-[var(--ink-soft)]">
            <p className="text-sm font-medium text-[var(--ink)]">{summary.label}</p>
            {summary.target && <p className="mt-1.5 whitespace-pre-wrap break-words text-xs leading-5 [overflow-wrap:anywhere]">目标：{summary.target}</p>}
            {submissionDetails(call.name, call.input).map((detail) => <p key={detail} className="mt-1 break-words text-xs leading-5">{detail}</p>)}
            <details className="mt-2 text-xs text-[var(--ink-faint)]">
              <summary className="cursor-pointer select-none py-1">查看操作详情</summary>
              <p className="mt-2">工具：{call.name || "未命名工具"}</p>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-[var(--line)] bg-[var(--surface)] p-3 font-mono text-xs leading-5">{call.input || "{}"}</pre>
              <p className="mt-2">本任务：{approvalProfileLabel(profile)}</p>
              <p className="mt-1 leading-5">{explanation}</p>
            </details>
            {!isSingle && <div className="mt-2 flex gap-2" role="group" aria-label={`${summary.label}的审批选择`}>
              <button type="button" aria-pressed={decisions[call.id] === true} disabled={submitting} onClick={() => setDecisions((current) => ({ ...current, [call.id]: true }))} className={`rounded-lg border px-3 py-1.5 text-xs disabled:opacity-40 ${decisions[call.id] === true ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]" : "border-[var(--line)]"}`}>允许</button>
              <button type="button" aria-pressed={decisions[call.id] === false} disabled={submitting} onClick={() => setDecisions((current) => ({ ...current, [call.id]: false }))} className={`rounded-lg border px-3 py-1.5 text-xs disabled:opacity-40 ${decisions[call.id] === false ? "border-[var(--ink-soft)] bg-[var(--surface)]" : "border-[var(--line)]"}`}>拒绝</button>
            </div>}
          </article>;
        })}
      </div>
      {error && <p role="alert" className="mt-3 text-xs leading-5 text-red-600">提交失败：{error}。选择已保留，请重试。</p>}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] pt-3">
        <span className="text-xs text-[var(--ink-faint)]">{submitting ? "正在提交决定…" : isSingle ? "仅对本次操作生效" : "逐项选择后统一提交"}</span>
        <div className="ml-auto flex gap-2">
          <button type="button" disabled={submitting || !toolCalls.length} onClick={() => submitAll(false)} className="rounded-lg px-3 py-2 text-xs text-[var(--ink-soft)] hover:bg-[var(--surface)] disabled:opacity-40">{isSingle ? "拒绝本次" : "拒绝当前全部"}</button>
          <button type="button" disabled={submitting || !toolCalls.length || (!isSingle && !allSelected)} onClick={() => isSingle ? submitAll(true) : onConfirm(toolCalls.map((call) => ({ tool_call_id: call.id, approved: decisions[call.id] })))} className="rounded-lg bg-[var(--ink)] px-4 py-2 text-xs font-medium text-[var(--surface)] disabled:opacity-40">{isSingle ? approveLabel : "提交选择"}</button>
        </div>
      </div>
      {settings && <div className="mt-2">{settings}</div>}
    </section>
  );
}
