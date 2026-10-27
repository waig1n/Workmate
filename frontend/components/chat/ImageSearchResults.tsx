"use client";

import { useState } from "react";

function publicLink(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return null;
    const host = url.hostname.toLowerCase();
    // Reject literal/local targets. DNS/network isolation is not provided
    // by browser rendering; do not route arbitrary URLs through our server.
    if (!host.includes(".") || host.endsWith(".localhost") || host.endsWith(".local")
      || host.endsWith(".internal") || /^[\d.]+$/.test(host) || host.includes(":")) return null;
    return url.href;
  } catch { return null; }
}

function Thumbnail({ src, title }: { src: string | null; title: string }) {
  const [failed, setFailed] = useState(false);
  return src && !failed
    // External previews deliberately bypass the Next.js server image proxy.
    // eslint-disable-next-line @next/next/no-img-element
    ? <img src={src} alt={title} loading="lazy" decoding="async" referrerPolicy="no-referrer"
        onError={() => setFailed(true)} className="aspect-[4/3] w-full rounded-lg bg-[var(--surface-subtle)] object-contain" />
    : <div className="grid aspect-[4/3] place-items-center rounded-lg bg-[var(--surface-subtle)] text-xs text-[var(--ink-faint)]">缩略图不可用</div>;
}

export function ImageSearchResults({ result, answer = "" }: { result: string | string[]; answer?: string }) {
  const [loadImages, setLoadImages] = useState(false);
  let rows: Record<string, unknown>[] = [];
  for (const payload of typeof result === "string" ? [result] : result) try {
    const parsed = JSON.parse(payload);
    if (parsed?.category === "images" && Array.isArray(parsed.results)) {
      rows.push(...parsed.results.filter((row: unknown) => row && typeof row === "object").slice(0, 20));
    }
  } catch { /* Partial SSE or non-image results are not a gallery. */ }
  const candidates = rows.map((row) => ({
    image: publicLink(row.image_url), source: publicLink(row.url),
    thumbnail: publicLink(row.thumbnail_url),
    title: typeof row.title === "string" ? row.title : "搜索图片",
  })).filter((row) => row.image && row.source);
  // Exact URL references, never title/substring guesses. An ambiguous source
  // page with multiple images does not identify a selected image.
  const references = [...answer.matchAll(/https?:\/\/[^\s<>"\u3000]+/g)]
    .map((match) => publicLink(match[0].replace(/[)\]}>.,;!?，。；！？：]+$/g, "")))
    .filter((url): url is string => Boolean(url));
  const unique = [...new Map(candidates.map((row) => [row.image, row])).values()];
  const images = unique.filter((row) => references.includes(row.image!) || (
    references.includes(row.source!) && unique.filter((other) => other.source === row.source).length === 1
  )).sort((a, b) => {
    const position = (row: typeof a) => Math.min(...references.flatMap((url, index) =>
      url === row.image || url === row.source ? [index] : []));
    return position(a) - position(b);
  });
  if (!images.length) return null;
  return <section aria-label="图片搜索结果" className="my-3 space-y-3">
    <div className="flex flex-wrap items-center gap-3 text-xs text-[var(--ink-soft)]">
      <span>回答引用的 {images.length} 张图片 · 使用前请核对来源与版权</span>
      <button type="button" onClick={() => setLoadImages(!loadImages)} className="text-[var(--accent)] underline underline-offset-4">{loadImages ? "隐藏缩略图" : "加载缩略图（访问外部网站）"}</button>
    </div>
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      {images.map((row, index) => <article key={`${row.image}-${index}`} className="min-w-0 space-y-2">
        {loadImages && <Thumbnail src={row.thumbnail} title={row.title} />}
        <p className="line-clamp-2 text-xs leading-5 text-[var(--ink-soft)]">{row.title}</p>
        <div className="flex gap-3 text-xs text-[var(--accent)]">
          <a href={row.image!} target="_blank" rel="noopener noreferrer">查看原图</a>
          <a href={row.source!} target="_blank" rel="noopener noreferrer">来源页</a>
        </div>
      </article>)}
    </div>
  </section>;
}
