"use client";
export function ResourcePagination({ page, totalPages, total, busy, onPage }: {
 page: number; totalPages: number; total: number; busy: boolean; onPage: (page: number) => void;
}) {
 return <nav aria-label="资源分页" className="mt-6 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] pt-4 text-sm">
  <span className="text-[var(--ink-soft)]">第 {page} / {totalPages} 页 · 共 {total} 条</span>
  <div className="flex gap-2"><button className="product-control px-3 py-2 disabled:opacity-50" disabled={busy || page <= 1} onClick={() => onPage(page - 1)}>上一页</button><button className="product-control px-3 py-2 disabled:opacity-50" disabled={busy || page >= totalPages} onClick={() => onPage(page + 1)}>下一页</button></div>
 </nav>;
}
export function CatalogPagination({ page, nextCursor, busy, onPrevious, onNext }: {
 page: number; nextCursor?: string | null; busy: boolean; onPrevious: () => void; onNext: (cursor: string) => void;
}) {
 if (page === 1 && !nextCursor) return null;
 return <nav aria-label="目录分页" className="mt-4 flex items-center justify-between gap-3 text-sm"><span>目录第 {page} 页</span><div className="flex gap-2"><button className="product-control px-3 py-2 disabled:opacity-50" disabled={busy || page === 1} onClick={onPrevious}>上一页目录</button><button className="product-control px-3 py-2 disabled:opacity-50" disabled={busy || !nextCursor} onClick={() => nextCursor && onNext(nextCursor)}>下一页目录</button></div></nav>;
}
