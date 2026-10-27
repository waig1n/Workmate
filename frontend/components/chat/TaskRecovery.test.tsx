import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { TaskRecovery } from './TaskRecovery';
const { get, post, push } = vi.hoisted(() => ({get:vi.fn(),post:vi.fn(),push:vi.fn()}));
vi.mock('@/lib/api',()=>({api:{get,post}}));
vi.mock('next/navigation',()=>({useRouter:()=>({push})}));
beforeEach(()=>{vi.clearAllMocks();get.mockResolvedValue({eligible:true,file_count:2,checkpoint_created_at:null,warnings:['未保存的修改可能丢失']});});
it('requires explicit acknowledgement and preserves request identity after an uncertain response',async()=>{
 post.mockRejectedValueOnce(new Error('网络中断')).mockResolvedValueOnce({conversation_id:'old',task_id:'retry'});
 const recovered=vi.fn();
 render(<TaskRecovery conversationId="old" onRecovered={recovered} />);
 fireEvent.click(await screen.findByRole('button',{name:'重新执行'}));
 const submit=screen.getByRole('button',{name:'确认并重新执行'});
 expect(submit).toBeDisabled();expect(post).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('checkbox'));
 fireEvent.click(submit);
 await screen.findByText('网络中断');
 fireEvent.click(submit);
 await waitFor(()=>expect(recovered).toHaveBeenCalledWith('retry'));
 expect(push).not.toHaveBeenCalled();
 expect(screen.queryByRole('dialog')).toBeNull();
 expect(post.mock.calls[0][1]).toEqual(post.mock.calls[1][1]);
 expect(post.mock.calls[0][1].acknowledge_data_loss).toBe(true);
});
it('does not offer recovery without server eligibility',async()=>{
 get.mockResolvedValue({eligible:false,reason:'没有健康节点',warnings:[]});
 render(<TaskRecovery conversationId="old" />);
 await screen.findByText('没有健康节点');
 expect(screen.queryByRole('button',{name:'重新执行'})).toBeNull();
});
it('keeps the same conversation without exposing replacement infrastructure',async()=>{
 get.mockResolvedValue({eligible:false,memory_disabled:true,warnings:[]});
 render(<TaskRecovery conversationId="same" />);
 expect(await screen.findByText(/长期记忆暂不可用/)).toBeVisible();
 expect(screen.queryByRole('link')).toBeNull();
 expect(screen.queryByText(/新对话|节点|Workspace/)).toBeNull();
});
it('keeps normal conversations quiet',async()=>{
 get.mockResolvedValue({eligible:false,reason:null,warnings:[]});
 const {container}=render(<TaskRecovery conversationId="normal" />);
 await waitFor(()=>expect(get).toHaveBeenCalled());
 expect(container).toBeEmptyDOMElement();
});
it('labels an inherited file snapshot without claiming working files are absent',async()=>{
 get.mockResolvedValue({eligible:true,file_count:1,checkpoint_created_at:null,has_file_snapshot:true,warnings:[]});
 render(<TaskRecovery conversationId="again" />);
 fireEvent.click(await screen.findByRole('button',{name:'重新执行'}));
 expect(screen.getByText(/上次恢复时保存的文件清单/)).toBeVisible();
 expect(screen.queryByText(/没有可用工作目录检查点/)).toBeNull();
});
it('shows a retryable read error',async()=>{
 get.mockRejectedValue(new Error('network'));
 render(<TaskRecovery conversationId="old" />);
 expect(await screen.findByRole('alert')).toHaveTextContent('恢复信息暂不可用');
});
