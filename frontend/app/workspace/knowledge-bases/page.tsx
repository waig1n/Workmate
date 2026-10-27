'use client';
import { ResourceToolbar } from "@/components/ui/ResourceToolbar";
import { ResourcePagination } from "@/components/ui/ResourcePagination";
import { useResourcePage } from "@/lib/useResourcePage";
import { ResourceCard, resourceDrawerWidth, resourceGridClass } from '@/components/ui/ResourceCard';
import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { Scope } from '@/lib/types';
import { documentStatus, downloadKnowledgeDocument, processingDocument, type KnowledgeBase, type KnowledgeDocument, type KnowledgeSource } from '@/lib/knowledge';
import { Dialog } from '@/components/ui/Dialog';
import { ResourceAccessSources } from '@/components/sharing/ResourceAccessSources';
import { ResourceSharing } from '@/components/sharing/ResourceSharing';
import { KnowledgeSources } from '@/components/KnowledgeSources';

const control = 'product-control px-3 text-sm disabled:opacity-50';
type LibraryView = 'personal' | 'shared' | 'managed';
type CreateKind = 'personal' | 'team';

function relationshipLabel(library: KnowledgeBase) {
    if (library.scope.kind === 'personal') return '个人知识库';
    return library.actions.manage ? '团队知识库' : '共享给我';
}

export default function KnowledgeBasesPage() {
    const client = useQueryClient();
    const scopes = useQuery({ queryKey: ['scopes'], queryFn: () => api.get<Scope[]>('/scopes/accessible') });
    const teamScopes = (scopes.data ?? []).filter(scope => scope.kind !== 'personal' && scope.permissions?.includes('knowledge.manage'));
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [selectedSnapshot, setSelectedSnapshot] = useState<KnowledgeBase | null>(null);
    const detail = useQuery({ queryKey: ['knowledge-bases', 'detail', selectedId], queryFn: () => api.get<KnowledgeBase>(`/knowledge-bases/${selectedId}`), enabled: Boolean(selectedId), initialData: selectedSnapshot?.id === selectedId ? selectedSnapshot : undefined });
    const selected = selectedId ? detail.data ?? selectedSnapshot : null;
    const [creating, setCreating] = useState(false);
    const [createKind, setCreateKind] = useState<CreateKind>('personal');
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [scopeId, setScopeId] = useState('');
    const [error, setError] = useState('');
    const [filter, setFilter] = useState('');
    const [libraryView, setLibraryView] = useState<LibraryView>('personal');
    const libraries = useResourcePage<KnowledgeBase>('knowledge-bases', { q: filter.trim(), view: libraryView });
    const views: { id: LibraryView; label: string }[] = [
        { id: 'personal', label: '个人知识库' },
        { id: 'shared', label: '共享给我' },
        ...(teamScopes.length ? [{ id: 'managed' as const, label: '团队知识库' }] : []),
    ];

    useEffect(() => {
        if (libraryView === 'managed' && scopes.isSuccess && !teamScopes.length) setLibraryView('personal');
    }, [libraryView, scopes.isSuccess, teamScopes.length]);

    const create = useMutation({
        mutationFn: () => api.post<KnowledgeBase>('/knowledge-bases', {
            name: name.trim(), description,
            ...(createKind === 'team' ? { scope_id: scopeId } : {}),
        }),
        onSuccess: async library => {
            setSelectedSnapshot(library);
            client.setQueryData(['knowledge-bases', 'detail', library.id], library);
            setSelectedId(library.id);
            setFilter('');
            setLibraryView(createKind === 'team' ? 'managed' : 'personal');
            libraries.setPage(1);
            await client.invalidateQueries({ queryKey: ['knowledge-bases'] });
            setCreating(false);
        },
        onError: reason => setError(reason.message),
    });

    function beginCreate(kind: CreateKind) {
        setCreateKind(kind);
        setCreating(true);
        setName('');
        setDescription('');
        setScopeId(kind === 'team' ? teamScopes[0]?.id ?? '' : '');
        setError('');
    }

    const listed = libraries.items;
    const emptyText = libraryView === 'personal'
        ? '还没有个人知识库。创建后可以上传自己的资料。'
        : libraryView === 'shared'
          ? '暂时没有共享给你的知识库。'
          : '当前管理范围内还没有团队知识库。';

    return <div className="h-full overflow-y-auto bg-[var(--surface)] p-5 pb-28 sm:px-6 lg:pb-6"><div className="mx-auto max-w-[1180px]">
        <ResourceToolbar label="知识库分类" options={views.map(option => ({ value: option.id, label: option.label }))}
            value={libraryView} onChange={setLibraryView} searchLabel="搜索知识库" placeholder="搜索名称或描述" search={filter} onSearch={setFilter}
            actions={<>
                <button className="inline-flex h-10 shrink-0 items-center rounded-xl bg-[var(--ink)] px-4 text-sm font-medium text-[var(--surface)]" onClick={() => beginCreate('personal')}>创建个人知识库</button>
                {!!teamScopes.length && <button className="product-control h-10 px-4 text-sm" onClick={() => beginCreate('team')}>创建团队知识库</button>}
            </>} />
        {libraries.isSuccess && <p className="my-4 text-xs text-[var(--ink-soft)]">显示 {listed.length} 个知识库 · 管理与检索权限分别生效</p>}
        {libraries.isLoading && <p role="status">加载知识库…</p>}
        {libraries.isError && <p role="alert">知识库加载失败：{libraries.error.message} <button className="underline" onClick={() => void libraries.refetch()}>重试加载</button></p>}
        <div className={resourceGridClass}>{listed.map(library => {
            const relation = relationshipLabel(library);
            const maintenance = library.actions.manage && library.scope.kind !== 'personal' ? ` · ${library.scope.name}` : '';
            const access = library.actions.manage ? '可管理' : library.actions.use ? '可检索' : '无使用权限';
            return <ResourceCard key={library.id} onClick={() => { setSelectedSnapshot(library); setSelectedId(library.id); }} icon="archive" title={library.name} description={library.description || '尚未填写描述'} badge={<span className={`rounded-full px-2 py-1 text-[11px] ${library.enabled ? 'bg-[var(--surface-subtle)] text-[var(--ink-soft)]' : 'bg-[var(--surface-subtle)] text-[var(--ink-faint)]'}`}>{library.enabled ? '已启用' : '已停用'}</span>} metadata={`${relation}${maintenance} · ${access}`} />;
        })}</div>
        {libraries.isSuccess && !libraries.total && <p className="py-16 text-center text-[var(--ink-soft)]">{emptyText}</p>}
        {libraries.isSuccess && !!libraries.total && !listed.length && <p className="py-16 text-center text-[var(--ink-soft)]">当前筛选下没有知识库。调整搜索词后重试。</p>}
        <ResourcePagination page={libraries.page} totalPages={libraries.totalPages} total={libraries.total} busy={libraries.isFetching} onPage={libraries.setPage} />
        <Dialog open={creating} onClose={() => { if (!create.isPending) setCreating(false); }} title={createKind === 'team' ? '创建团队知识库' : '创建个人知识库'}>
            <form className="space-y-4" onSubmit={event => { event.preventDefault(); setError(''); create.mutate(); }}>
                <label className="block text-sm">知识库名称<input required maxLength={100} className={`${control} mt-1 w-full`} value={name} onChange={event => setName(event.target.value)}/></label>
                <label className="block text-sm">描述<textarea maxLength={2000} className={`${control} mt-1 w-full py-2`} value={description} onChange={event => setDescription(event.target.value)}/></label>
                {createKind === 'team' && teamScopes.length === 1 && <p className="text-sm">维护范围：{teamScopes[0].name}</p>}
                {createKind === 'team' && teamScopes.length > 1 && <label className="block text-sm">维护范围<select className={`${control} mt-1 w-full`} value={scopeId} onChange={event => setScopeId(event.target.value)}>{teamScopes.map(scope => <option key={scope.id} value={scope.id}>{scope.name}</option>)}</select></label>}
                <p className="text-xs text-[var(--ink-soft)]">{createKind === 'team' ? '团队知识库创建后默认不开放使用，请在详情中共享。维护范围创建后不可修改，管理权限也不自动授予检索或原文下载权限。' : '个人知识库仅由你管理和使用。需要团队协作时，由管理员创建并共享团队知识库。'}</p>
                {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
                <button className={control} disabled={create.isPending || !name.trim() || (createKind === 'team' && !scopeId)}>{create.isPending ? '创建中…' : '确认创建'}</button>
            </form>
        </Dialog>
        <Dialog open={!!selected} onClose={() => setSelectedId(null)} title={selected?.name ?? '知识库'} width={resourceDrawerWidth} side>{selected && <KnowledgeDetail key={selected.id} library={selected} onDeleted={() => setSelectedId(null)}/>}</Dialog>
    </div></div>;
}
function KnowledgeDetail({ library, onDeleted }: {
    library: KnowledgeBase;
    onDeleted: () => void;
}) {
    const client = useQueryClient();
    const base = `/knowledge-bases/${encodeURIComponent(library.id)}`;
    const documents = useQuery({ queryKey: ['knowledge-documents', library.id], queryFn: () => api.get<{
            items: KnowledgeDocument[];
            total: number;
        }>(`${base}/documents`), refetchInterval: q => q.state.data?.items.some(d => processingDocument(d.status)) ? 3000 : false });
    const [error, setError] = useState('');
    const [query, setQuery] = useState('');
    const [topK, setTopK] = useState(5);
    const [confirm, setConfirm] = useState<{
        path: string;
        label: string;
        library?: boolean;
    } | null>(null);
    const [name, setName] = useState(library.name);
    const [description, setDescription] = useState(library.description ?? '');
    const [documentFilter, setDocumentFilter] = useState<'all' | 'ready' | 'pending' | 'error'>('all');
    const refresh = () => client.invalidateQueries({ queryKey: ['knowledge-documents', library.id] });
    const mutation = useMutation({ mutationFn: async (action: () => Promise<unknown>) => action(), onSuccess: async () => { setError(''); await refresh(); await client.invalidateQueries({ queryKey: ['knowledge-bases'] }); }, onError: e => setError(e.message) });
    const startLock = useRef(false);
    const [starting, setStarting] = useState(false);
    const [startSummary, setStartSummary] = useState('');
    const busy = mutation.isPending || starting;
    const uploaded = (documents.data?.items ?? []).filter(doc => doc.status === 'uploaded');
    const visibleDocuments = (documents.data?.items ?? []).filter(doc => documentFilter === 'all'
        || (documentFilter === 'pending' && ['uploading', 'submitting', 'uploaded', 'pending', 'parsing', 'chunking', 'indexing'].includes(doc.status))
        || (documentFilter === 'error' && ['error', 'upload_failed'].includes(doc.status))
        || doc.status === documentFilter);
    async function startDocuments(ids: string[]) {
        if (startLock.current || mutation.isPending || !ids.length) return;
        startLock.current = true;
        setStarting(true);
        setError('');
        setStartSummary('');
        let succeeded = 0;
        const failures: string[] = [];
        try {
            for (const id of ids) {
                const doc = uploaded.find(item => item.id === id);
                if (!doc) continue;
                try {
                    await api.post(`${base}/documents/${encodeURIComponent(id)}/start`);
                    succeeded += 1;
                } catch (reason) {
                    failures.push(`${doc.filename}：${reason instanceof Error ? reason.message : '启动失败，请重试'}`);
                }
            }
            setStartSummary(`启动完成：成功 ${succeeded} 个，失败 ${failures.length} 个。`);
            setError(failures.join('；'));
            await refresh();
        } finally {
            startLock.current = false;
            setStarting(false);
        }
    }
    const search = useMutation({ mutationFn: () => api.post<{
            sources: KnowledgeSource[];
        }>(`${base}/search`, { query: query.trim(), top_k: topK }) });
    const remove = () => { if (!confirm)
        return; const target = confirm; mutation.mutate(async () => { await api.delete(target.path); setConfirm(null); if (target.library)
        onDeleted(); }); };
    const detailKind = library.scope.kind === 'personal'
        ? '个人知识库'
        : library.actions.manage
          ? `团队知识库 · 维护范围：${library.scope.name}`
          : '共享知识库';
    return <div className="space-y-6"><section className="rounded-xl bg-[var(--surface-subtle)] p-4 text-sm"><p className="font-medium">{detailKind}</p><p className="mt-1 text-xs text-[var(--ink-soft)]">{library.enabled ? '已启用' : '已停用'} · {library.actions.manage ? '可管理' : '仅可查看'} · {library.actions.use ? '可检索' : '当前账号无检索权限'}</p>{library.actions.manage && library.scope.kind !== 'personal' && <p className="mt-2 text-xs text-[var(--ink-soft)]">维护范围不决定使用范围。检索须显式共享，原文下载单独授权；管理权限不授予使用权。</p>}</section>
 {library.scope.kind !== 'personal' && <ResourceAccessSources kind="knowledge-bases" resourceId={library.id} />}
 {library.actions.manage && library.scope.kind !== 'personal' && <ResourceSharing kind="knowledge-bases" resourceId={library.id} />}
 {library.actions.manage && <details className="rounded-xl border border-[var(--line)] p-4"><summary className="cursor-pointer text-sm">知识库设置</summary><div className="mt-4 space-y-3"><label className="block text-sm">名称<input maxLength={100} className={`${control} w-full`} value={name} onChange={e => setName(e.target.value)}/></label><label className="block text-sm">描述<textarea className={`${control} w-full py-2`} maxLength={2000} value={description} onChange={e => setDescription(e.target.value)}/></label><p className="text-xs text-[var(--ink-soft)]">维护范围创建后不可修改；需要其他成员使用时，请共享此知识库，无需复制资料。</p><div className="flex flex-wrap gap-2"><button className={control} disabled={busy || !name.trim() || (name.trim() === library.name && description === library.description)} onClick={() => mutation.mutate(() => api.patch(base, { name: name.trim(), description }))}>保存信息</button><button className={control} disabled={busy} onClick={() => mutation.mutate(() => api.patch(base, { enabled: !library.enabled }))}>{library.enabled ? '停用知识库' : '启用知识库'}</button><button className={`${control} text-red-600`} disabled={busy} onClick={() => setConfirm({ path: base, label: library.name, library: true })}>删除知识库</button></div></div></details>}
 <section className="space-y-3"><div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">文档</h3>{library.actions.manage && uploaded.length > 0 && <button className={control} disabled={busy} onClick={() => void startDocuments(uploaded.map(doc => doc.id))}>启动全部待处理</button>}{library.actions.manage && <label className={`${control} flex cursor-pointer items-center`}>上传文档<input aria-label="上传文档" className="sr-only" type="file" accept=".txt,.md,.markdown,.pdf,.docx,.pptx,.xlsx" disabled={busy} onChange={event => { const file = event.target.files?.[0]; if (file) {
        const form = new FormData();
        form.append('file', file);
        mutation.mutate(() => api.upload(`${base}/documents`, form));
    } event.target.value = ''; }}/></label>}</div><p className="text-xs text-[var(--ink-soft)]">支持 TXT、Markdown、文本 PDF、DOCX、PPTX、XLSX。扫描件需先转换为可提取文本。上传只保存原文，不会立即解析或生成向量；可在低峰时手动启动处理。</p>{startSummary && <p role="status" className="text-sm">{startSummary}</p>}{busy && <p role="status" className="text-sm">处理中…</p>}{error && <p role="alert" className="text-sm text-red-600">{error}</p>}
 {documents.isLoading && <p role="status">加载文档…</p>}{documents.isError && <p role="alert">文档加载失败。<button className="underline" onClick={() => void documents.refetch()}>重新加载</button></p>}
 {documents.isSuccess && !!documents.data.items.length && <div className="flex flex-wrap items-center gap-2 text-xs"><span className="text-[var(--ink-soft)]">共 {documents.data.total} 份文档</span><select aria-label="筛选文档状态" className={`${control} ml-auto`} value={documentFilter} onChange={e => setDocumentFilter(e.target.value as typeof documentFilter)}><option value="all">全部状态</option><option value="ready">可用</option><option value="pending">待处理与处理中</option><option value="error">失败</option></select></div>}
 {visibleDocuments.map(doc => <article key={doc.id} className="rounded-xl border border-[var(--line)] p-4"><div className="flex flex-wrap justify-between gap-2"><h4 className="min-w-0 break-all text-sm font-medium">{doc.filename}</h4><span className="text-xs">{documentStatus[doc.status] ?? doc.status} · {doc.chunk_count ?? 0} 个片段</span></div>{doc.status === 'upload_failed' && <p className="mt-2 text-sm text-[var(--warning)]">原文上传未完成，请删除此记录后重新上传。</p>}{doc.error && <p className="mt-2 text-sm text-red-600">{doc.error}</p>}<div className="mt-3 flex gap-3 text-sm">{library.actions.download && !['uploading', 'upload_failed'].includes(doc.status) && <button className="underline" onClick={() => void downloadKnowledgeDocument(library.id, doc.id, doc.filename).catch(e => setError(e.message))}>下载原文</button>}{library.actions.manage && doc.status === 'uploaded' && <button className="underline disabled:opacity-50" disabled={busy} onClick={() => void startDocuments([doc.id])}>启动处理</button>}{library.actions.manage && doc.status === 'error' && <button className="underline disabled:opacity-50" disabled={busy} onClick={() => mutation.mutate(() => api.post(`${base}/documents/${encodeURIComponent(doc.id)}/retry`))}>重试</button>}{library.actions.manage && <button className="text-red-600 disabled:opacity-50" disabled={busy || processingDocument(doc.status)} onClick={() => setConfirm({ path: `${base}/documents/${encodeURIComponent(doc.id)}`, label: doc.filename })}>删除文档</button>}</div></article>)}{documents.isSuccess && !documents.data.items.length && <p className="rounded-xl bg-[var(--surface-subtle)] p-6 text-sm">尚未上传文档。</p>}{documents.isSuccess && !!documents.data.items.length && !visibleDocuments.length && <p className="rounded-xl bg-[var(--surface-subtle)] p-6 text-sm">当前状态下没有文档。</p>}</section>
 {library.actions.use && <section className="space-y-3 border-t border-[var(--line)] pt-5"><h3 className="font-medium">检索测试</h3><form className="flex flex-wrap gap-2" onSubmit={event => { event.preventDefault(); search.mutate(); }}><input aria-label="检索问题" placeholder="输入一个业务问题" className={`${control} min-w-0 flex-1`} value={query} onChange={e => setQuery(e.target.value)}/><input aria-label="返回片段数" type="number" min={1} max={50} className={`${control} w-20`} value={topK} onChange={e => setTopK(Number(e.target.value))}/><button className={control} disabled={search.isPending || !query.trim() || topK < 1 || topK > 50 || !library.enabled}>{search.isPending ? '检索中…' : '检索'}</button></form>{search.isError && <p role="alert" className="text-sm text-red-600">检索失败：{search.error.message}</p>}{search.isSuccess && (search.data.sources.length ? <KnowledgeSources sources={search.data.sources}/> : <p className="text-sm">没有命中相关片段。</p>)}</section>}
 <Dialog open={!!confirm} onClose={() => { if (!busy)
        setConfirm(null); }} title="确认删除"><p className="text-sm">确定删除“{confirm?.label}”？原文及索引将被移除。{confirm?.library ? '已被智能体绑定或有文档正在处理时无法删除。' : ''}</p>{error && <p role="alert" className="mt-3 text-sm text-red-600">{error}</p>}<div className="mt-4 flex gap-3"><button className={control} disabled={busy} onClick={remove}>确认删除</button><button className={control} disabled={busy} onClick={() => setConfirm(null)}>取消</button></div></Dialog></div>;
}
