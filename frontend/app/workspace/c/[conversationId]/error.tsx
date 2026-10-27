"use client";

import { useEffect } from "react";

import { Icon } from "@/components/ui/Icons";

export default function ConversationError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("conversation workbench render failed", error);
  }, [error]);
  return (
    <main className="grid h-full place-items-center bg-[#fcfcfd] p-6 dark:bg-[#121316]">
      <section className="max-w-sm rounded-2xl border border-red-200 bg-white p-6 text-center shadow-sm dark:border-red-900 dark:bg-neutral-900">
        <span className="mx-auto grid h-10 w-10 place-items-center rounded-xl bg-red-50 text-red-600 dark:bg-red-950"><Icon name="x" className="h-4 w-4" /></span>
        <h1 className="mt-4 text-base font-semibold">会话工作台暂时无法显示</h1>
        <p className="mt-2 text-[11px] leading-5 text-neutral-500">消息和成果仍保存在产品层。你可以安全重试，不会重新执行任务。</p>
        <button type="button" onClick={reset} className="mt-5 rounded-xl bg-neutral-900 px-4 py-2 text-[11px] font-medium text-white dark:bg-white dark:text-neutral-900">重新加载界面</button>
      </section>
    </main>
  );
}
