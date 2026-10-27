import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {beforeEach,expect,it,vi} from 'vitest';
import {TaskKnowledgeSources} from './TaskKnowledgeSources';
import {api} from '@/lib/api';
vi.mock('@/lib/api',()=>({api:{get:vi.fn()}}));
const source={source_id:'s',knowledge_base_id:'kb',document_id:'doc',filename:'手册.pdf',chunk_index:0,metadata:{page:1},excerpt:'制度片段',score:1};
const event={event_type:'knowledge_sources',seq:1501,payload:{sources:[source]}};
beforeEach(()=>vi.clearAllMocks());
it('fetches summaries separately and requests excerpts only after expansion',async()=>{
 vi.mocked(api.get).mockResolvedValue([event]);
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 client.setQueryData(['task-events','t'],[{event_type:'tool_result',payload:{}}]);
 render(<QueryClientProvider client={client}><TaskKnowledgeSources taskId="t" live={[]}/></QueryClientProvider>);
 expect(await screen.findByText('手册.pdf')).toBeInTheDocument();
 expect(screen.queryByText('制度片段')).toBeNull();
 expect(api.get).toHaveBeenCalledTimes(1);
 expect(api.get).toHaveBeenCalledWith('/tasks/t/knowledge-events?details=false&limit=100&offset=0');
 fireEvent.click(screen.getByRole('button',{name:'查看检索来源与详情'}));
 expect(await screen.findByText('制度片段')).toBeInTheDocument();
 expect(api.get).toHaveBeenCalledWith('/tasks/t/knowledge-events?details=true&limit=100&offset=0');
 expect(client.getQueryData(['task-knowledge-sources','t'])).toBeDefined();
 expect(client.getQueryData(['task-events','t'])).toEqual([{event_type:'tool_result',payload:{}}]);
});
it('loads source events beyond the first page on demand and deduplicates replay',async()=>{
 vi.mocked(api.get).mockImplementation(async path=>path.endsWith('offset=0')?Array.from({length:100},(_,seq)=>({...event,seq})):[{...event,seq:2001,payload:{sources:[{...source,source_id:'second',excerpt:'第二页片段'}]}}]);
 render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><TaskKnowledgeSources taskId="t" live={[{taskId:'t',runId:'r',source}]}/></QueryClientProvider>);
 await screen.findByRole('button',{name:'加载更多检索来源'});
 expect(api.get).toHaveBeenCalledTimes(1);
 fireEvent.click(screen.getByRole('button',{name:'查看检索来源与详情'}));
 const more=await screen.findByRole('button',{name:'加载更多检索来源'});
 fireEvent.click(more);
 expect(await screen.findByText('第二页片段')).toBeInTheDocument();
 expect(screen.getAllByText('制度片段')).toHaveLength(1);
 await waitFor(()=>expect(screen.queryByRole('button',{name:'加载更多检索来源'})).toBeNull());
 expect(api.get).toHaveBeenLastCalledWith('/tasks/t/knowledge-events?details=true&limit=100&offset=100');
});

it('does not poll sources during a running task and preserves failures',async()=>{
 vi.mocked(api.get).mockResolvedValue([{event_type:'tool_result',seq:1,payload:{tool:'search_knowledge',status:'denied'}}]);
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 const view=render(<QueryClientProvider client={client}><TaskKnowledgeSources taskId="t" live={[]} complete={false}/></QueryClientProvider>);
 expect(api.get).not.toHaveBeenCalled();
 view.rerender(<QueryClientProvider client={client}><TaskKnowledgeSources taskId="t" live={[]} complete/></QueryClientProvider>);
 expect(await screen.findByText(/检索失败或被拒绝/)).toBeVisible();
 expect(screen.queryByText('未找到相关资料。')).toBeNull();
});
