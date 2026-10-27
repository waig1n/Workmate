import { getToken } from './api';
export interface KnowledgeConfig {
    knowledge_base_ids: string[];
    parameters: {
        mode: 'agentic' | 'static';
        top_k: number;
    };
}
export const emptyKnowledgeConfig = (): KnowledgeConfig => ({ knowledge_base_ids: [], parameters: { mode: 'agentic', top_k: 5 } });
export interface KnowledgeBase {
    id: string;
    name: string;
    description: string;
    scope_id: string;
    scope: {
        id: string;
        name: string;
        kind: string;
    };
    enabled: boolean;
    actions: {
        use: boolean;
        manage: boolean;
        download: boolean;
    };
    created_at: string;
}
export interface KnowledgeDocument {
    id: string;
    filename: string;
    size: number;
    content_type: string;
    status: string;
    error: string | null;
    chunk_count: number;
    created_at: string;
    updated_at: string;
}
export interface KnowledgeSource {
    source_id: string;
    knowledge_base_id: string;
    document_id: string;
    filename: string;
    chunk_index: number;
    metadata: Record<string, unknown>;
    excerpt?: string;
    score?: number;
}
export const documentStatus: Record<string, string> = { uploading: '上传中', upload_failed: '上传失败', submitting: '提交处理中', uploaded: '待启动', pending: '等待处理', parsing: '解析中', chunking: '切分中', indexing: '索引中', ready: '可用', error: '失败' };
export const processingDocument = (status: string) => ['uploading', 'submitting', 'pending', 'parsing', 'chunking', 'indexing'].includes(status);
export async function downloadKnowledgeDocument(kb: string, doc: string, filename: string) {
    const token = getToken();
    const response = await fetch(`/api/knowledge-bases/${encodeURIComponent(kb)}/documents/${encodeURIComponent(doc)}/content`, { headers: token ? { Authorization: `Bearer ${token}` } : {}, cache: 'no-store' });
    if (!response.ok)
        throw new Error(response.status === 404 || response.status === 403 ? '原文不存在或当前无权访问' : `原文下载失败 (${response.status})`);
    const url = URL.createObjectURL(await response.blob());
    try {
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = filename;
        anchor.click();
    }
    finally {
        URL.revokeObjectURL(url);
    }
}
export interface TaskKnowledgeSource {
    taskId: string;
    runId: string | null;
    source: KnowledgeSource;
}
export function parseKnowledgeSources(value: unknown): KnowledgeSource[] {
    if (!Array.isArray(value))
        return [];
    return value.filter((s): s is KnowledgeSource => !!s && typeof s === 'object' && typeof s.source_id === 'string' && typeof s.knowledge_base_id === 'string' && typeof s.document_id === 'string' && typeof s.filename === 'string' && (s.excerpt === undefined || typeof s.excerpt === 'string')).map(s => ({ ...s, metadata: s.metadata && typeof s.metadata === 'object' ? s.metadata : {}, chunk_index: Number.isInteger(s.chunk_index) ? s.chunk_index : 0 }));
}

export interface KnowledgeProgress {
    status: string;
    search_count: number;
    failure_count: number;
}
