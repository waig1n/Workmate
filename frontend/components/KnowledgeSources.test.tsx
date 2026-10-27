import {act,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {api} from '@/lib/api';
import {KnowledgeSources} from './KnowledgeSources';
import {downloadKnowledgeDocument} from '@/lib/knowledge';
vi.mock('@/lib/api',()=>({api:{get:vi.fn()}}));
beforeEach(()=>{vi.clearAllMocks();vi.mocked(api.get).mockResolvedValue({items:[{id:'kb',actions:{download:true}}],total:1,page:1,page_size:100});});
function mount(sources: Parameters<typeof KnowledgeSources>[0]['sources']) { return render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><KnowledgeSources sources={sources}/></QueryClientProvider>); }
vi.mock('@/lib/knowledge',()=>({downloadKnowledgeDocument:vi.fn()}));
it('downloads a selected original and shows authorization failures',async()=>{
 vi.mocked(downloadKnowledgeDocument).mockRejectedValue(new Error('当前无权访问'));
 mount([{source_id:'s',knowledge_base_id:'kb',document_id:'doc',filename:'资料.docx',chunk_index:2,metadata:{},excerpt:'片段',score:1}]);
 expect(screen.getByText('片段 3')).toBeInTheDocument();
 fireEvent.click(await screen.findByRole('button',{name:'资料.docx'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('当前无权访问');
 expect(downloadKnowledgeDocument).toHaveBeenCalledWith('kb','doc','资料.docx');
});
it('does not render an empty sources panel',()=>{const {container}=mount([]);expect(container).toBeEmptyDOMElement();});

it('renders citations as text when raw download is not authorized',async()=>{vi.mocked(api.get).mockResolvedValue({items:[{id:'kb',actions:{download:false}}],total:1,page:1,page_size:100});mount([{source_id:'s',knowledge_base_id:'kb',document_id:'doc',filename:'受限资料.pdf',chunk_index:0,metadata:{},excerpt:'可检索片段'}]);expect(screen.getByText('受限资料.pdf')).toBeVisible();expect(screen.queryByRole('button',{name:'受限资料.pdf'})).toBeNull();});

it('removes the original download action when current knowledge access is revoked',async()=>{
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><KnowledgeSources sources={[{source_id:'s',knowledge_base_id:'kb',document_id:'doc',filename:'历史资料.pdf',chunk_index:0,metadata:{},excerpt:'已交付的片段'}]}/></QueryClientProvider>);
 await screen.findByRole('button',{name:'历史资料.pdf'});
 act(()=>client.setQueryData(['knowledge-bases'],[{id:'kb',actions:{download:false}}]));
 await waitFor(()=>expect(screen.queryByRole('button',{name:'历史资料.pdf'})).toBeNull());
 expect(screen.getByText('已交付的片段')).toBeVisible();
 expect(downloadKnowledgeDocument).not.toHaveBeenCalled();
});
