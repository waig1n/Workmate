"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { api } from "@/lib/api";
import { adminErrorMessage } from "@/lib/errors";
import type { AdminScopeNode, SecurityAuditEvent, SecurityAuditPage } from "@/lib/types";

const labels: Record<string, string> = {
  "user.create": "创建用户", "user.update": "修改用户", "user.disable": "禁用用户",
  "user.enable": "恢复用户", "user.reset_password": "重置密码", "user.access_update": "修改用户授权",
  "user.operation_denied": "用户操作被拒绝", "user.access_update_denied": "用户授权修改被拒绝",
  "user.disable_interrupt_failed": "禁用用户后任务中断失败",
  "role.create": "创建角色", "role.update": "修改角色", "role.delete": "删除角色",
  "role.permissions_update": "修改角色权限", "role.permissions_update_denied": "角色权限修改被拒绝",
  "permission.create": "创建权限定义", "permission.update": "修改权限定义", "permission.delete": "删除权限定义",
  "organization.initialize": "创建企业工作空间", "organization.rename": "组织改名",
  "scope.create": "创建部门", "scope.rename": "部门改名",
  "scope.move": "移动部门", "scope.delete": "删除部门", "membership.add": "添加成员",
  "membership.remove": "移除成员", "role_binding.set": "分配角色", "role_binding.remove": "撤销角色",
};

function flatten(nodes: AdminScopeNode[]): AdminScopeNode[] {
  return nodes.flatMap((node) => [node, ...flatten(node.children)]);
}

export default function AuditPage() {
  const [scope, setScope] = useState("");
  const [eventType, setEventType] = useState("");
  const [since, setSince] = useState("");
  const [selected, setSelected] = useState<SecurityAuditEvent | null>(null);
  const scopes = useQuery({ queryKey: ["admin-scopes"], queryFn: api.adminScopeTree });
  const events = useInfiniteQuery({
    queryKey: ["admin-audit", scope, eventType, since],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams();
      if (scope) params.set("scope_id", scope);
      if (eventType) params.set("event_type", eventType);
      if (since) params.set("since", new Date(`${since}T00:00:00`).toISOString());
      if (pageParam) params.set("cursor", pageParam);
      return api.get<SecurityAuditPage>(`/admin/security-audit-events?${params}`);
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: false,
  });
  const items = events.data?.pages.flatMap((page) => page.items) ?? [];
  return <div className="h-full overflow-y-auto p-5 pb-28 sm:p-8 lg:p-10">
    <div className="mx-auto max-w-6xl">
      <header><p className="product-eyebrow">组织与访问</p><h1 className="mt-2 text-3xl font-semibold">管理审计</h1><p className="mt-3 text-sm text-[var(--ink-soft)]">查看有权访问范围内的账号、部门和角色变更。</p></header>
      <div className="mt-6 flex flex-wrap items-end gap-3">
        <label className="text-sm">生效范围<select aria-label="审计范围" className="product-control mt-2 block px-3" value={scope} onChange={(e) => setScope(e.target.value)}><option value="">全部可见范围</option>{flatten(scopes.data ?? []).filter((item) => item.permissions.includes("audit.read")).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label className="text-sm">操作类型<select aria-label="操作类型" className="product-control mt-2 block px-3" value={eventType} onChange={(e) => setEventType(e.target.value)}><option value="">全部操作</option>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label className="text-sm">开始日期<input aria-label="开始日期" type="date" className="product-control mt-2 block px-3" value={since} onChange={(e) => setSince(e.target.value)} /></label>
        <Button variant="secondary" onClick={() => void events.refetch()}>刷新记录</Button>
      </div>
      {events.isPending && <p role="status" className="mt-8 text-sm">正在加载审计记录…</p>}
      {events.isError && <div role="alert" className="mt-6 rounded-xl border border-red-200 p-4 text-sm text-red-700">{adminErrorMessage(events.error)}<button className="ml-3 underline" onClick={() => void events.refetch()}>重试</button></div>}
      {!events.isPending && !events.isError && !items.length && <p className="mt-8 rounded-xl border border-dashed p-8 text-center text-sm text-[var(--ink-soft)]">当前筛选下没有审计记录。</p>}
      {items.length > 0 && <div className="mt-6 overflow-x-auto rounded-2xl border border-[var(--line)]"><table className="w-full min-w-[620px] text-left text-sm"><thead className="bg-[var(--surface-subtle)] text-[var(--ink-soft)]"><tr><th className="p-4">时间</th><th className="p-4">操作</th><th className="p-4">操作者</th><th className="p-4">对象 / 范围</th><th className="p-4">详情</th></tr></thead><tbody className="divide-y divide-[var(--line)]">{items.map((item) => <tr key={item.id}><td className="p-4 whitespace-nowrap">{new Date(item.created_at).toLocaleString()}</td><td className="p-4">{labels[item.event_type] ?? item.event_type}</td><td className="p-4">{item.actor_name ?? "系统"}</td><td className="p-4">{item.target_name ?? item.scope_name ?? "组织治理"}</td><td className="p-4"><button className="text-[var(--accent-strong)]" onClick={() => setSelected(item)}>查看变更</button></td></tr>)}</tbody></table></div>}
      {items.length > 0 && <p role="status" className="mt-4 text-sm text-[var(--ink-faint)]">已加载 {items.length} 条记录{!events.hasNextPage && !events.isError ? " · 已显示全部记录" : " · 每批最多 50 条"}</p>}
      {events.hasNextPage && <Button className="mt-5" variant="secondary" disabled={events.isFetchingNextPage} onClick={() => void events.fetchNextPage()}>{events.isFetchingNextPage ? "加载中…" : "加载更多记录"}</Button>}
    </div>
    <Dialog open={Boolean(selected)} onClose={() => setSelected(null)} title="管理变更详情" description={selected ? `${labels[selected.event_type] ?? selected.event_type} · ${new Date(selected.created_at).toLocaleString()}` : undefined}>
      {selected && <div className="max-h-[60vh] overflow-y-auto text-sm"><dl className="grid grid-cols-[5rem_1fr] gap-3"><dt>操作者</dt><dd>{selected.actor_name ?? "系统"}</dd><dt>范围</dt><dd>{selected.scope_name ?? "组织治理"}</dd><dt>请求编号</dt><dd className="break-all">{selected.request_id ?? "未记录"}</dd></dl><h3 className="mt-5 font-medium">变更内容</h3><pre className="mt-3 overflow-x-auto rounded-xl bg-[var(--surface-subtle)] p-4 text-xs">{JSON.stringify(selected.metadata, null, 2)}</pre></div>}
    </Dialog>
  </div>;
}
