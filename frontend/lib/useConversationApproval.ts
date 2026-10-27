"use client";

import { useEffect, useState } from "react";
import { APPROVAL_PROFILES, DEFAULT_APPROVAL_PROFILE } from "./approvalProfiles";
import type { ApprovalProfile, TaskApprovalProfile } from "./types";

/** Initialize once per conversation; later Task snapshots must not replace a user's choice. */
export function useConversationApproval(conversationId: string, submitted?: ApprovalProfile, latest?: TaskApprovalProfile) {
  const interactiveLatest = latest === "unattended" ? DEFAULT_APPROVAL_PROFILE : latest;
  const [choice, setChoice] = useState<{ id: string; profile: ApprovalProfile } | null>(null);
  const storageKey = `workmate_approval_profile:${conversationId}`;
  useEffect(() => {
    if (choice?.id === conversationId) return;
    let saved: string | null = null;
    try { saved = localStorage.getItem(storageKey); } catch { /* In-memory choices remain available. */ }
    const valid = APPROVAL_PROFILES.find((profile) => profile.value === saved)?.value;
    const profile = valid ?? submitted ?? interactiveLatest;
    if (profile) setChoice({ id: conversationId, profile });
  }, [choice?.id, conversationId, storageKey, submitted, interactiveLatest]);
  const select = (profile: ApprovalProfile) => {
    setChoice({ id: conversationId, profile });
    try { localStorage.setItem(storageKey, profile); } catch { /* Keep the explicit in-memory choice. */ }
  };
  return [choice?.id === conversationId ? choice.profile : submitted ?? interactiveLatest ?? DEFAULT_APPROVAL_PROFILE, select] as const;
}
