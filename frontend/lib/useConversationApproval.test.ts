import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";
import { useConversationApproval } from "./useConversationApproval";

it("keeps the submitted choice despite stale global preferences and task updates", () => {
  localStorage.setItem("workmate_approval_profile", "full_access");
  const { result, rerender } = renderHook(({ profile }) => useConversationApproval("new-conversation", "standard", profile), { initialProps: { profile: "standard" as "standard" | "full_access" } });
  expect(result.current[0]).toBe("standard");
  act(() => result.current[1]("strict"));
  rerender({ profile: "full_access" });
  expect(result.current[0]).toBe("strict");
});

it("restores the conversation choice and waits for history without adopting another conversation", () => {
  localStorage.setItem("workmate_approval_profile:saved", "read_only");
  const { result, rerender } = renderHook(({ id, profile }) => useConversationApproval(id, undefined, profile), { initialProps: { id: "saved", profile: undefined as "strict" | undefined } });
  expect(result.current[0]).toBe("read_only");
  rerender({ id: "other", profile: undefined });
  expect(result.current[0]).toBe("standard");
  rerender({ id: "other", profile: "strict" });
  expect(result.current[0]).toBe("strict");
});

it('never inherits unattended as the next interactive task approval',()=>{
 const {result}=renderHook(()=>useConversationApproval('scheduled',undefined,'unattended'));
 expect(result.current[0]).toBe('standard');
});
