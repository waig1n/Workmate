import asyncio
import importlib.util
from pathlib import Path
from collections import Counter

import pytest

PATH=Path(__file__).resolve().parents[2]/'scripts/reliability/capacity_support/client.py'
spec=importlib.util.spec_from_file_location('capacity_client',PATH)
client=importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def test_frozen_mix_and_percentiles():
    assert Counter(client.case_for(i,'mixed') for i in range(20))==dict(text=8,file=5,skill=2,mcp=1,rag=2,approval=1,stop=1)
    assert client.percentile(list(range(1,21)),.95)==19
    assert client.percentile([],.95) is None


@pytest.mark.parametrize('url',['http://example.com:80','postgresql://user@10.0.0.1/db','redis://127.0.0.1:6379/0?host=example.com','http://localhost.evil:8080'])
def test_reject_non_loopback_or_overrides(url):
    with pytest.raises(ValueError):client.loopback_url(url)


def test_loopback_ipv4_ipv6():
    assert client.loopback_url('http://127.0.0.1:8080')
    assert client.loopback_url('redis://[::1]:6379/0')


async def test_closed_admission_is_bounded_and_retains_failures():
    active=maximum=0
    async def operation(index):
        nonlocal active,maximum
        active+=1;maximum=max(maximum,active)
        try:
            await asyncio.sleep(.001)
            if index==2:raise ValueError('expected')
            return {'index':index,'correct':True}
        finally:active-=1
    rows=await client.closed_schedule(8,2,operation)
    assert maximum==2 and len(rows)==8
    assert sum(row['correct'] for row in rows)==7
    assert rows[2]['error_type']=='ValueError'


def test_budget_removes_human_wait_only_from_completion():
    rows=[dict(correct=True,first_body_seconds=9,elapsed_seconds=64,human_wait_seconds=5,workload='approval')]
    result=client.budgets(rows)
    assert result['passed'] and result['completion_p95_seconds']==59
    rows[0]['first_body_seconds']=11
    assert not client.budgets(rows)['passed']


async def test_open_arrivals_drop_at_bound_without_semaphore_delay():
    async def operation(index):
        await asyncio.sleep(.025)
        return dict(correct=True,index=index)
    rows=await client.open_schedule(6,1000,.1,operation,max_outstanding=2)
    assert len(rows)==6
    assert any(r.get('invalid') and r['error_type']=='OutstandingLimit' for r in rows)
    assert all('planned_monotonic' in r and 'send_lateness_seconds' in r for r in rows)


@pytest.mark.parametrize('terminal',['RUN_FINISHED','RUN_CANCELLED','RUN_ERROR'])
async def test_consume_recognizes_all_product_terminals(tmp_path,terminal):
    import json
    class Response:
        status_code=200
        async def aiter_lines(self):
            for frame in [dict(type='CUSTOM',name='REALTIME_RESET'),dict(type='CUSTOM',name='REPLAY_DONE'),
                          dict(type='RUN_STARTED',runId='reply'),dict(type=terminal,runId='reply')]:
                yield 'data: '+json.dumps(frame)
    class Stream:
        async def __aenter__(self):return Response()
        async def __aexit__(self,*args):pass
    class Http:
        def stream(self,*args):return Stream()
    ready=asyncio.Event();admitted=asyncio.Event()
    result=await client.consume(Http(),'cid',0,{},ready,admitted,tmp_path)
    assert ready.is_set() and result['events'][-1]['type']==terminal
    assert result['body']=='' and result['first'] is None
    assert result['reset_count']==1


async def test_consume_rejects_text_offset_gap(tmp_path):
    import json
    class Response:
        status_code=200
        async def aiter_lines(self):
            for frame in [dict(name='REPLAY_DONE'),dict(name='REALTIME_EVENT',value=dict(task_id='task',text=dict(from_offset=1),payload=dict(type='TEXT_MESSAGE_CONTENT',delta='x')))]:
                yield 'data: '+json.dumps(frame)
    class Stream:
        async def __aenter__(self):return Response()
        async def __aexit__(self,*args):pass
    class Http:
        def stream(self,*args):return Stream()
    admitted=asyncio.Event();admitted.set()
    with pytest.raises(AssertionError,match='offset'):
        await client.consume(Http(),'cid',0,{'id':'task'},asyncio.Event(),admitted,tmp_path)


def test_cli_exposes_sse_activity_and_open_rate():
    args=client.build_parser().parse_args(['--base-url','http://127.0.0.1:1','--dsn','postgresql://127.0.0.1/db','--redis-url','redis://127.0.0.1:2/0','--output','/tmp/capacity','--mode','sse','--connections','200','--sse-activity','active'])
    assert args.connections==200 and args.duration==900 and args.sse_activity=='active'


async def test_stop_signal_requires_actual_output_not_tool_arguments(tmp_path):
    import json
    signal=asyncio.Event()
    class Response:
        status_code=200
        async def aiter_lines(self):
            yield 'data: '+json.dumps(dict(name='REPLAY_DONE'))
            yield 'data: '+json.dumps(dict(type='TOOL_CALL_ARGS',delta='CAPACITY_STOP_STARTED:key'))
            assert not signal.is_set()
            yield 'data: '+json.dumps(dict(type='CUSTOM',name='TOOL_RESULT_DELTA',value=dict(delta='CAPACITY_STOP_')))
            assert not signal.is_set()
            yield 'data: '+json.dumps(dict(type='CUSTOM',name='TOOL_RESULT_DELTA',value=dict(delta='STARTED:key')))
            assert signal.is_set()
            yield 'data: '+json.dumps(dict(type='RUN_CANCELLED'))
    class Stream:
        async def __aenter__(self):return Response()
        async def __aexit__(self,*args):pass
    class Http:
        def stream(self,*args):return Stream()
    await client.consume(Http(),'cid',0,{'key':'key'},asyncio.Event(),asyncio.Event(),tmp_path,signal)
    assert signal.is_set()


def test_text_32_seconds_is_hard_budget_but_mixed_uses_60():
    rows=[dict(correct=True,first_body_seconds=1,elapsed_seconds=33,workload='text')]
    assert not client.budgets(rows,workload='text')['passed']
    assert client.budgets(rows,workload='mixed')['passed']


def test_sse_shared_conversations_keep_execution_concurrency_four():
    for connections in (50,100,200):
        assignments=client.sse_assignments(connections)
        assert len(assignments)==connections
        assert set(assignments)==set(range(4))
        assert min(Counter(assignments).values())>=2


def test_single_text_deadline_miss_fails_even_when_p95_is_under_32():
    rows=[dict(correct=True,first_body_seconds=1,elapsed_seconds=20,workload='text') for _ in range(20)]
    rows[-1]['elapsed_seconds']=33
    result=client.budgets(rows,workload='text')
    assert result['text_baseline_p95_seconds']==20 and not result['passed']


async def test_standard_tool_confirmation_has_no_human_delay():
    class Response:
        status_code=200
        def json(self):return {'decisionVersion':1,'toolCalls':[{'id':'tool','name':'Bash'}]}
    finished=asyncio.Event()
    consumer=asyncio.create_task(finished.wait())
    class Http:
        async def get(self,*args):return Response()
        async def post(self,*args,**kwargs):
            assert kwargs['json']['decisions']==[dict(tool_call_id='tool',approved=True)]
            finished.set();return Response()
    record={}
    await client.approve_available(Http(),'task',[consumer],record,tool='Bash')
    await consumer
    assert record['automatic_approvals']==[dict(decision_version=1,tool='Bash')]
    assert 'human_wait_seconds' not in record


def test_file20_is_only_smoke_and_has_independent_budget():
    row=dict(correct=True,workload='file20',first_body_seconds=11,elapsed_seconds=110)
    assert client.budgets([row],workload='file20')['passed']
    row['elapsed_seconds']=121
    assert not client.budgets([row],workload='file20')['passed']
    assert len(client.expected_file('a'*32,20*1024*1024))==20*1024*1024


def test_client_validity_enforces_lateness_lag_and_drops():
    assert client.sample_validity([{'send_lateness_seconds':.5}],.1,mode='open')['valid']
    assert not client.sample_validity([{'send_lateness_seconds':.501}],0,mode='open')['valid']
    assert not client.sample_validity([], .101, mode='sse')['valid']
    assert not client.sample_validity([{'invalid':True}],0,mode='open')['valid']


def test_reader_delivery_metrics_use_each_local_reader_interval():
    result=client.reader_delivery_metrics([
        dict(started=100,first=101,terminal=115),
        dict(started=100.1,first=102,terminal=115.4),
    ])
    assert result['first_body_to_terminal_seconds']==14
    assert result['readers_terminal_spread_seconds']==pytest.approx(.4)
    assert result['reader_timings'][1]['run_started_monotonic']==100.1
    assert result['reader_timings'][1]['first_body_to_terminal_seconds']==pytest.approx(13.4)


def test_reader_without_body_has_no_invented_delivery_duration():
    result=client.reader_delivery_metrics([dict(started=1,first=None,terminal=3)])
    assert result['first_body_to_terminal_seconds'] is None
    assert result['reader_timings'][0]['first_body_monotonic'] is None


def test_pure_text_queues_over_ten_seconds_but_meets_32_second_deadline():
    rows=[dict(correct=True,first_body_seconds=13,elapsed_seconds=30,workload='text')]
    assert client.budgets(rows,workload='text')['passed']
    assert not client.budgets(rows,workload='mixed')['passed']


async def test_stop_waits_for_real_marker_and_rejects_completed_tool():
    class Response:
        status_code=200
        def json(self):return dict(key='key',started_count=1,completed_exists=False,result_exists=False)
    class Http:
        async def get(self,path):
            assert path=='/__capacity/stop/key';return Response()
    proof=await client.wait_stop_marker(Http(),'key')
    assert proof['started_count']==1
    Response.json=lambda self:dict(key='key',started_count=1,completed_exists=True,result_exists=True)
    with pytest.raises(AssertionError,match='completed'):
        await client.wait_stop_marker(Http(),'key')


def test_smoke_case_selector_accepts_stop():
    args=client.build_parser().parse_args(['--base-url','http://127.0.0.1:1','--dsn','postgresql://127.0.0.1/db','--redis-url','redis://127.0.0.1:2/0','--output','/tmp/capacity','--case','stop'])
    assert args.case=='stop' and args.mode=='smoke'


@pytest.mark.parametrize('phase',['final','interrupted'])
def test_snapshot_exact_blocks_preserve_trailing_newline_but_text_is_normalized(phase):
    body=' CAPACITY stop fixture starting\n'
    content=dict(text=body.strip(),blocks=[dict(kind='text',text=body)])
    client.verify_snapshot_body(content,phase,body)
    with pytest.raises(AssertionError,match='blocks'):
        client.verify_snapshot_body(content,phase,body.rstrip())
    content['text']=body
    with pytest.raises(AssertionError,match='normalized'):
        client.verify_snapshot_body(content,phase,body)


def test_nonterminal_snapshot_text_is_not_stripped():
    body='partial\n'
    client.verify_snapshot_body(dict(text=body,blocks=[dict(kind='text',text=body)]),'approval',body)


async def test_admission_wait_never_posts_early_or_catches_up_late():
    import time
    due=time.monotonic()+.02
    await client.wait_admission(due)
    assert time.monotonic()>=due
    with pytest.raises(TimeoutError,match='deadline'):
        await client.wait_admission(time.monotonic()-1)


def test_reader_metrics_keep_connection_and_reset_counts():
    result=client.reader_delivery_metrics([dict(started=1,first=2,terminal=3,reset_count=1),dict(started=1,first=2,terminal=3,reset_count=2)])
    assert result['sse_connection_count']==2 and result['sse_reset_count_total']==3
    assert [r['reset_count'] for r in result['reader_timings']]==[1,2]


def reset_fixture(text='你好',epoch=4,reply='reply',task='task'):
    from workmate_contracts.realtime import message_id
    generation='6f3b777e-3921-4d92-b4bf-6a725e63c31e'
    return dict(conversation_id='cid',generation=generation,reason='content_gap',business_seq=24,
        sync_status='synced',business_state={'tasks':[{'id':task,'run_id':reply}]},snapshots=[dict(
        message_id=message_id(task,reply),conversation_id='cid',task_id=task,reply_id=reply,
        execution_epoch=epoch,snapshot_version=1,business_seq=24,phase='partial',incomplete=False,
        covered_cursor=dict(conversation_id='cid',generation=generation,stream_id='12-0'),
        blocks=[dict(block_id='text',kind='text',text=text,content_version=1,closed=False)])])


async def replay_frames(tmp_path,frames):
    import json
    class Response:
        status_code=200
        async def aiter_lines(self):
            for frame in frames:yield 'data: '+json.dumps(frame)
    class Stream:
        async def __aenter__(self):return Response()
        async def __aexit__(self,*args):pass
    class Http:
        def stream(self,*args):return Stream()
    admitted=asyncio.Event();admitted.set()
    return await client.consume(Http(),'cid',0,{'id':'task'},asyncio.Event(),admitted,tmp_path)


async def test_partial_reset_restores_current_body_and_resume_epoch(tmp_path):
    frames=[dict(name='REALTIME_RESET',value=reset_fixture('OLD',task='old')),
        dict(name='REPLAY_DONE'),dict(name='REALTIME_EVENT',value=dict(task_id='task',reply_id='reply',
            execution_epoch=3,payload=dict(type='RUN_STARTED',runId='reply'))),
        dict(name='REALTIME_RESET',value=reset_fixture('OLD',task='old')),
        dict(name='REALTIME_RESET',value=reset_fixture()),
        dict(name='REALTIME_EVENT',value=dict(task_id='task',reply_id='reply',execution_epoch=4,
            text=dict(from_offset=6),payload=dict(type='TEXT_MESSAGE_CONTENT',delta='!'))),
        dict(type='RUN_FINISHED',runId='reply')]
    result=await replay_frames(tmp_path,frames)
    assert result['body']=='你好!' and result['first'] is not None
    assert result['reset_count']==3


@pytest.mark.parametrize('change',['reply','epoch','prefix','cursor','scope','missing','run_id','sync','generation'])
async def test_partial_reset_rejects_conflicting_authority(tmp_path,change):
    reset=reset_fixture('你好!')
    if change=='reply':reset=reset_fixture('你好!',reply='other')
    if change=='epoch':reset['snapshots'][0]['execution_epoch']=2
    if change=='prefix':reset['snapshots'][0]['blocks'][0]['text']='错'
    if change=='cursor':reset['snapshots'][0]['covered_cursor']=None
    if change=='scope':reset['conversation_id']='other'
    if change=='missing':reset=reset_fixture('OLD',task='old')
    if change=='run_id':reset['business_state']['tasks'][0]['run_id']='other'
    if change=='sync':reset['sync_status']='incomplete'
    if change=='generation':reset['snapshots'][0]['covered_cursor']['generation']='00000000-0000-4000-8000-000000000000'
    frames=[dict(name='REPLAY_DONE'),dict(name='REALTIME_EVENT',value=dict(task_id='task',reply_id='reply',
        execution_epoch=3,text=dict(from_offset=0),payload=dict(type='TEXT_MESSAGE_CONTENT',delta='你好'))),
        dict(name='REALTIME_RESET',value=reset),dict(type='RUN_FINISHED',runId='reply')]
    with pytest.raises((AssertionError,ValueError)):
        await replay_frames(tmp_path,frames)


@pytest.mark.parametrize('offset',[6,9])
async def test_reset_extends_existing_prefix_but_keeps_delta_offsets_strict(tmp_path,offset):
    frames=[dict(name='REPLAY_DONE'),dict(name='REALTIME_EVENT',value=dict(task_id='task',reply_id='reply',
        execution_epoch=3,text=dict(from_offset=0),payload=dict(type='TEXT_MESSAGE_CONTENT',delta='你'))),
        dict(name='REALTIME_RESET',value=reset_fixture()),
        dict(name='REALTIME_EVENT',value=dict(task_id='task',reply_id='reply',execution_epoch=4,
            text=dict(from_offset=offset),payload=dict(type='TEXT_MESSAGE_CONTENT',delta='!'))),
        dict(type='RUN_FINISHED',runId='reply')]
    if offset==6:
        assert (await replay_frames(tmp_path,frames))['body']=='你好!'
    else:
        with pytest.raises(AssertionError,match='offset'):await replay_frames(tmp_path,frames)
