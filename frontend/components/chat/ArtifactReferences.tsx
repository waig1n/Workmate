"use client";

import { useState } from "react";
import { Icon } from "@/components/ui/Icons";
import { downloadArtifact } from "@/lib/api";
import { groupArtifactVersions } from "@/lib/artifacts/versions";
import type { Artifact } from "@/lib/types";

function fileDescription(artifact: Artifact) {
  const extension = artifact.name.split(".").pop()?.toLowerCase() ?? "";
  const names: Record<string, string> = { pptx: "PowerPoint", ppt: "PowerPoint", docx: "Word", xlsx: "Excel", pdf: "PDF", md: "Markdown", csv: "CSV", txt: "文本", png: "图片", jpg: "图片", jpeg: "图片" };
  const bytes = artifact.size;
  const size = bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${names[extension] ?? (extension.toUpperCase() || "文件")} · ${size}`;
}

function FileAttachment({ artifact, showVersion, onOpen }: { artifact: Artifact; showVersion: boolean; onOpen: (id: string) => void }) {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function download() {
    if (downloading) return;
    setDownloading(true);
    setError(null);
    try { await downloadArtifact(artifact.id, artifact.name); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "下载失败，请重试"); }
    finally { setDownloading(false); }
  }
  return <div>
    <div className="flex items-center rounded-xl border border-[var(--line)] bg-[var(--surface-subtle)] transition-colors hover:border-[var(--line-strong)]">
      <button type="button" aria-label={`打开成果 ${artifact.name}`} onClick={() => onOpen(artifact.id)} className="flex min-w-0 flex-1 items-center gap-3 rounded-xl px-3 py-3 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)] sm:px-4">
        <Icon name="file" className="h-6 w-6 shrink-0 text-[var(--ink-faint)]" />
        <span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium text-[var(--ink)]" title={artifact.name}>{artifact.name}</span><span className="mt-1 block text-xs text-[var(--ink-faint)]">{fileDescription(artifact)}{showVersion ? ` · v${artifact.version}` : ""}</span></span>
        <span className="hidden shrink-0 text-xs text-[var(--ink-soft)] sm:inline">打开</span>
      </button>
      <button type="button" aria-label={`下载 ${artifact.name}`} title={downloading ? "正在下载…" : "下载文件"} disabled={downloading} onClick={() => void download()} className="mr-2 grid h-10 w-10 shrink-0 place-items-center rounded-lg text-[var(--ink-soft)] hover:bg-[var(--surface)] disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"><Icon name="download" className="h-4 w-4" /></button>
    </div>
    {error && <p role="alert" className="mt-1 px-3 text-xs text-red-500">{error}，请重试下载。</p>}
  </div>;
}

export function ArtifactReferences({ artifacts, onOpen }: { artifacts: Artifact[]; onOpen: (artifactId: string) => void }) {
  const series = groupArtifactVersions(artifacts);
  if (!series.length) return null;
  return <div className="ml-[52px] grid max-w-[520px] gap-2 pt-1" aria-label="本次任务成果">
    {series.map(({ latest, versions }) => <FileAttachment key={latest.id} artifact={latest} showVersion={latest.version > 1 || versions.length > 1} onOpen={onOpen} />)}
  </div>;
}
