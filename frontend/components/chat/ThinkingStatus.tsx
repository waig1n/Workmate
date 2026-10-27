import { Icon } from "@/components/ui/Icons";
export function ThinkingStatus() {
  return <div role="status" className="flex items-center gap-3 py-3 text-sm text-[var(--ink-soft)]"><Icon name="spark" className="h-5 w-5 text-[var(--accent)]" /><span>工作中…</span><span aria-hidden="true" className="run-thinking-dots"><i /><i /><i /></span></div>;
}
