"use client";

import { Children, type ReactNode, useState } from "react";
import dynamic from "next/dynamic";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

const DynamicCodeHighlighter = dynamic(() => import("./CodeHighlighter"), {
  ssr: false,
  loading: () => <pre className="overflow-auto bg-[#111318] p-4 text-[12px] text-neutral-200">正在加载代码高亮…</pre>,
});

function safeUrlTransform(url: string): string {
  if (url.startsWith("#") || url.startsWith("/")) return url;
  try {
    const parsed = new URL(url, "https://workmate.invalid");
    if (!SAFE_PROTOCOLS.has(parsed.protocol)) return "";
  } catch {
    return "";
  }
  return defaultUrlTransform(url);
}

function Code({ className, children }: { className?: string; children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const value = String(children ?? "").replace(/\n$/, "");
  const language = className?.match(/language-([\w-]+)/)?.[1];
  const block = Boolean(language || String(children ?? "").includes("\n"));

  if (!block) {
    return (
      <code className="rounded-md bg-neutral-100 px-1.5 py-0.5 font-mono text-[.9em] text-pink-700 dark:bg-neutral-800 dark:text-pink-300">
        {children}
      </code>
    );
  }

  return (
    <div className="my-4 overflow-hidden rounded-xl border border-neutral-700 bg-[#111318] text-neutral-100">
      <div className="flex items-center justify-between border-b border-neutral-700 px-3 py-2 text-[10px] text-neutral-400">
        <span>{language ?? "代码"}</span>
        <button
          type="button"
          className="rounded-md px-2 py-1 hover:bg-neutral-800"
          onClick={async () => {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          }}
        >
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <div className="max-h-[32rem] overflow-auto"><DynamicCodeHighlighter value={value} language={language} /></div>
    </div>
  );
}

export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-message min-w-0 break-words text-[15px] leading-[1.82] text-[var(--ink)]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        skipHtml
        urlTransform={safeUrlTransform}
        components={{
          a: ({ children, node: _node, ...props }) => (
            <a
              {...props}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-[var(--accent-strong)] underline decoration-[color-mix(in_srgb,var(--accent)_25%,transparent)] underline-offset-4 hover:decoration-[var(--accent)]"
            >
              {children}
              <span className="sr-only">（在新窗口打开）</span>
            </a>
          ),
          blockquote: ({ children }) => (
            <blockquote className="my-5 border-l-2 border-[var(--accent)] pl-4 text-[var(--ink-soft)]">
              {children}
            </blockquote>
          ),
          code: ({ className, children }) => <Code className={className}>{children}</Code>,
          h1: ({ children }) => <h1 className="mb-4 mt-7 text-[24px] font-semibold leading-tight tracking-[-.03em] first:mt-0">{children}</h1>,
          h2: ({ children }) => <h2 className="mb-3 mt-7 text-[19px] font-semibold tracking-[-.02em] first:mt-0">{children}</h2>,
          h3: ({ children }) => <h3 className="mb-2 mt-6 text-[16px] font-semibold first:mt-0">{children}</h3>,
          hr: () => <hr className="my-7 border-[var(--line)]" />,
          li: ({ children }) => <li className="my-1 pl-1">{children}</li>,
          ol: ({ children }) => <ol className="my-4 list-decimal space-y-1.5 pl-6">{children}</ol>,
          p: ({ children }) => <p className="my-3 first:mt-0 last:mb-0">{children}</p>,
          pre: ({ children }) => <>{Children.toArray(children)}</>,
          table: ({ children }) => (
            <div className="my-5 max-w-full overflow-x-auto rounded-[14px] border border-[var(--line)]">
              <table className="w-full min-w-[420px] border-collapse text-left text-[13px]">{children}</table>
            </div>
          ),
          td: ({ children }) => <td className="border-t border-neutral-200 px-3 py-2 align-top dark:border-neutral-700">{children}</td>,
          th: ({ children }) => <th className="bg-neutral-50 px-3 py-2 font-semibold dark:bg-neutral-800">{children}</th>,
          ul: ({ children }) => <ul className="my-4 list-disc space-y-1.5 pl-6 marker:text-[var(--accent)]">{children}</ul>,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
