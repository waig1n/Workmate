"use client";

import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import { Button } from '@/components/ui/Button';
import { Dialog } from '@/components/ui/Dialog';

type Preview = {
  eligible: boolean;
  reason?: string | null;
  file_count?: number;
  checkpoint_created_at?: string | null;
  has_file_snapshot?: boolean;
  memory_disabled?: boolean;
  warnings: string[];
};

export function TaskRecovery({ conversationId, onRecovered }: { conversationId: string; onRecovered?: (taskId: string) => void | Promise<void> }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [open, setOpen] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef<string | null>(null);
  useEffect(() => {
    let active = true;
    setPreview(null); setOpen(false); setError(null); setAcknowledged(false);
    requestId.current = null;
    api.get<Preview>(`/conversations/${conversationId}/recovery`).then(value => {
      if (active) setPreview(value);
    }).catch(() => { if (active) setError('恢复信息暂不可用，请刷新后重试'); });
    return () => { active = false; };
  }, [conversationId]);
  async function recover() {
    if (!acknowledged || busy) return;
    requestId.current ??= crypto.randomUUID();
    setBusy(true); setError(null);
    try {
      const result = await api.post<{ conversation_id: string; task_id: string }>(`/conversations/${conversationId}/recovery`, {
        request_id: requestId.current, acknowledge_data_loss: true,
      });
      if (result.conversation_id !== conversationId) throw new Error('恢复返回了不同的对话，请刷新后检查');
      setOpen(false);
      setPreview(previous => previous ? { ...previous, eligible: false, reason: null, memory_disabled: true } : null);
      await onRecovered?.(result.task_id);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '恢复失败，请重试');
    } finally { setBusy(false); }
  }
  if (!preview && !error) return null;
  if (preview && !preview.eligible && !preview.memory_disabled && !preview.reason && !error) return null;
  return <div className="rounded-xl border border-[var(--line)] bg-[var(--surface-subtle)] p-4 text-sm">
    {preview?.memory_disabled && <p className="mt-1 text-[var(--ink-soft)]">长期记忆暂不可用，当前对话的历史和已保存文件仍会保留。</p>}
    {preview?.eligible ? <Button onClick={() => setOpen(true)}>重新执行</Button> : preview?.reason && <p>{preview.reason}</p>}
    {error && <p role="alert" className="mt-2 text-red-700">{error}</p>}
    <Dialog open={open} onClose={() => { if (!busy) setOpen(false); }} title="恢复并重新执行" footer={<Button disabled={!acknowledged || busy} onClick={recover}>{busy ? '正在恢复…' : '确认并重新执行'}</Button>}>
      <p>将在当前对话重新执行原请求，消息、成果和之前的执行记录都会保留。</p>
      <p className="mt-2">可恢复 {preview?.file_count ?? 0} 个文件。{preview?.checkpoint_created_at ? `检查点时间：${new Date(preview.checkpoint_created_at).toLocaleString()}` : preview?.has_file_snapshot ? '将使用上次恢复时保存的文件清单。' : '没有可用工作目录检查点，仅恢复已保存的附件和成果。'}</p>
      <ul className="my-3 list-disc pl-5">{preview?.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
      <label className="flex items-start gap-2"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} />我理解未保存的修改可能丢失，重新执行可能重复已经发生的外部操作</label>
    </Dialog>
  </div>;
}
