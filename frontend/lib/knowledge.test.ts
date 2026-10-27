import {beforeEach,expect,it,vi} from 'vitest';
import {downloadKnowledgeDocument,emptyKnowledgeConfig,parseKnowledgeSources,processingDocument} from './knowledge';
beforeEach(()=>vi.restoreAllMocks());
it('validates source payloads and models the supported indexing states',()=>{
 expect(emptyKnowledgeConfig().knowledge_base_ids).toEqual([]);
 expect(parseKnowledgeSources(null)).toEqual([]);
 expect(parseKnowledgeSources([{},null])).toEqual([]);
 expect(processingDocument('uploaded')).toBe(false);
 expect(processingDocument('ready')).toBe(false);
 expect(processingDocument('indexing')).toBe(true);
});
it('reauthenticates original downloads and never exposes a failed response as a file',async()=>{
 localStorage.setItem('workmate_token','token');
 const fetch=vi.fn().mockResolvedValue({ok:false,status:403});vi.stubGlobal('fetch',fetch);
 await expect(downloadKnowledgeDocument('k/b','d','file.pdf')).rejects.toThrow('当前无权访问');
 expect(fetch).toHaveBeenCalledWith('/api/knowledge-bases/k%2Fb/documents/d/content',expect.objectContaining({headers:{Authorization:'Bearer token'},cache:'no-store'}));
});
it('downloads a successful original and releases its temporary URL',async()=>{
 const blob=new Blob(['text']);
 vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,blob:async()=>blob}));
 const create=vi.fn().mockReturnValue('blob:original');const revoke=vi.fn();
 Object.defineProperty(URL,'createObjectURL',{configurable:true,value:create});Object.defineProperty(URL,'revokeObjectURL',{configurable:true,value:revoke});
 const click=vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{});
 await downloadKnowledgeDocument('kb','doc','readme.txt');
 expect(click).toHaveBeenCalledOnce();expect(create).toHaveBeenCalledWith(blob);expect(revoke).toHaveBeenCalledWith('blob:original');
});
it('polls upload and submission handoffs but leaves failed uploads idle',()=>{
 expect(processingDocument('uploading')).toBe(true);
 expect(processingDocument('submitting')).toBe(true);
 expect(processingDocument('upload_failed')).toBe(false);
});
