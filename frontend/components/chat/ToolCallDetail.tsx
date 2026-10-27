"use client";

import { useMemo, useState } from "react";

import { getToken } from "@/lib/api";
import type { ChatToolMsg } from "@/lib/chat/model";

const STATE_LABEL: Record<string, string> = {
  running: "执行中",
  success: "成功",
  error: "失败",
  interrupted: "已中断",
  unknown: "结果未知",
};

/**
 * Detailed view of one tool call (args + result). Used by the inline
 * failure rows in the message stream and by the Activity side panel;
 * plain successful calls never render in the main stream anymore.
 */
export function ToolCallDetail({
  item,
  defaultOpen = false,
}: {
  item: ChatToolMsg;
  defaultOpen?: boolean;
}) {
  const call = item.call;
  const [open, setOpen] = useState(defaultOpen);

  const [fullArgs, setFullArgs] = useState(false);
  const [fullResult, setFullResult] = useState(false);
  const [fullMetadata, setFullMetadata] = useState(false);
  const [loaded, setLoaded] = useState<Partial<Record<"arguments" | "result" | "metadata", { sha256: string; text: string }>>>({});
  const [loading, setLoading] = useState<"arguments" | "result" | "metadata" | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const showFull = async (kind: "arguments" | "result" | "metadata") => {
    const setter = kind === "arguments" ? setFullArgs : kind === "result" ? setFullResult : setFullMetadata;
    if (kind === "arguments" ? fullArgs : kind === "result" ? fullResult : fullMetadata) { setter(false); return; }
    const ref = item.content?.[kind];
    if (!ref) { setter(true); return; }
    if (loaded[kind]?.sha256 === ref.sha256) { setter(true); return; }
    if (!item.taskId) { setLoadError("暂时无法定位完整内容，请刷新后重试。"); return; }
    setLoading(kind); setLoadError(null);
    try {
      const token = getToken();
      const response = await fetch(`/api/tasks/${encodeURIComponent(item.taskId)}/tools/${encodeURIComponent(call.toolCallId)}/content?kind=${kind}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!response.ok) throw new Error("unavailable");
      const text = await response.text();
      setLoaded(previous => ({ ...previous, [kind]: { sha256: ref.sha256, text } }));
      setter(true);
    } catch { setLoadError("完整内容暂时无法读取，请重试。"); }
    finally { setLoading(null); }
  };
  const parsed = useMemo(() => {
    try {
      const value: unknown = JSON.parse(call.args);
      return value && typeof value === "object" && !Array.isArray(value)
        ? value as Record<string, unknown> : null;
    } catch { return null; }
  }, [call.args]);
  const fileTool = /^(write|edit)$/i.test(call.toolCallName);
  const filePath = fileTool && typeof parsed?.file_path === "string" ? parsed.file_path : null;
  const command = /^(bash|shell|exec|execute_command)$/i.test(call.toolCallName)
    ? parsed?.command ?? parsed?.cmd ?? parsed?.script : null;
  const commandPreview = typeof command === "string" ? command.trim().replace(/\s+/g, " ").slice(0, 240) : null;
  // Do not pretty-print large inline payloads until the user requests them.
  const preview = (value: string) => value.slice(0, 2000).split("\n").slice(0, 20).join("\n");
  const fileContent = parsed?.content;
  const editBefore = parsed?.old_string;
  const editAfter = parsed?.new_string;
  const argsView = fullArgs && item.content?.arguments ? (loaded.arguments?.sha256 === item.content.arguments.sha256 ? loaded.arguments.text : item.content.arguments.preview) : fullArgs ? (parsed ? JSON.stringify(parsed, null, 2) : call.args)
    : filePath && typeof fileContent === "string" ? preview(fileContent)
    : filePath && typeof editBefore === "string" && typeof editAfter === "string"
      ? `修改前：\n${preview(editBefore)}\n修改后：\n${preview(editAfter)}`
      : preview(call.args);
  const resultView = fullResult && item.content?.result ? (loaded.result?.sha256 === item.content.result.sha256 ? loaded.result.text : item.content.result.preview) : fullResult ? call.result : preview(call.result);

  const stateTone =
    call.state === "error"
      ? "text-red-500"
      : call.state === "running"
        ? "text-blue-500"
        : "text-neutral-400";

  return (
    <div className="min-w-0">
      <button
        type="button"
        className="flex w-full items-center gap-2 py-1 text-left text-[11px] hover:text-neutral-700 dark:hover:text-neutral-300"
        onClick={() => setOpen(!open)}
      >
        <span className="font-medium">{call.toolCallName || "工具调用"}</span>
        <span className={`shrink-0 text-[10px] ${stateTone}`}>
          {STATE_LABEL[call.state] ?? call.state}
        </span>
        {commandPreview && (
          <code
            className="min-w-0 truncate rounded bg-neutral-100 px-1.5 py-0.5 text-[9px] text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400"
            title={commandPreview}
          >
            {commandPreview}
          </code>
        )}
        <span className="ml-auto shrink-0 text-[10px] text-neutral-400">
          {open ? "收起" : "展开"}
        </span>
      </button>
      {open && (
        <div className="py-1.5 pl-2">
          {loading && <p role="status" className="text-xs">正在读取完整内容…</p>}
          {loadError && <p role="alert" className="text-xs text-red-500">{loadError}</p>}
          {(item.content?.arguments?.incomplete || item.content?.result?.incomplete || item.content?.metadata?.incomplete) && <p role="status" className="text-xs">工具内容不完整</p>}
          <p className="mb-1 text-[10px] font-medium text-neutral-500">{filePath ? "文件内容预览" : "参数预览"}</p>
          {filePath && <p className="mb-2 break-all text-xs">{filePath}</p>}
          {filePath && typeof fileContent === "string" && <p className="mb-2 text-xs text-neutral-500">{new TextEncoder().encode(fileContent).length.toLocaleString()} 字节</p>}
          <pre className="mb-3 max-h-40 overflow-auto rounded-lg bg-neutral-50 p-3 text-[10px] leading-5 ring-1 ring-neutral-200/70 dark:bg-neutral-900 dark:ring-neutral-800">
            {argsView || "(空)"}
          </pre>
          {(call.args || item.content?.arguments) && <button type="button" className="mb-3 text-xs text-[var(--accent)]" disabled={loading !== null} onClick={() => void showFull("arguments")}>{fullArgs ? "收起完整参数" : "查看完整参数"}</button>}
          <p className="mb-1 text-[10px] font-medium text-neutral-500">结果</p>
          <pre className="max-h-60 overflow-auto rounded-lg bg-neutral-50 p-3 text-[10px] leading-5 ring-1 ring-neutral-200/70 dark:bg-neutral-900 dark:ring-neutral-800">
            {resultView || (call.state === "running" ? "(等待结果…)" : call.state === "unknown" ? "未收到工具结束记录，无法确认操作结果。" : "(无结果内容)")}
          </pre>
          {(item.content?.result || preview(call.result) !== call.result) && <button type="button" className="mt-2 text-xs text-[var(--accent)]" disabled={loading !== null} onClick={() => void showFull("result")}>{fullResult ? "收起完整结果" : "查看完整结果"}</button>}
          {item.content?.metadata && <>
            <p className="mb-1 mt-3 text-[10px] font-medium text-neutral-500">修改详情</p>
            <pre className="max-h-60 overflow-auto rounded-lg bg-neutral-50 p-3 text-[10px] leading-5 ring-1 ring-neutral-200/70 dark:bg-neutral-900 dark:ring-neutral-800">
              {fullMetadata && loaded.metadata?.sha256 === item.content.metadata.sha256 ? loaded.metadata.text : preview(item.content.metadata.preview)}
            </pre>
            <button type="button" className="mt-2 text-xs text-[var(--accent)]" disabled={loading !== null} onClick={() => void showFull("metadata")}>{fullMetadata ? "收起完整修改详情" : "查看完整修改详情"}</button>
          </>}
        </div>
      )}
    </div>
  );
}
