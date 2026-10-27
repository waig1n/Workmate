"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { cx } from "@/components/ui/Button";

export const resourceDestinations = [
  { href: "/workspace/agents", label: "智能体" },
  { href: "/workspace/knowledge-bases", label: "知识库" },
  { href: "/workspace/skills", label: "技能" },
  { href: "/workspace/mcps", label: "连接器" },
];
const storageKey = "workmate:resource-center:last-tab";
export const matchesDestination = (pathname: string, href: string) => pathname === href || pathname.startsWith(`${href}/`);

export function useResourceDestination(pathname: string) {
  const active = resourceDestinations.find(item => matchesDestination(pathname, item.href));
  const [lastHref, setLastHref] = useState(resourceDestinations[0].href);
  useEffect(() => {
    try {
      if (active) {
        setLastHref(active.href);
        localStorage.setItem(storageKey, active.href);
      } else {
        const stored = localStorage.getItem(storageKey);
        if (resourceDestinations.some(item => item.href === stored)) setLastHref(stored!);
      }
    } catch { /* Navigation remains available when browser storage is disabled. */ }
  }, [active]);
  return { active, href: active?.href ?? lastHref };
}

export function ResourceNavigation({ pathname }: { pathname: string }) {
  return <header className="shrink-0 border-b border-[var(--line)] px-4 pt-4 sm:px-6">
    <h1 className="text-sm font-semibold text-[var(--ink)]">资源中心</h1>
    <nav aria-label="资源中心分类" className="mt-2 flex gap-5 overflow-x-auto">
      {resourceDestinations.map(item => {
        const active = matchesDestination(pathname, item.href);
        return <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined}
          className={cx("shrink-0 border-b-2 px-1 py-3 text-sm transition-colors", active ? "border-[var(--accent)] font-medium text-[var(--accent-strong)]" : "border-transparent text-[var(--ink-soft)] hover:text-[var(--ink)]")}>{item.label}</Link>;
      })}
    </nav>
  </header>;
}
