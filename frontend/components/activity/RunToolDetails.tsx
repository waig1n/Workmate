"use client";

import { useState } from "react";

import { ToolCallDetail } from "@/components/chat/ToolCallDetail";
import type { ChatToolMsg } from "@/lib/chat/model";
import {
  classifyTool,
  type ToolCategory,
} from "@/lib/chat/toolCategories";

const CATEGORY_TITLES: Record<ToolCategory, string> = {
  read: "文件读取",
  command: "命令执行",
  write: "文件修改",
  skill: "Skill 调用",
  mcp: "连接器调用",
  other: "其他操作",
};

const ORDER: ToolCategory[] = ["read", "command", "write", "skill", "mcp", "other"];

/**
 * Live tool-call details for the Activity side panel: grouped by
 * category, collapsed by default. Bash rows expose a compact command
 * preview while their complete arguments and output remain one click away.
 */
export function RunToolDetails({ tools }: { tools: ChatToolMsg[] }) {
  const [open, setOpen] = useState(false);
  if (!tools.length) return null;

  const groups = new Map<ToolCategory, ChatToolMsg[]>();
  for (const item of tools) {
    const category = classifyTool(item.call.toolCallName);
    const list = groups.get(category) ?? [];
    list.push(item);
    groups.set(category, list);
  }

  return (
    <section className="border-b border-neutral-100 dark:border-neutral-800">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-[10px] font-medium text-neutral-600 hover:bg-neutral-50 dark:text-neutral-300 dark:hover:bg-neutral-800/50"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span>本次执行的工具调用</span>
        <span className="text-neutral-400">{tools.length}</span>
        <span className="ml-auto text-neutral-400">{open ? "收起" : "展开"}</span>
      </button>
      {open && (
        <div className="max-h-[45%] space-y-3 overflow-y-auto px-4 pb-3">
          {ORDER.filter((category) => groups.has(category)).map((category) => (
            <div key={category}>
              <p className="mb-1 text-[9px] font-medium uppercase tracking-wide text-neutral-400">
                {CATEGORY_TITLES[category]} · {groups.get(category)!.length}
              </p>
              <div className="space-y-1">
                {groups.get(category)!.map((item) => (
                  <ToolCallDetail key={item.id} item={item} />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
