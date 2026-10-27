"use client";
import { resourceList } from "@/lib/resourceList";

import { createContext, useContext, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { DEFAULT_APPROVAL_PROFILE } from "@/lib/approvalProfiles";
import type { Agent, ApprovalProfile, Conversation, Task } from "@/lib/types";

export interface WorkspaceDraft {
  input: string;
  attachments: File[];
  approvalProfile: ApprovalProfile;
  modelProfile: string;
}
export interface PendingSubmission {
  token: string;
  draftKey: string;
  agent: Agent;
  draft: WorkspaceDraft;
  status: "sending" | "failed" | "sent";
  error: string | null;
  conversationId?: string;
  task?: Task;
}
const emptyDraft = (): WorkspaceDraft => ({ input: "", attachments: [], approvalProfile: DEFAULT_APPROVAL_PROFILE, modelProfile: "" });
function useDraftState() {
  const router = useRouter();
  const client = useQueryClient();
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => resourceList<Agent>("/agents") });
  const me = useQuery({ queryKey: ["auth", "me"], queryFn: () => api.me(), staleTime: 30_000, retry: false });
  const [agentId, setAgentId] = useState("");
  const [, render] = useState(0);
  const drafts = useRef(new Map<string, WorkspaceDraft>());
  const conversations = useRef(new Map<string, Conversation>());
  const conversationFiles = useRef(new Map<string, File[]>());
  const lock = useRef(false);
  const [sending, setSending] = useState(false);
  const [pendingSubmission, setPendingSubmission] = useState<PendingSubmission | null>(null);
  const [error, setError] = useState<string | null>(null);
  const usableAgents = (agents.data ?? []).filter((agent) => agent.actions.use);
  const selectedAgent = agentId ? usableAgents.find((agent) => agent.id === agentId) : usableAgents.find((agent) => agent.is_default) ?? usableAgents[0];
  useEffect(() => { if (!agentId && selectedAgent) setAgentId(selectedAgent.id); }, [agentId, selectedAgent]);
  const key = me.data?.id && selectedAgent ? `workmate_workspace_draft:${me.data.id}:${selectedAgent.id}` : null;
  function readDraft(draftKey: string): WorkspaceDraft {
    const existing = drafts.current.get(draftKey);
    if (existing) return existing;
    const initial = emptyDraft();
    try { initial.input = window.localStorage.getItem(draftKey) ?? ""; } catch { /* Storage may be unavailable. */ }
    drafts.current.set(draftKey, initial);
    return initial;
  }
  const draft = key ? readDraft(key) : emptyDraft();
  function saveText(draftKey: string, input: string) {
    try { if (input) window.localStorage.setItem(draftKey, input); else window.localStorage.removeItem(draftKey); } catch { /* In-memory draft remains usable. */ }
  }
  function updateDraft(partial: Partial<WorkspaceDraft>) {
    if (lock.current || !key) return;
    setAgentId(selectedAgent!.id);
    const next = { ...readDraft(key), ...partial };
    drafts.current.set(key, next);
    if (partial.input !== undefined) saveText(key, next.input);
    render((version) => version + 1);
  }
  async function runSubmission(submission: PendingSubmission) {
    if (lock.current || !me.data?.id || !submission.draftKey.startsWith(`workmate_workspace_draft:${me.data.id}:`)) return;
    if (!usableAgents.some((agent) => agent.id === submission.agent.id)) {
      setPendingSubmission({ ...submission, status: "failed", error: "该智能体暂不可用，请返回选择其他智能体。" });
      return;
    }
    const key = submission.draftKey;
    const snapshot = submission.draft;
    const modelProfile = snapshot.modelProfile;
    lock.current = true;
    setSending(true);
    setError(null);
    setPendingSubmission({ ...submission, status: "sending", error: null });
    try {
      let conversation = conversations.current.get(key);
      if (!conversation) {
        conversation = await api.post<Conversation>("/conversations", { agent_id: submission.agent.id, title: snapshot.input.trim().slice(0, 80) || "文件对话", model_profile: modelProfile || null });
        conversations.current.set(key, conversation);
      } else {
        const existingTasks = await api.get<Task[]>(`/tasks?conversation_id=${conversation.id}&limit=1`);
        if (existingTasks.length) throw new Error("该对话已有执行记录，请从历史对话打开后继续。当前草稿已保留，未重复发送。");
        if ((conversation.model_profile ?? "") !== modelProfile) {
          conversation = await api.patch<Conversation>(`/conversations/${conversation.id}/model`, { model_profile: modelProfile || null });
          conversations.current.set(key, conversation);
        }
      }
      let result: { task: Task } | undefined;
      if (snapshot.attachments.length) {
        const form = new FormData();
        form.append("message", snapshot.input);
        form.append("approval_profile", snapshot.approvalProfile);
        snapshot.attachments.forEach((file) => form.append("files", file, file.name));
        result = await api.upload<{ task: Task }>(`/tasks/conversations/${conversation.id}/tasks-with-files`, form);
      } else {
        result = await api.post<{ task: Task }>(`/tasks/conversations/${conversation.id}/tasks`, { message: snapshot.input, approval_profile: snapshot.approvalProfile });
      }
      client.setQueryData(["conversation", conversation.id], conversation);
      if (drafts.current.get(key) === snapshot) {
        drafts.current.set(key, emptyDraft());
        saveText(key, "");
      }
      conversations.current.delete(key);
      if (result?.task) client.setQueryData(["tasks", conversation.id], [result.task]);
      setPendingSubmission({ ...submission, status: "sent", error: null, conversationId: conversation.id, task: result?.task });
      void client.invalidateQueries({ queryKey: ["conversations"] });
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "发送失败，请重试";
      setError(message);
      setPendingSubmission({ ...submission, status: "failed", error: message, conversationId: conversations.current.get(key)?.id });
    } finally {
      lock.current = false;
      setSending(false);
    }
  }
  async function submit(modelProfile: string) {
    if (lock.current || !key || !selectedAgent) return;
    const snapshot = { ...readDraft(key), attachments: [...readDraft(key).attachments], modelProfile };
    if (!snapshot.input.trim() && !snapshot.attachments.length) return;
    const token = Array.from(crypto.getRandomValues(new Uint8Array(12)), (value) => value.toString(16).padStart(2, "0")).join("");
    setAgentId(selectedAgent.id);
    drafts.current.set(key, snapshot);
    const submission: PendingSubmission = { token, draftKey: key, agent: selectedAgent, draft: snapshot, status: "sending", error: null };
    setPendingSubmission(submission);
    router.push(`/workspace/pending/${token}`);
    await runSubmission(submission);
  }
  return {
    usableAgents, selectedAgent, draft, updateDraft, submit, sending, error,
    pendingSubmission: me.data?.id && pendingSubmission?.draftKey.startsWith(`workmate_workspace_draft:${me.data.id}:`) ? pendingSubmission : null,
    retrySubmission: (token: string) => pendingSubmission?.token === token && pendingSubmission.status === "failed" ? runSubmission(pendingSubmission) : Promise.resolve(),
    resumeConversationId: key ? conversations.current.get(key)?.id ?? null : null,
    selectAgent: (id: string) => { if (!lock.current && usableAgents.some((agent) => agent.id === id)) { setAgentId(id); setError(null); } },
    agentsLoading: agents.isLoading || me.isLoading,
    agentsError: agents.isError || me.isError,
    retryAgents: () => Promise.all([agents.refetch(), me.refetch()]),
    getConversationFiles: (id: string): File[] => me.data?.id ? conversationFiles.current.get(`${me.data.id}:${id}`) ?? [] : [],
    setConversationFiles: (id: string, files: File[]) => {
      if (!me.data?.id) return;
      const fileKey = `${me.data.id}:${id}`;
      if (files.length) conversationFiles.current.set(fileKey, [...files]);
      else conversationFiles.current.delete(fileKey);
    },
    attachmentsNotice: "附件仅保留在当前页面会话中，刷新后需重新添加。",
  };
}
const WorkspaceDraftContext = createContext<ReturnType<typeof useDraftState> | null>(null);
export function WorkspaceDraftProvider({ children }: { children: React.ReactNode }) {
  return <WorkspaceDraftContext.Provider value={useDraftState()}>{children}</WorkspaceDraftContext.Provider>;
}
export function useWorkspaceDraft() {
  const value = useContext(WorkspaceDraftContext);
  if (!value) throw new Error("useWorkspaceDraft requires WorkspaceDraftProvider");
  return value;
}
