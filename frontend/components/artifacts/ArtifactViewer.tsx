"use client";

import { useEffect, useMemo, useState } from "react";
import { parse as parseYaml, stringify as stringifyYaml } from "yaml";

import { MarkdownMessage } from "@/components/chat/MarkdownMessage";
import { Icon } from "@/components/ui/Icons";
import { downloadArtifact, fetchArtifactBlob } from "@/lib/api";
import { artifactRendererFor, canRenderAsText } from "@/lib/artifacts/registry";
import {
  MAX_TEXT_PREVIEW_BYTES,
  parseCsvPreview,
} from "@/lib/artifacts/preview";
import { dynamicHtmlDocument } from "@/lib/artifacts/html-preview";
import type { Artifact } from "@/lib/types";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formattedText(kind: string, content: string): string {
  try {
    if (kind === "json") return JSON.stringify(JSON.parse(content), null, 2);
    if (kind === "yaml") return stringifyYaml(parseYaml(content));
  } catch {
    // A malformed structured file is still safe and useful as plain text.
  }
  return content;
}

export function ArtifactViewer({
  artifact,
  versions = artifact ? [artifact] : [],
  onVersionChange,
}: {
  artifact: Artifact | null;
  versions?: Artifact[];
  onVersionChange?: (artifactId: string) => void;
}) {
  const [content, setContent] = useState("");
  const [htmlPreview, setHtmlPreview] = useState<{ document: string; blockedScripts: number } | null>(null);
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sourceMode, setSourceMode] = useState(false);
  const [fullScreen, setFullScreen] = useState(false);
  const [copied, setCopied] = useState(false);
  const renderer = artifact
    ? artifactRendererFor(artifact.name, artifact.mime_type)
    : "download";

  useEffect(() => {
    setContent("");
    setHtmlPreview(null);
    setObjectUrl(null);
    setError(null);
    setSourceMode(false);
    setLoading(false);
    if (!artifact || renderer === "download") return;
    if (canRenderAsText(renderer) && artifact.size > MAX_TEXT_PREVIEW_BYTES) {
      setError("文件超过 5 MB 前端预览上限，请下载后查看");
      return;
    }

    const controller = new AbortController();
    let createdUrl: string | null = null;
    setLoading(true);
    fetchArtifactBlob(artifact.id, controller.signal)
      .then(async (blob) => {
        if (controller.signal.aborted) return;
        if (canRenderAsText(renderer)) {
          if (blob.size > MAX_TEXT_PREVIEW_BYTES) throw new Error("文件超过 5 MB 前端预览上限，请下载后查看");
          const text = await blob.text();
          if (!controller.signal.aborted) setContent(text);
          if (renderer === "html" && !controller.signal.aborted) {
            const preview = await dynamicHtmlDocument(text, controller.signal);
            if (!controller.signal.aborted) setHtmlPreview(preview);
          }
        } else {
          createdUrl = URL.createObjectURL(blob);
          setObjectUrl(createdUrl);
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "预览加载失败");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [artifact, renderer]);

  const csv = useMemo(
    () => renderer === "csv" ? parseCsvPreview(content) : null,
    [content, renderer],
  );

  if (!artifact) {
    return (
      <div className="grid flex-1 place-items-center p-8 text-center text-[11px] text-neutral-400">
        <div><Icon name="eye" className="mx-auto mb-3 h-6 w-6" />选择一个成果进行预览</div>
      </div>
    );
  }

  const showingHtmlSource = renderer === "html" && sourceMode && Boolean(content);

  const body = (
    <div className="flex min-h-0 flex-1 flex-col bg-white dark:bg-[#121316]">
      <div className="flex min-h-12 items-center gap-2 border-b border-neutral-100 px-3 dark:border-neutral-800">
        <div className="min-w-0 flex-1">
          <p className="truncate text-[11px] font-medium" title={artifact.name}>{artifact.name}</p>
          <p className="text-[9px] text-neutral-400">v{artifact.version} · {formatSize(artifact.size)}</p>
        </div>
        {versions.length > 1 && onVersionChange && (
          <select
            aria-label="成果版本"
            value={artifact.id}
            onChange={(event) => onVersionChange(event.target.value)}
            className="h-8 rounded-lg border border-[var(--line)] bg-[var(--surface-raised)] px-2 text-[10px] font-medium text-[var(--ink-soft)] outline-none"
          >
            {versions.map((version) => <option key={version.id} value={version.id}>v{version.version}{version.id === versions[0]?.id ? " · 最新" : ""}</option>)}
          </select>
        )}
        {["markdown", "html"].includes(renderer) && (
          <button type="button" className="rounded-lg px-2 py-1 text-[9px] text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800" onClick={() => setSourceMode((value) => !value)}>
            {sourceMode ? "预览" : "源码"}
          </button>
        )}
        {canRenderAsText(renderer) && content && <button type="button" className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-[9px] text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800" onClick={async () => { await navigator.clipboard.writeText(content); setCopied(true); window.setTimeout(() => setCopied(false), 1500); }}><Icon name="copy" className="h-3 w-3" />{copied ? "已复制" : "复制"}</button>}
        <button type="button" aria-label="全屏预览" className="grid h-8 w-8 place-items-center rounded-lg text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800" onClick={() => setFullScreen((value) => !value)}><Icon name="expand" className="h-3.5 w-3.5" /></button>
        <button type="button" aria-label={`下载 ${artifact.name}`} className="grid h-8 w-8 place-items-center rounded-lg text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800" onClick={() => downloadArtifact(artifact.id, artifact.name)}><Icon name="download" className="h-3.5 w-3.5" /></button>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {loading && !showingHtmlSource && <div className="space-y-3 p-5" aria-label="正在加载预览"><div className="h-4 w-2/3 animate-pulse rounded bg-neutral-100 dark:bg-neutral-800" /><div className="h-32 animate-pulse rounded-xl bg-neutral-100 dark:bg-neutral-800" /></div>}
        {!loading && error && !showingHtmlSource && (
          <div className="m-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-[11px] text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            <p>{error}</p>
            <button type="button" className="mt-3 rounded-lg bg-white px-3 py-2 font-medium text-neutral-700 shadow-sm dark:bg-neutral-900 dark:text-neutral-200" onClick={() => downloadArtifact(artifact.id, artifact.name)}>下载文件</button>
          </div>
        )}
        {!loading && !error && renderer === "download" && (
          <div className="grid min-h-64 place-items-center p-8 text-center">
            <div><Icon name="file" className="mx-auto mb-3 h-8 w-8 text-neutral-300" /><p className="text-[12px] font-medium">此类型仅支持安全下载</p><p className="mt-1 text-[10px] text-neutral-400">不会在浏览器中执行或渲染主动内容</p><button type="button" className="mt-4 rounded-xl bg-neutral-900 px-4 py-2 text-[11px] font-medium text-white dark:bg-white dark:text-neutral-900" onClick={() => downloadArtifact(artifact.id, artifact.name)}>下载文件</button></div>
          </div>
        )}
        {renderer === "html" && (showingHtmlSource || (!loading && !error)) && (
          sourceMode ? <pre className="min-h-full whitespace-pre-wrap break-words p-5 font-mono text-[11px] leading-6">{content}</pre> :
            <div className="flex h-full min-h-[560px] flex-col">
              <p className="border-b border-neutral-100 px-3 py-2 text-[10px] text-neutral-500">交互预览 · ECharts / Chart.js</p>
              {!!htmlPreview?.blockedScripts && <p className="px-3 py-2 text-[10px] text-amber-700">{htmlPreview.blockedScripts} 个外部脚本未加载，仅支持内嵌脚本和内置图表库。</p>}
              <iframe key={artifact.id} title={`${artifact.name} 交互预览`} sandbox="allow-scripts" referrerPolicy="no-referrer"
                srcDoc={htmlPreview?.document ?? ""} className="min-h-[520px] w-full flex-1 border-0 bg-white" />
            </div>
        )}
        {!loading && !error && renderer === "markdown" && (
          <div className="p-5">{sourceMode ? <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-6">{content}</pre> : <MarkdownMessage content={content} />}</div>
        )}
        {!loading && !error && ["text", "code"].includes(renderer) && (
          <pre className="min-h-full whitespace-pre-wrap break-words p-5 font-mono text-[11px] leading-6">{formattedText(renderer, content)}</pre>
        )}
        {!loading && !error && ["json", "yaml"].includes(renderer) && (
          <details open className="m-4 rounded-xl border border-neutral-200 dark:border-neutral-700"><summary className="cursor-pointer bg-neutral-50 px-3 py-2 text-[10px] font-medium dark:bg-neutral-800">格式化内容</summary><pre className="max-h-[calc(100vh-12rem)] overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-[11px] leading-6">{formattedText(renderer, content)}</pre></details>
        )}
        {!loading && !error && renderer === "csv" && csv && (
          <div className="p-4">
            <div className="overflow-auto rounded-xl border border-neutral-200 dark:border-neutral-700"><table className="w-full min-w-[460px] border-collapse text-left text-[10px]"><tbody>{csv.rows.map((row, rowIndex) => <tr key={rowIndex} className={rowIndex === 0 ? "bg-neutral-50 font-semibold dark:bg-neutral-800" : ""}>{row.map((cell, cellIndex) => <td key={cellIndex} className="border-b border-r border-neutral-100 px-3 py-2 last:border-r-0 dark:border-neutral-800">{cell}</td>)}</tr>)}</tbody></table></div>
            {csv.truncated && <p className="mt-2 text-[9px] text-neutral-400">仅显示前 200 行，下载文件可查看完整内容。</p>}
          </div>
        )}
        {!loading && !error && renderer === "image" && objectUrl && <div className="grid min-h-full place-items-center bg-neutral-50 p-4 dark:bg-neutral-950"><img src={objectUrl} alt={artifact.name} className="max-h-full max-w-full rounded-lg object-contain shadow-sm" /></div>}
        {!loading && !error && renderer === "pdf" && objectUrl && <iframe src={objectUrl} title={`${artifact.name} 预览`} className="h-full min-h-[560px] w-full border-0" />}
      </div>
    </div>
  );

  if (fullScreen) {
    return <div className="fixed inset-3 z-[80] flex overflow-hidden rounded-2xl border border-neutral-200 bg-white shadow-2xl dark:border-neutral-700 dark:bg-[#121316] sm:inset-8">{body}</div>;
  }
  return body;
}
