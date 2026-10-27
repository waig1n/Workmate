"use client";

import { useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, listModelProfiles } from "./api";
import type { Conversation } from "./types";

export function useConversationModel(conversation: Conversation | undefined, busy: boolean) {
  const client = useQueryClient();
  const switchingRef = useRef(false);
  const models = useQuery({ queryKey: ["models"], queryFn: listModelProfiles, staleTime: 5 * 60_000 });
  const change = useMutation({
    mutationFn: (model: string) => api.patch<Conversation>(`/conversations/${conversation?.id}/model`, { model_profile: model }),
    onSettled: () => { switchingRef.current = false; },
    onSuccess: async (updated) => {
      client.setQueryData(["conversation", updated.id], updated);
      await client.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
  const profiles = models.data ?? [];
  const value = conversation?.model_profile ?? profiles.find((profile) => profile.is_default)?.id ?? "";
  const disabled = !conversation || conversation.read_only || busy || profiles.length < 2 || change.isPending;
  const hint = conversation?.read_only ? "会话为只读，无法切换模型"
    : busy ? "任务运行、排队或发送中，暂不可切换模型"
    : change.isPending ? "正在切换模型，请稍候"
    : profiles.length < 2 ? "当前只有一个可用模型" : "选择后续对话使用的模型";
  return {
    profiles, value, disabled, hint,
    loading: models.isLoading,
    pending: change.isPending,
    isSwitching: () => switchingRef.current,
    error: models.isError ? "模型列表加载失败" : change.isError ? (change.error as Error).message : null,
    retry: () => models.refetch(),
    onChange: (model: string) => {
      if (disabled || switchingRef.current || model === value || !profiles.some((profile) => profile.id === model && profile.available !== false)) return;
      switchingRef.current = true;
      change.mutate(model);
    },
  };
}
