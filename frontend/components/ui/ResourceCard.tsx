import type { ReactNode } from "react";
import { Icon, type IconName } from "./Icons";

export const resourceGridClass = "grid gap-4 md:grid-cols-2 lg:grid-cols-3";
export const resourceDrawerWidth = "max-w-[760px]";

export function ResourceCard({ title, description, icon, badge, metadata, onClick }: {
  title: string;
  description: string;
  icon: IconName;
  badge?: ReactNode;
  metadata: string;
  onClick: () => void;
}) {
  return <button type="button" onClick={onClick} className="group flex h-60 min-w-0 flex-col rounded-2xl border border-[var(--line)] bg-[var(--surface-raised)] p-5 text-left transition hover:border-[var(--ink-faint)] hover:shadow-sm">
    <div className="mb-4 flex w-full min-w-0 items-center gap-3">
      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-[var(--surface-subtle)]"><Icon name={icon} className="h-5 w-5 text-[var(--ink-soft)]" /></span>
      <h2 className="min-w-0 flex-1 truncate text-base font-semibold" title={title}>{title}</h2>
      {badge}
    </div>
    <p className="line-clamp-3 break-words text-sm leading-6 text-[var(--ink-soft)]">{description}</p>
    <div className="mt-auto flex w-full min-w-0 items-center gap-2 pt-4 text-xs text-[var(--ink-faint)]">
      <span className="min-w-0 flex-1 truncate" title={metadata}>{metadata}</span>
      <span className="shrink-0 group-hover:text-[var(--ink)]">查看详情 →</span>
    </div>
  </button>;
}
