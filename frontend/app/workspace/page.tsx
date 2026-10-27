"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Composer } from "@/components/composer/Composer";
import { ModelSelector } from "@/components/composer/ModelSelector";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { useWorkspaceDraft } from "@/components/workspace/WorkspaceDraftProvider";
import { listModelProfiles } from "@/lib/api";
import type { ApprovalProfile } from "@/lib/types";

const suggestions = ["整理资料并总结", "分析数据与风险", "生成可交付成果"];

export default function WorkspaceHome() {
  const workspace = useWorkspaceDraft();
  const { draft, selectedAgent, sending, updateDraft } = workspace;
  const [confirmFullAccess, setConfirmFullAccess] = useState(false);
  const models = useQuery({ queryKey: ["models"], queryFn: listModelProfiles, staleTime: 5 * 60_000 });
  const profiles = models.data ?? [];
  const modelId = draft.modelProfile || profiles.find((model) => model.is_default && model.available !== false)?.id || profiles.find((model) => model.available !== false)?.id || "";
  const modelReady = profiles.some((model) => model.id === modelId && model.available !== false);
  const disabled = workspace.agentsLoading || workspace.agentsError || !selectedAgent || models.isLoading || !modelReady;
  function chooseApproval(profile: ApprovalProfile) {
    if (profile === "full_access") setConfirmFullAccess(true);
    else updateDraft({ approvalProfile: profile });
  }
  return <div className="flex h-full flex-col justify-center overflow-y-auto bg-[var(--surface)] pb-24 pt-8 lg:pb-10">
    <div className="mx-auto w-full max-w-[880px] px-5 pb-6 text-center">
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">今天，我能帮你做些什么？</h1>
      <p className="mt-4 text-sm text-[var(--ink-soft)]" aria-live="polite">{workspace.agentsLoading ? "正在准备智能体…" : selectedAgent ? `正在与「${selectedAgent.display_name}」对话` : workspace.usableAgents.length ? "所选智能体暂不可用，请在左侧选择其他智能体。" : "暂无可用智能体，请联系管理员开通访问权限。"}</p>
      {selectedAgent?.description && <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-[var(--ink-faint)]">{selectedAgent.description}</p>}
      {workspace.agentsError && <button type="button" onClick={() => workspace.retryAgents()} className="mt-3 text-sm text-[var(--danger)]">智能体加载失败，点击重试</button>}
      <div className="mt-8 flex flex-wrap justify-center gap-2">{suggestions.map((prompt) => <button key={prompt} type="button" disabled={disabled || sending} onClick={() => updateDraft({ input: prompt })} className="rounded-full border border-[var(--line)] px-4 py-2 text-xs text-[var(--ink-soft)] hover:bg-[var(--surface-subtle)] disabled:opacity-50">{prompt}</button>)}</div>
    </div>
    <Composer centered input={draft.input} onInputChange={(input) => updateDraft({ input })}
      attachments={draft.attachments} onAttachmentsChange={(attachments) => updateDraft({ attachments })}
      approvalProfile={draft.approvalProfile} onApprovalProfileChange={chooseApproval} runningTaskProfile={null}
      disabled={disabled} busy={false} sending={sending} error={workspace.error}
      notice={draft.attachments.length ? workspace.attachmentsNotice : null}
      placeholder="告诉我你想完成什么，或添加文件…"
      onSubmit={() => { if (!disabled) void workspace.submit(modelId); }}
      modelSelector={<ModelSelector selection={{ profiles, value: modelId, disabled: sending || models.isLoading || profiles.length === 0,
        hint: "选择本次对话使用的模型", loading: models.isLoading, pending: false, isSwitching: () => false,
        error: models.isError ? "模型列表加载失败" : !models.isLoading && !modelReady ? "请选择可用模型" : null,
        retry: () => models.refetch(), onChange: (modelProfile) => updateDraft({ modelProfile }),
      }} />}
    />
    {workspace.error && workspace.resumeConversationId && <Link href={`/workspace/c/${workspace.resumeConversationId}`} className="mx-auto mt-2 text-sm text-[var(--accent)] underline">打开已创建的对话</Link>}
    <Dialog open={confirmFullAccess} onClose={() => setConfirmFullAccess(false)} title="确认开启自动执行？"
      description="常规操作将自动执行；创建或修改智能体、安装技能等操作仍需确认，禁止操作仍会被拦截。"
      footer={<><Button variant="ghost" onClick={() => setConfirmFullAccess(false)}>取消</Button><Button variant="danger" onClick={() => { updateDraft({ approvalProfile: "full_access" }); setConfirmFullAccess(false); }}>确认选择自动执行</Button></>}>
      <p className="text-sm leading-6 text-[var(--ink-soft)]">仅在受支持的隔离环境中可用，智能体、企业和外部系统的权限限制仍然生效。该选择用于这份草稿的下一次发送。</p>
    </Dialog>
  </div>;
}
