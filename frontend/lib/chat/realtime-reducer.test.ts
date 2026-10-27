import { describe, expect, it } from 'vitest';
import { chatReducer, initialChatState } from './reducer';
import { encodeRealtimeCursor } from './realtime-protocol';
const cid = 'c', generation = '11111111-1111-4111-8111-111111111111';
const id = (n: number) => encodeRealtimeCursor({ schema_version: 1, conversation_id: cid, generation, stream_id: `${n}-0` });
const reset = { schema_version: 1, conversation_id: cid, generation, reason: 'initial', snapshots: [], business_state: { messages: [], tasks: [], artifacts: [], activity: [], knowledge: [], pending_confirm: null, decided_confirm_tools: {}, closed_confirm_versions: [] }, business_seq: 5, sync_status: 'synced' };
const apply = (state: ReturnType<typeof initialChatState>, name: string, value: unknown, cursor = '') => chatReducer(state, { type: 'realtime', name, value, cursor, conversationId: cid });
describe('atomic realtime state', () => {
  it('installs reset with cursor atomically and blocks invalid progress', () => {
    const s = apply(initialChatState(), 'REALTIME_RESET', reset, id(1));
    expect(s.realtime?.cursor).toBe(id(1));
    const broken = apply(s, 'REALTIME_EVENT', {}, id(2));
    expect(broken.realtime?.cursor).toBe('');
    expect(broken.realtime?.needsReset).toBe(true);
  });
  it('accepts late business sequence above baseline and deduplicates identities', () => {
    let s = apply(initialChatState(), 'REALTIME_RESET', reset, id(1));
    const event = (seq: number, event_id: string) => ({ schema_version: 1, event_id, conversation_id: cid, source: 'business', business_seq: seq, payload: { type: 'STEP_STARTED', stepName: event_id } });
    s = apply(s, 'REALTIME_EVENT', event(9, 'nine'), id(2));
    s = apply(s, 'REALTIME_EVENT', event(7, 'seven'), id(3));
    s = apply(s, 'REALTIME_EVENT', event(7, 'seven'), id(4));
    expect(s.steps.map(x => x.name)).toEqual(['nine', 'seven']);
    expect(s.realtime?.cursor).toBe(id(4));
  });
});
import { realtimeMessageId } from './realtime-protocol';
const mid = realtimeMessageId('t', 'r');
const body = (kind: string, version = 1, delta = '你😀', epoch = 1) => ({ schema_version: 1, event_id: `${kind}-${version}-${epoch}`, conversation_id: cid, task_id: 't', reply_id: 'r', source: 'worker', execution_epoch: epoch, payload: { type: `TEXT_MESSAGE_${kind}`, messageId: 'block', ...(kind === 'CONTENT' ? { delta } : {}) }, ...(kind === 'CONTENT' ? { text: { message_id: mid, block_id: 'block', content_version: version, from_offset: version === 1 ? 0 : 7, to_offset: version === 1 ? 7 : 7 + new TextEncoder().encode(delta).length } } : {}) });
const snapshot = { schema_version: 1, message_id: mid, conversation_id: cid, task_id: 't', reply_id: 'r', execution_epoch: 1, snapshot_version: 1, blocks: [{ block_id: 'block', kind: 'text', text: '你😀', content_version: 1, closed: false }], covered_cursor: { schema_version: 1, conversation_id: cid, generation, stream_id: '3-0' }, business_seq: 5, phase: 'approval', incomplete: false, process_end: 2 };
const boot = () => apply(initialChatState(), 'REALTIME_RESET', reset, id(1));
it('applies UTF8 intervals once, ignoring duplicate versions at new stream positions', () => {
  let s = apply(boot(), 'REALTIME_EVENT', body('START'), id(2));
  s = apply(s, 'REALTIME_EVENT', body('CONTENT'), id(3));
  s = apply(s, 'REALTIME_EVENT', body('CONTENT'), id(4));
  expect(s.items.find(i => i.id === mid)).toMatchObject({ text: '你😀' });
  expect(s.realtime?.cursor).toBe(id(4));
});
it('a gap preserves shown content, clears cursor and refuses subsequent events until reset', () => {
  let s = apply(boot(), 'REALTIME_EVENT', body('START'), id(2));
  s = apply(s, 'REALTIME_EVENT', body('CONTENT'), id(3));
  s = apply(s, 'REALTIME_EVENT', body('CONTENT', 3, '!'), id(4));
  expect(s.realtime?.needsReset).toBe(true);
  expect(s.items.find(i => i.id === mid)).toMatchObject({ text: '你😀' });
  expect(apply(s, 'REALTIME_EVENT', body('CONTENT', 2, '!'), id(5)).realtime?.cursor).toBe('');
});
it('installs id-less boundary snapshot and continues same Reply on a new epoch', () => {
  let s = apply(boot(), 'REALTIME_SNAPSHOT', snapshot);
  expect(s.realtime?.cursor).toBe(id(1));
  s = apply(s, 'REALTIME_EVENT', body('CONTENT', 2, '!', 2), id(4));
  expect(s.items.find(i => i.id === mid)).toMatchObject({ text: '你😀!' });
  s = apply(s, 'REALTIME_EVENT', body('CONTENT', 2, 'old', 1), id(5));
  expect(s.items.find(i => i.id === mid)).toMatchObject({ text: '你😀!' });
});
it('late terminal delta cannot append and stale approval cut cannot erase live suffix', () => {
  let s = apply(boot(), 'REALTIME_SNAPSHOT', snapshot);
  s = apply(s, 'REALTIME_EVENT', body('CONTENT', 2, '!'), id(4));
  s = apply(s, 'REALTIME_SNAPSHOT', snapshot);
  expect(s.items.find(i => i.id === mid)).toMatchObject({ text: '你😀!' });
  const final = { ...snapshot, snapshot_version: 2, phase: 'final', blocks: [{ ...snapshot.blocks[0], text: '你😀!', content_version: 2, closed: true }] };
  s = apply(s, 'REALTIME_SNAPSHOT', final);
  s = apply(s, 'REALTIME_EVENT', body('START'), id(5));
  s = apply(s, 'REALTIME_EVENT', body('CONTENT', 3, 'late'), id(6));
  expect(s.items.find(i => i.id === mid)).toMatchObject({ text: '你😀!', done: true });
});
it('reset atomically replaces approval/artifacts and rejects late REST hydration', () => {
  let s = boot();
  s = chatReducer(s, { type: 'hydrate-confirm', pending: { decisionVersion: 'stale', taskId: 't', toolCalls: [] } as never });
  expect(s.pendingConfirm).toBeNull();
  s = chatReducer(s, { type: 'hydrate-artifacts', artifacts: [{ id: 'stale' }] as never, conversationId: cid });
  expect(s.artifacts).toEqual([]);
  const changed = apply(s, 'REALTIME_RESET', { ...reset, generation: '22222222-2222-4222-8222-222222222222' }, id(2));
  expect(changed.realtime?.needsReset).toBe(true);
});
it('historical incomplete status does not block a new healthy message', () => {
  let s = apply(initialChatState(), 'REALTIME_RESET', { ...reset, sync_status: 'incomplete' }, id(1));
  s = apply(s, 'REALTIME_EVENT', body('START'), id(2));
  s = apply(s, 'REALTIME_EVENT', body('CONTENT'), id(3));
  expect(s.items.find(i => i.id === mid)).toMatchObject({ text: '你😀' });
});
const task = (key: string, date: string) => ({ id: key, conversation_id: cid, title: key, status: 'running', run_id: `reply-${key}`, approval_profile: 'standard', error: null, duration_ms: null, steps: null, model_name: null, token_usage: null, created_at: date, updated_at: date });
it('validates reset business data before replacing baseline', () => {
  const s = boot();
  for (const business of [{ ...reset.business_state, tasks: [{ id: 'missing-fields' }] }, { ...reset.business_state, pending_confirm: { decisionVersion: 'x', toolCalls: 'bad' } }, { ...reset.business_state, artifacts: [{ id: 'a' }] }, { ...reset.business_state, activity: [{ event_type: 'tool_call', payload: {} }] }, { ...reset.business_state, messages: [{ id: 'm', content: null }] }, { ...reset.business_state, knowledge: [{ event_type: 'knowledge_sources', payload: { sources: 'bad' } }] }]) {
    const failed = apply(s, 'REALTIME_RESET', { ...reset, business_state: business }, id(2));
    expect(failed.realtime?.needsReset).toBe(true);
    expect(failed.items).toEqual(s.items);
  }
});
it('requires an exact applied approval reference and does not acknowledge a missing cut', () => {
  const saved = apply(boot(), 'REALTIME_SNAPSHOT', snapshot);
  const event = { schema_version: 1, event_id: 'approval', conversation_id: cid, task_id: 't', reply_id: 'r', source: 'business', business_seq: 6, payload: { type: 'CUSTOM', name: 'HITL_APPROVAL_REQUEST', value: { taskId: 't', decisionVersion: 'v', toolCalls: [{ id: 'call', name: 'write', input: '{}' }] }, display_snapshot: { message_id: mid, snapshot_version: 1, execution_epoch: 1, sha256: 'a'.repeat(64) } } };
  expect(apply(saved, 'REALTIME_EVENT', event, id(4)).pendingConfirm?.decisionVersion).toBe('v');
  const wrong = { ...event, payload: { ...event.payload, display_snapshot: { ...event.payload.display_snapshot, snapshot_version: 2 } } };
  expect(apply(saved, 'REALTIME_EVENT', wrong, id(4)).realtime?.needsReset).toBe(true);
});
it('orders reset history chronologically and reconstructs tool activity', () => {
  const old = task('old', '2026-01-01'), recent = task('new', '2026-02-01');
  const b = { ...reset.business_state, tasks: [recent, old], messages: [{ id: 'n', role: 'user', task_id: 'new', content: { text: 'new' } }, { id: 'o', role: 'user', task_id: 'old', content: { text: 'old' } }], activity: [{ id: 1, seq: 1, task_id: 'old', event_type: 'tool_call', created_at: '2026-01-01', payload: { toolCallId: 'call', tool: 'write' } }, { id: 2, seq: 2, task_id: 'old', event_type: 'tool_result', created_at: '2026-01-01', payload: { toolCallId: 'call', tool: 'write', status: 'success' } }] };
  const s = apply(initialChatState(), 'REALTIME_RESET', { ...reset, business_state: b }, id(1));
  expect(s.items.filter(i => i.kind === 'text').map(i => i.id)).toEqual(['o', 'n']);
  expect(s.items.find(i => i.kind === 'tool')).toMatchObject({ call: { toolCallId: 'call', state: 'success' }, runId: 'reply-old' });
});
it('preserves optimistic send and accepts another tab Task without waiting for a periodic snapshot', () => {
  const optimistic = chatReducer(boot(), { type: 'optimistic-send', id: 'local', text: 'new', fileNames: [] });
  const restored = apply(optimistic, 'REALTIME_RESET', reset, id(2));
  expect(restored.pendingAssistantId).toBe('assistant-pending-local');
  expect(restored.run.active).toBe(true);
  expect(restored.items.some(i => i.id === 'user-local')).toBe(true);
  const event = { schema_version: 1, event_id: 'start-new', conversation_id: cid, task_id: 'another', reply_id: 'reply', source: 'business', business_seq: 6, payload: { type: 'RUN_STARTED' } };
  expect(apply(restored, 'REALTIME_EVENT', event, id(3)).runs.some(run => run.taskId === 'another')).toBe(true);
});

import actualBusiness from './fixtures/reset-business-state.json';
it('accepts real TaskOut and numeric TaskEventOut Activity/RAG JSON',()=>{
 const s=apply(initialChatState(),'REALTIME_RESET',{...reset,business_state:actualBusiness},id(1));
 expect(s.realtime?.needsReset).toBe(false);
 expect(s.items.find(i=>i.kind==='tool')).toMatchObject({call:{toolCallId:'call',state:'success'}});
 expect(s.knowledgeSources).toHaveLength(1);
 expect(s.pendingConfirm?.decisionVersion).toBe('decision');
});

const businessEvent = (type: string, seq: number, payload: Record<string, unknown> = {}) => ({schema_version:1,event_id:`business-${seq}`,conversation_id:cid,task_id:'t',reply_id:'r',source:'business',business_seq:seq,payload:{type,...payload}});
it('orders task controls newest first independently of chronological messages',()=>{
 const s=apply(initialChatState(),'REALTIME_RESET',{...reset,business_state:{...reset.business_state,tasks:[task('old','2026-01-01'),task('new','2026-02-01')]}},id(1));
 expect(s.realtime?.tasks.map(t=>t.id)).toEqual(['new','old']);
});
it('updates known task Reply when RUN_STARTED follows created receipt',()=>{
 let s=apply(initialChatState(),'REALTIME_RESET',{...reset,business_state:{...reset.business_state,tasks:[{...task('t','2026-01-01'),status:'created',run_id:null}]}},id(1));
 s=apply(s,'REALTIME_EVENT',businessEvent('RUN_STARTED',6),id(2));
 expect(s.realtime?.tasks[0]).toMatchObject({status:'running',run_id:'r'});
});
it('new cross-tab Task takes focus after old task finished and displays approval',()=>{
 let s=boot();
 s={...s,run:{active:false,status:'RUN_FINISHED',runId:'old-r',error:null},runs:[{id:'old-r',taskId:'old',status:'RUN_FINISHED',error:null}]};
 s=apply(s,'REALTIME_EVENT',businessEvent('RUN_STARTED',6),id(2));
 expect(s.run).toMatchObject({active:true,runId:'r'});
 s=apply(s,'REALTIME_EVENT',businessEvent('CUSTOM',7,{name:'HITL_APPROVAL_REQUEST',display_empty:true,value:{taskId:'t',decisionVersion:'v',toolCalls:[{id:'approve',name:'write',input:'{}'}]}}),id(3));
 expect(s.pendingConfirm?.decisionVersion).toBe('v');
});
it('replaces same task optimistic assistant when typed body starts',()=>{
 let s=chatReducer(boot(),{type:'optimistic-send',id:'local',text:'question',fileNames:[]});
 s=chatReducer(s,{type:'task-accepted',assistantId:'assistant-pending-local',taskId:'t',queued:false});
 s=apply(s,'REALTIME_EVENT',businessEvent('RUN_STARTED',6),id(2));
 s=apply(s,'REALTIME_EVENT',body('START'),id(3));
 s=apply(s,'REALTIME_EVENT',body('CONTENT'),id(4));
 expect(s.pendingAssistantId).toBeNull();
 expect(s.items.filter(i=>i.kind==='text'&&i.role==='assistant')).toHaveLength(1);
 expect(s.items.some(i=>i.id==='user-local')).toBe(true);
});

import { mergeRealtimeTasks } from './realtime-tasks';
import type { Task } from '../types';
it('REST refresh enriches terminal statistics without rolling back realtime state',()=>{
 const live: Task={...actualBusiness.tasks[0],status:'completed',approval_profile:'standard',execution_phase:null};
 const fetched: Task={...live,status:'running',run_id:null,steps:5,duration_ms:800,model_name:'updated',token_usage:{output_tokens:12},updated_at:'2026-10-01T02:00:00Z'};
 expect(mergeRealtimeTasks([live],[fetched])[0]).toMatchObject({status:'completed',run_id:'r',steps:5,duration_ms:800,model_name:'updated',token_usage:{output_tokens:12}});
});

it('rebuilds RAG counters from completed search calls and compact sources',()=>{
 const b={...actualBusiness,tasks:[{...actualBusiness.tasks[0],status:'completed'}]};
 const s=apply(initialChatState(),'REALTIME_RESET',{...reset,business_state:b},id(1));
 expect(s.knowledgeProgress.r).toEqual({status:'completed',search_count:1,failure_count:0});
});

it('same Reply resume clears approval state and survives late REST metadata',()=>{
 let s=apply(initialChatState(),'REALTIME_RESET',{...reset,business_state:actualBusiness},id(1));
 s=apply(s,'REALTIME_EVENT',businessEvent('CUSTOM',6,{name:'TASK_TIMELINE_EVENT',value:{phase:'run_resumed',replyId:'r'}}),id(2));
 expect(s.realtime?.tasks[0]).toMatchObject({status:'running',run_id:'r'});
 expect(s.run).toMatchObject({active:true,status:'running',runId:'r'});
 expect(s.pendingConfirm).toBeNull();
 expect(s.runs.find(r=>r.id==='r')?.status).toBe('running');
 const stale: Task={...actualBusiness.tasks[0],status:'awaiting_permission',approval_profile:'standard',execution_phase:null};
 expect(mergeRealtimeTasks(s.realtime!.tasks,[stale])[0].status).toBe('running');
 s=chatReducer(s,{type:'record-task',task:stale});
 expect(s.realtime?.tasks[0].status).toBe('running');
});
it('Stop marks cancelling without allowing late REST running to revert it',()=>{
 let s=apply(initialChatState(),'REALTIME_RESET',{...reset,business_state:actualBusiness},id(1));
 s=apply(s,'REALTIME_EVENT',businessEvent('CUSTOM',6,{name:'TASK_CANCEL_REQUESTED',value:{taskId:'t',status:'cancelling'}}),id(2));
 expect(s.realtime?.tasks[0].status).toBe('cancelling');
 expect(s.run).toMatchObject({active:true,status:'cancelling'});
 const stale:Task={...actualBusiness.tasks[0],status:'running',approval_profile:'standard',execution_phase:null};
 expect(mergeRealtimeTasks(s.realtime!.tasks,[stale])[0].status).toBe('cancelling');
});
it('queued cancellation retains cancelled status from the real control event',()=>{
 let s=apply(initialChatState(),'REALTIME_RESET',{...reset,business_state:{...actualBusiness,pending_confirm:null,tasks:[{...actualBusiness.tasks[0],status:'created',run_id:null}]}},id(1));
 s=apply(s,'REALTIME_EVENT',businessEvent('CUSTOM',6,{name:'TASK_CANCEL_REQUESTED',value:{taskId:'t',status:'cancelled'}}),id(2));
 expect(s.realtime?.tasks[0].status).toBe('cancelled');
 expect(s.run.active).toBe(false);
});


it('projects confirmed incomplete content without clearing it on a later delta', () => {
  let s = apply(initialChatState(), 'REALTIME_RESET', { ...reset, sync_status: 'incomplete',
    snapshots: [{ ...snapshot, phase: 'partial', incomplete: true }], business_state: actualBusiness }, id(3));
  expect(s.items.find(i => i.id === mid)).toMatchObject({ incomplete: true, taskId: 't', runId: 'r' });
  const approval = s.pendingConfirm;
  s = apply(s, 'REALTIME_EVENT', body('CONTENT', 2, '!'), id(4));
  expect(s.items.find(i => i.id === mid)).toMatchObject({ text: '你😀!', incomplete: true });
  expect(s.realtime?.sync).toBe('incomplete');
  expect(s.pendingConfirm).toEqual(approval);
});

it('projects incomplete empty text and removes its notice on an authoritative complete reset', () => {
  const s = apply(initialChatState(), 'REALTIME_RESET', { ...reset, sync_status: 'incomplete',
    snapshots: [{ ...snapshot, phase: 'interrupted', incomplete: true, blocks: [] }] }, id(3));
  expect(s.items.find(i => i.id === mid)).toMatchObject({ text: '', incomplete: true, done: true });
  const restored = apply(s, 'REALTIME_RESET', { ...reset, snapshots: [{ ...snapshot,
    phase: 'final', incomplete: false, blocks: [{ ...snapshot.blocks[0], closed: true }] }] }, id(4));
  expect(restored.items.find(i => i.id === mid)).toMatchObject({ text: '你😀', incomplete: false });
  expect(restored.items.filter(i => i.id === mid)).toHaveLength(1);
  expect(restored.realtime?.sync).toBe('synced');
});

it('keeps an unassigned incomplete history notice across new healthy output', () => {
  let s = apply(initialChatState(), 'REALTIME_RESET', { ...reset, sync_status: 'incomplete' }, id(1));
  s = apply(s, 'REALTIME_EVENT', body('START'), id(2));
  s = apply(s, 'REALTIME_EVENT', body('CONTENT'), id(3));
  expect(s.realtime?.sync).toBe('incomplete');
  expect(s.items.find(i => i.id === mid)).toMatchObject({ incomplete: false, text: '你😀' });
});

it('preserves persisted process boundaries on historical reset messages', () => {
  const s = apply(boot(), 'REALTIME_RESET', { ...reset, business_state: {
    ...reset.business_state, messages: [{ id: 'history', role: 'assistant', task_id: 'old',
      content: { text: '查询完成答案', presentation: { version: 1, process_end: 4 } } }],
  } }, id(7));
  expect(s.items[0]).toMatchObject({ presentation: { version: 1, process_end: 4 } });
});

it('recovers atomically when business identity history exceeds its 8192-event bound', () => {
  let s = boot();
  // Seed the already-applied identities; avoid making this regression itself
  // spend quadratic time constructing thousands of unrelated UI transitions.
  s = {...s,realtime:{...s.realtime!,seen:Object.fromEntries(Array.from({length:8192},(_,i)=>[`event-${i}`,true as const]))}};
  const overflow = {schema_version:1,event_id:'event-8192',conversation_id:cid,source:'business',business_seq:8198,payload:{type:'TOOL_CALL_ARGS',toolCallId:'write',delta:'suffix'}};
  s = apply(s,'REALTIME_EVENT',overflow,id(9000));
  expect(s.realtime?.needsReset).toBe(true);
  expect(s.realtime?.cursor).toBe('');
  // The fresh server cut includes the completed task and every durable detail.
  s = apply(s,'REALTIME_RESET',{...reset,business_seq:9000,business_state:{...reset.business_state,tasks:[{...task('t','2026-10-08T00:00:00Z'),status:'completed'}]}},id(10000));
  expect(s.realtime?.needsReset).toBe(false);
  expect(s.realtime?.tasks[0].status).toBe('completed');
  expect(s.realtime?.seen).toEqual({});
  s = apply(s,'REALTIME_EVENT',overflow,id(10001));
  expect(s.realtime?.needsReset).toBe(false);
  expect(s.realtime?.cursor).toBe(id(10001));
});

it('restores private tool content descriptors and task identity from historical reset', () => {
  const payload = { toolCallId: 'call', kind: 'arguments', sha256: 'a'.repeat(64), sizeBytes: 90000, preview: 'preview', incomplete: false };
  const activity = [
    { id: 1, seq: 1, task_id: 't', event_type: 'tool_call', payload: {toolCallId:'call',tool:'Write'}, created_at:'2026-10-08' },
    { id: 2, seq: 2, task_id: 't', event_type: 'tool_content', payload, created_at:'2026-10-08' },
    { id: 3, seq: 3, task_id: 't', event_type: 'tool_result', payload: {toolCallId:'call',tool:'Write',status:'success'}, created_at:'2026-10-08' },
  ];
  const s = apply(boot(), 'REALTIME_RESET', { ...reset, business_state: { ...reset.business_state, tasks: [task('t','2026-10-08')], activity } }, id(9));
  expect(s.realtime?.needsReset).toBeFalsy();
  expect(s.items[0]).toMatchObject({kind:'tool',taskId:'t',call:{args:'preview',state:'success'},content:{arguments:{sha256:payload.sha256,sizeBytes:90000,preview:'preview',incomplete:false}}});
});

it('applies authoritative quarantine without a forged final snapshot and retains uncertain tools', () => {
  let s = apply(initialChatState(), 'REALTIME_RESET', {...reset,business_state:{...reset.business_state,tasks:[{...task('t','2026-10-08T00:00:00Z'),run_id:'r'}]}},id(1));
  s = apply(s,'REALTIME_EVENT',body('START'),id(2));
  s = apply(s,'REALTIME_EVENT',body('CONTENT'),id(3));
  s = apply(s,'REALTIME_EVENT',businessEvent('TOOL_CALL_START',6,{toolCallId:'uncertain',toolCallName:'Write'}),id(4));
  s = apply(s,'REALTIME_EVENT',businessEvent('RUN_ERROR',7,{code:'runtime_execution_quarantined',message:'执行结果未知',context:{resumable:false}}),id(5));
  expect(s.realtime?.needsReset).toBe(false);
  expect(s.realtime?.tasks[0].status).toBe('failed');
  expect(s.items.find(i=>i.kind==='tool')).toMatchObject({call:{state:'unknown'}});
  expect(s.items.find(i=>i.kind==='text'&&i.role==='assistant')).toMatchObject({text:'你😀',incomplete:true});
  expect(s.pendingConfirm).toBeNull();
});

it('cold reset preserves successful tools and marks only missing outcomes unknown for terminal task',()=>{
  const state = apply(initialChatState(),'REALTIME_RESET',{...reset,business_state:{...reset.business_state,tasks:[{...task('t','2026-10-08T00:00:00Z'),status:'failed',error:{code:'runtime_execution_quarantined'}}],activity:[
    {id:1,seq:1,task_id:'t',event_type:'tool_call',payload:{toolCallId:'done',tool:'Bash'},created_at:'2026-10-08'},
    {id:2,seq:2,task_id:'t',event_type:'tool_result',payload:{toolCallId:'done',tool:'Bash',status:'success'},created_at:'2026-10-08'},
    {id:3,seq:3,task_id:'t',event_type:'tool_call',payload:{toolCallId:'unknown',tool:'Write'},created_at:'2026-10-08'},
  ]}},id(1));
  expect(state.realtime?.needsReset).toBe(false);
  expect(state.items.filter(i=>i.kind==='tool').map(i=>i.call.state)).toEqual(['success','unknown']);
});

it('preserves a complete final display when execution is later quarantined',()=>{
  const final={...snapshot,phase:'final',incomplete:false,blocks:snapshot.blocks.map(b=>({...b,closed:true}))};
  const state=apply(initialChatState(),'REALTIME_RESET',{...reset,snapshots:[final],business_state:{...reset.business_state,tasks:[{...task('t','2026-10-08T00:00:00Z'),status:'failed',error:{code:'runtime_execution_quarantined'}}]}},id(3));
  expect(state.realtime?.needsReset).toBe(false);
  expect(state.items.find(i=>i.kind==='text')).toMatchObject({done:true,incomplete:false,text:'你😀'});
});
