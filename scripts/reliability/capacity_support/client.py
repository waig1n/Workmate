"""Independent, bounded CAPACITY HTTP/SSE client. Never launches or mutates services."""
import argparse
import asyncio
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import resource
import time
from urllib.parse import urlsplit
import uuid

CASES=('text','file','skill','text','rag','file','text','approval','file','text',
       'mcp','text','file','rag','text','skill','file','text','stop','text')


def case_for(index,workload):
    return CASES[index%20] if workload=='mixed' else 'text'


def percentile(values,fraction):
    return sorted(values)[max(0,math.ceil(len(values)*fraction)-1)] if values else None


def loopback_url(value):
    parsed=urlsplit(value)
    if parsed.query or parsed.fragment or parsed.hostname not in ('localhost',):
        try:
            if parsed.query or parsed.fragment or not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError('Only explicit loopback tunnel URLs without query overrides are allowed')
        except (TypeError,ValueError) as error:
            raise ValueError('Only explicit loopback tunnel URLs without query overrides are allowed') from error
    return value


def budgets(rows,*,workload=None):
    workload=workload or ('text' if all(r.get('workload')=='text' for r in rows) else 'mixed')
    good=[r for r in rows if r.get('correct')]
    if workload=='file20':
        return dict(boundary='20MiB',deadline_seconds=120,passed=bool(rows) and len(good)==len(rows) and all(r['elapsed_seconds']<=120 for r in good))
    first=percentile([r['first_body_seconds'] for r in good if r.get('first_body_seconds') is not None],.95)
    completion=percentile([r['elapsed_seconds']-r.get('human_wait_seconds',0) for r in good],.95)
    text=percentile([r['elapsed_seconds'] for r in good if r['workload']=='text'],.95)
    text_pass=bool(good) and all(r['elapsed_seconds']<=32 for r in good)
    policy_pass=text_pass if workload=='text' else first is not None and first<=10 and completion is not None and completion<=60
    return dict(first_body_p95_seconds=first,completion_p95_seconds=completion,
                text_baseline_p95_seconds=text,text_baseline_32s=text_pass if workload=='text' else None,
                policy='each_text_task_32s' if workload=='text' else 'mixed_first_p95_10s_completion_without_human_wait_p95_60s',
                passed=bool(rows) and len(good)==len(rows) and policy_pass)



def reader_delivery_metrics(streams):
    """Compare only timestamps captured by this sender's monotonic clock.

    Body-to-terminal includes controlled generation and product delivery work;
    it is not a measurement of pure network latency.
    """
    timings=[]
    items=streams.items() if isinstance(streams,dict) else enumerate(streams)
    for index,stream in items:
        first=stream.get('first');terminal=stream['terminal']
        timings.append(dict(reader_index=index,run_started_monotonic=stream.get('started'),reset_count=stream.get('reset_count',0),
                            first_body_monotonic=first,terminal_monotonic=terminal,
                            first_body_to_terminal_seconds=terminal-first if first is not None else None))
    durations=[row['first_body_to_terminal_seconds'] for row in timings if row['first_body_to_terminal_seconds'] is not None]
    ends=[row['terminal_monotonic'] for row in timings]
    return dict(reader_timings=timings,sse_connection_count=len(timings),sse_reset_count_total=sum(row['reset_count'] for row in timings),first_body_to_terminal_seconds=max(durations) if durations else None,
                readers_terminal_spread_seconds=max(ends)-min(ends) if ends else None)


def verify_snapshot_body(content,phase,delivered):
    """Exact blocks are authoritative; Product terminal display text is stripped.

    Matches persistence/repositories/message_snapshots.py write_message without
    weakening detection of whitespace loss in delivered or persisted blocks.
    """
    exact=''.join(block['text'] for block in content['blocks'] if block['kind']=='text')
    assert exact==delivered,'PG snapshot blocks differ from SSE body'
    normalized=exact.strip() if phase in ('final','interrupted') else exact
    assert content['text']==normalized,'PG normalized display text differs from product policy'


def expected_file(key,size):
    pattern=(key+'\n').encode()
    return (pattern*((size//len(pattern))+1))[:size]


def sample_validity(rows,loop_lag_p95,*,mode):
    reasons=[]
    if any(row.get('invalid') for row in rows):reasons.append('outstanding_limit_drop')
    if mode=='open' and any(row.get('send_lateness_seconds',0)>.5 for row in rows):reasons.append('send_lateness_over_0.5s')
    if loop_lag_p95 is not None and loop_lag_p95>.1:reasons.append('client_loop_lag_p95_over_0.1s')
    return dict(valid=not reasons,reasons=reasons,send_lateness_limit_seconds=.5,loop_lag_p95_limit_seconds=.1)


async def closed_schedule(count,concurrency,operation):
    semaphore=asyncio.Semaphore(concurrency)
    async def one(index):
        async with semaphore:
            try:return await operation(index)
            except Exception as error:return dict(index=index,correct=False,error_type=type(error).__name__,error=str(error))
    return await asyncio.gather(*(one(i) for i in range(count)))


async def open_schedule(count,rate,duration,operation,*,max_outstanding=64):
    """Pace arrivals independently of completions; drop instead of queueing."""
    start=time.monotonic();pending=set();rows=[]
    async def launch(index,planned,launched):
        try:row=await operation(index)
        except Exception as error:row=dict(index=index,correct=False,error_type=type(error).__name__,error=str(error))
        row.update(planned_monotonic=planned,launch_lateness_seconds=launched-planned,
                   send_lateness_seconds=max(0,row.get('submitted_monotonic',launched)-planned))
        return row
    try:
        for index in range(count):
            planned=start+index/rate
            if planned>=start+duration:break
            await asyncio.sleep(max(0,planned-time.monotonic()))
            done={task for task in pending if task.done()}
            rows.extend(task.result() for task in done);pending-=done
            launched=time.monotonic()
            if len(pending)>=max_outstanding:
                rows.append(dict(index=index,correct=False,invalid=True,error_type='OutstandingLimit',
                                 planned_monotonic=planned,send_lateness_seconds=launched-planned))
                continue
            pending.add(asyncio.create_task(launch(index,planned,launched)))
        rows.extend(await asyncio.gather(*pending))
        return sorted(rows,key=lambda row:row['index'])
    finally:
        for task in pending:
            if not task.done():task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)


def write(path,value):
    path.write_text(json.dumps(value,indent=2,default=str))


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('base-url','dsn','redis-url','output'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--namespace',default=os.getenv('WORKMATE_LOAD_NAMESPACE'))
    parser.add_argument('--mode',choices=('smoke','closed','open','sse'),default='smoke')
    parser.add_argument('--workload',choices=('text','mixed'),default='text')
    parser.add_argument('--case',choices=('file20','stop'),default=None)
    parser.add_argument('--concurrency',type=int,choices=(1,2,4,8),default=1)
    parser.add_argument('--tasks',type=int,default=20)
    parser.add_argument('--repeat',type=int,default=1)
    parser.add_argument('--connections',type=int,choices=(50,100,200),default=50)
    parser.add_argument('--duration',type=float,default=900)
    parser.add_argument('--arrival-rate',type=float,default=1)
    parser.add_argument('--sse-activity',choices=('idle','active'),default='idle')
    return parser


def restore_reset(value,cid,task_id,body,reply,epoch):
    """Conservative verifier: authoritative resets may extend, never erase evidence.

    The UI can atomically replace its whole conversation. This load verifier
    intentionally rejects prefix truncation instead of claiming text loss.
    """
    from workmate_contracts.realtime import RealtimeReset
    reset=RealtimeReset.model_validate(value)
    assert reset.conversation_id==cid,'Reset Conversation mismatch'
    matches=[s for s in reset.snapshots if s.task_id==task_id]
    assert len(matches)<=1,'Ambiguous current Task snapshot'
    if not matches:
        assert not body,'Reset omitted delivered Task body (verifier cannot replay truncation)'
        return body,reply,epoch
    snapshot=matches[0]
    tasks=[t for t in reset.business_state.get('tasks',[]) if t.get('id')==task_id]
    assert len(tasks)==1 and tasks[0].get('run_id')==snapshot.reply_id,'Reset Task/Reply authority mismatch'
    assert reply is None or snapshot.reply_id==reply,'Reset Reply mismatch'
    assert snapshot.execution_epoch>=epoch,'Reset execution epoch regressed'
    assert reset.sync_status=='synced' and not snapshot.incomplete,'Reset is not a complete authoritative cut'
    cursor=snapshot.covered_cursor
    assert cursor is not None and cursor.generation==reset.generation,'Reset lacks covered cursor'
    updated=''.join(b.text for b in snapshot.blocks if b.kind=='text')
    assert updated.startswith(body),'Reset contradicts delivered prefix (verifier cannot replay truncation)'
    return updated,snapshot.reply_id,snapshot.execution_epoch


async def consume(http,cid,index,record,ready,admitted,output,stop_started=None):
    body='';events=[];first=None;tool_output='';started=None;reset_count=0;reply=None;epoch=0
    with (output/f'sse-{cid}-{index}.jsonl').open('w') as trace:
        async with http.stream('GET',f'/api/tasks/conversations/{cid}/events') as response:
            if response.status_code!=200:raise AssertionError(f'SSE HTTP {response.status_code}')
            async for line in response.aiter_lines():
                if not line.startswith('data:'):continue
                frame=json.loads(line[5:].lstrip());now=time.monotonic()
                trace.write(json.dumps({'monotonic':now,'frame':frame})+'\n')
                name=frame.get('name');kind=frame.get('type')
                if name=='REALTIME_RESET':reset_count+=1
                if name=='REPLAY_DONE':ready.set();continue
                if not ready.is_set():continue
                if name=='REALTIME_RESET':
                    await admitted.wait()
                    body,reply,epoch=restore_reset(frame['value'],cid,record['id'],body,reply,epoch)
                    if body and first is None:first=now
                    continue
                if name in ('REALTIME_EVENT','REALTIME_SNAPSHOT'):
                    await admitted.wait()
                    value=frame['value']
                    if value.get('task_id')!=record['id']:continue
                    incoming_reply=value.get('reply_id')
                    if incoming_reply is not None:
                        assert reply is None or incoming_reply==reply,'Realtime Reply mismatch'
                        reply=incoming_reply
                    incoming_epoch=value.get('execution_epoch')
                    if incoming_epoch is not None:
                        assert incoming_epoch>=epoch,'Realtime execution epoch regressed'
                        epoch=incoming_epoch
                    if name=='REALTIME_SNAPSHOT':
                        snapshot=''.join(b['text'] for b in value['blocks'] if b['kind']=='text')
                        if snapshot!=body:raise AssertionError('SSE snapshot differs from delivered deltas')
                        continue
                    frame=value['payload'];kind=frame['type']
                    if kind=='TEXT_MESSAGE_CONTENT' and value['text']['from_offset']!=len(body.encode()):
                        raise AssertionError('Duplicate/missing text offset')
                if frame.get('name')=='TOOL_RESULT_DELTA':
                    tool_output+=frame.get('value',{}).get('delta','')
                    if stop_started is not None and 'CAPACITY_STOP_STARTED:'+record['key'] in tool_output:
                        record['stop_started_observed_monotonic']=now
                        stop_started.set()
                if kind=='TEXT_MESSAGE_CONTENT':
                    if first is None:first=now
                    body+=frame['delta']
                if kind=='RUN_STARTED' and started is None:started=now
                if kind in ('RUN_STARTED','RUN_FINISHED','RUN_ERROR','RUN_CANCELLED'):
                    events.append(frame)
                if kind in ('RUN_FINISHED','RUN_ERROR','RUN_CANCELLED'):
                    return dict(body=body,events=events,first=first,terminal=now,started=started,reset_count=reset_count)
    raise AssertionError('SSE ended without terminal')


async def wait_stop_marker(http,key):
    """Read the isolated gateway's authenticated real-workspace marker proof."""
    async with asyncio.timeout(40):
        while True:
            response=await http.get('/__capacity/stop/'+key)
            assert response.status_code==200,f'Stop marker HTTP {response.status_code}'
            proof=response.json()
            assert proof.get('key')==key,'Stop marker belongs to another fixture key'
            assert type(proof.get('started_count')) is int and proof['started_count'] in (0,1),'Duplicate/invalid real start marker'
            assert type(proof.get('completed_exists')) is bool and type(proof.get('result_exists')) is bool,'Invalid real marker evidence'
            assert not proof['completed_exists'] and not proof['result_exists'],'Stop fixture already completed before cancellation'
            if proof['started_count']==1:return proof
            await asyncio.sleep(.1)


async def approve_available(http,tid,consumers,record,*,tool):
    """Real standard-policy approval without an artificial human delay."""
    seen=set();record['automatic_approvals']=[]
    while not all(task.done() for task in consumers):
        response=await http.get('/api/tasks/'+tid+'/pending-confirm')
        if response.status_code==200:
            pending=response.json();version=pending['decisionVersion']
            if version not in seen:
                calls=pending['toolCalls']
                assert len(calls)==1 and calls[0]['name']==tool,'Unexpected tool requesting fixture approval'
                result=await http.post('/api/tasks/'+tid+'/confirm',json=dict(decision_version=version,
                    decisions=[dict(tool_call_id=calls[0]['id'],approved=True)]))
                assert result.status_code==200,f'Automatic confirmation HTTP {result.status_code}'
                seen.add(version);record['automatic_approvals'].append(dict(decision_version=version,tool=tool))
        else:assert response.status_code==404,f'Pending approval HTTP {response.status_code}'
        await asyncio.sleep(.1)


async def sample_client(halt,output,samples):
    previous=time.monotonic();cpu=time.process_time()
    with (output/'client-samples.jsonl').open('w') as trace:
        while not halt.is_set():
            try:await asyncio.wait_for(halt.wait(),.5)
            except TimeoutError:pass
            now=time.monotonic();used=time.process_time()
            row=dict(monotonic=now,cpu_seconds=used,cpu_fraction=(used-cpu)/(now-previous),
                     max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                     loop_lag_seconds=max(0,now-previous-.5))
            samples.append(row);trace.write(json.dumps(row)+'\n');trace.flush()
            previous=now;cpu=used


async def drain(db,redis,namespace,*,timeout=60):
    deadline=time.monotonic()+timeout
    while True:
        counts=dict(await db.fetchrow("""SELECT
          (SELECT count(*) FROM runtime_commands WHERE status<>'completed') AS commands,
          (SELECT count(*) FROM command_deliveries WHERE status<>'settled') AS command_outbox,
          (SELECT count(*) FROM realtime_business_outbox WHERE status<>'published') AS business_outbox"""))
        groups=[]
        async for key in redis.scan_iter(match=f'workmate:{namespace}:*'):
            if await redis.type(key)=='stream':
                for group in await redis.xinfo_groups(key):groups.append(dict(key=key,**group))
        clean=not any(counts.values()) and all(g.get('pending')==0 and g.get('lag')==0 for g in groups)
        if clean or time.monotonic()>=deadline:return dict(passed=clean,counts=counts,groups=groups)
        await asyncio.sleep(.25)


def sse_assignments(connections):
    return [index%4 for index in range(connections)]


async def sse_round(args,http,headers,db,output):
    """Four continuous execution flows broadcast to N persistent SSE readers."""
    import asyncpg
    import httpx
    from scripts.reliability.load_support.common import chunk,wait_handshakes
    assignments=sse_assignments(args.connections)
    conversations=[];records={};ready=[asyncio.Event() for _ in assignments]
    for group in range(4):
        response=await http.post('/api/conversations',json=dict(agent_id='capacity-text',model_profile='fixture',title=f'CAPACITY SSE flow {group}'))
        assert response.status_code==201
        conversations.append(dict(id=response.json()['id'],admitted=asyncio.Event(),current=None))
    counts={group:assignments.count(group) for group in range(4)}
    readers=[];flows=[];started=deadline=None

    async def reader(index,client):
        conversation=conversations[assignments[index]];bodies={}
        async with client.stream('GET',f"/api/tasks/conversations/{conversation['id']}/events") as response:
            assert response.status_code==200
            async for line in response.aiter_lines():
                if not line.startswith('data:'):continue
                frame=json.loads(line[5:].lstrip());now=time.monotonic();name=frame.get('name')
                if name=='REPLAY_DONE':ready[index].set();continue
                if not ready[index].is_set():continue
                if args.sse_activity=='idle':
                    assert frame.get('type') not in ('RUN_STARTED','RUN_FINISHED','TEXT_MESSAGE_CONTENT')
                    assert name not in ('REALTIME_EVENT','REALTIME_SNAPSHOT'),'Idle connection received task output'
                    continue
                tid=None
                if name in ('REALTIME_EVENT','REALTIME_SNAPSHOT'):
                    await conversation['admitted'].wait()
                    envelope=frame['value'];tid=envelope.get('task_id')
                    assert tid in records,'Unknown Task in dedicated SSE Conversation'
                    if name=='REALTIME_SNAPSHOT':
                        body=''.join(block['text'] for block in envelope['blocks'] if block['kind']=='text')
                        completed=records[tid]['streams'].get(index)
                        if completed is not None:
                            assert len(body.encode())==completed['body_bytes'] and hashlib.sha256(body.encode()).hexdigest()==completed['sha256'],'SSE post-terminal snapshot mismatch'
                        else:
                            assert body==bodies.get(tid,{}).get('body',''),'SSE snapshot differs from task deltas'
                        continue
                    frame=envelope['payload']
                    if frame.get('type')=='TEXT_MESSAGE_CONTENT':
                        assert envelope['text']['from_offset']==len(bodies.get(tid,{}).get('body','').encode()),'Offset gap/duplicate'
                kind=frame.get('type')
                if kind in ('RUN_STARTED','RUN_FINISHED','RUN_ERROR','RUN_CANCELLED','TEXT_MESSAGE_CONTENT'):
                    await conversation['admitted'].wait()
                    tid=tid or conversation['current'];record=records[tid]
                    state=bodies.setdefault(tid,dict(body='',first=None,reply=None,started=None))
                    if kind=='RUN_STARTED':
                        state['reply']=frame.get('runId')
                        if state['started'] is None:state['started']=now
                    if kind=='TEXT_MESSAGE_CONTENT':
                        if state['first'] is None:state['first']=now
                        state['body']+=frame['delta']
                    if kind in ('RUN_FINISHED','RUN_ERROR','RUN_CANCELLED'):
                        assert kind=='RUN_FINISHED','Active SSE failed or interrupted'
                        expected=''.join(chunk(record['key'],i) for i in range(600))
                        assert state['body']==expected,'Active SSE body mismatch'
                        assert index not in record['streams'],'Duplicate terminal for reader/Task'
                        record['streams'][index]=dict(first=state['first'],terminal=now,started=state['started'],reply_id=state['reply'],
                                                     body_bytes=len(expected.encode()),sha256=hashlib.sha256(expected.encode()).hexdigest())
                        del bodies[tid]
                        if len(record['streams'])==counts[assignments[index]]:record['done'].set()
            raise AssertionError('Persistent SSE connection ended before shutdown')

    async def flow(group):
        conversation=conversations[group]
        check=await asyncpg.connect(args.dsn)
        try:
            while time.monotonic()<deadline:
                key=uuid.uuid4().hex;conversation['admitted'].clear()
                submitted=time.monotonic()
                response=await http.post(f"/api/tasks/conversations/{conversation['id']}/tasks",
                                        json=dict(message=f'fixture-case:text {key}',approval_profile='standard'))
                assert response.status_code==202
                tid=response.json()['task']['id'];conversation['current']=tid
                record=dict(id=tid,key=key,conversation_id=conversation['id'],group=group,workload='text',
                            submitted_monotonic=submitted,streams={},done=asyncio.Event(),correct=False)
                records[tid]=record;conversation['admitted'].set()
                async with asyncio.timeout(120):
                    await record['done'].wait()
                    while not await check.fetchval('SELECT active_product_task_id IS NULL FROM conversations WHERE id=$1',conversation['id']):
                        await asyncio.sleep(.05)
                    final=await check.fetchrow("SELECT content,snapshot_phase FROM task_messages WHERE task_id=$1 AND role='assistant'",tid)
                    assert final and final['snapshot_phase']=='final'
                    content=json.loads(final['content']) if isinstance(final['content'],str) else final['content']
                    verify_snapshot_body(content,final['snapshot_phase'],''.join(chunk(key,i) for i in range(600)))
                streams=list(record['streams'].values())
                assert len({s['reply_id'] for s in streams})==1 and streams[0]['reply_id'],'Readers disagree on Reply'
                record.update(reader_delivery_metrics(record['streams']))
                record.update(correct=True,reader_count=len(streams),
                              first_body_seconds=max(s['first'] for s in streams)-submitted,
                              elapsed_seconds=max(s['terminal'] for s in streams)-submitted)
                write(output/f"sse-task-{tid}.json",{k:v for k,v in record.items() if k!='done'})
        finally:await check.close()

    async with httpx.AsyncClient(base_url=args.base_url,headers=headers,timeout=None,
                                limits=httpx.Limits(max_connections=args.connections+4)) as client:
        readers=[asyncio.create_task(reader(i,client)) for i in range(args.connections)]
        try:
            async with asyncio.timeout(60):await wait_handshakes(ready,readers)
            started=time.monotonic();deadline=started+args.duration
            if args.sse_activity=='active':flows=[asyncio.create_task(flow(group)) for group in range(4)]
            async with asyncio.timeout(args.duration+120):
                while time.monotonic()<deadline or any(not task.done() for task in flows):
                    for task in readers:
                        if task.done():
                            await task
                            raise AssertionError('Long-lived reader terminated')
                    for task in flows:
                        if task.done():await task
                    await asyncio.sleep(.1)
                await asyncio.gather(*flows)
            finished=time.monotonic()
            rows=[{k:v for k,v in record.items() if k!='done'} for record in records.values()]
            performance=budgets(rows,workload='text') if args.sse_activity=='active' else {'passed':True}
            return dict(tasks=rows,passed=performance['passed'],budgets=performance,
                        requested_connections=args.connections,execution_concurrency=4 if flows else 0,
                        readers_per_conversation=counts,activity=args.sse_activity,
                        requested_seconds=args.duration,measured_seconds=finished-started,
                        bounded_drain_seconds=max(0,finished-deadline))
        finally:
            for task in flows+readers:task.cancel()
            await asyncio.gather(*flows,*readers,return_exceptions=True)


async def wait_admission(not_before):
    if not_before is None:return
    await asyncio.sleep(max(0,not_before-time.monotonic()))
    if time.monotonic()-not_before>.5:
        raise TimeoutError('Planned admission deadline missed; no catch-up POST')


async def execute_case(args,http,headers,output,index,*,case_override=None,conversation_id=None,admission_not_before=None):
    import asyncpg
    import httpx
    from scripts.reliability.load_support.common import chunk,wait_handshakes
    from scripts.reliability.load_support.approval import approve_once
    from scripts.reliability.load_support.file_workload import file_content
    boundary=getattr(args,'case',None)
    case=case_override or boundary or case_for(index,args.workload);key=uuid.uuid4().hex
    record=dict(index=index,workload=case,key=key,correct=False,planned_monotonic=None,send_lateness_seconds=0)
    if conversation_id is None:
        response=await http.post('/api/conversations',json=dict(agent_id='capacity-text' if args.workload=='text' and not boundary else 'fixture-agent',model_profile='fixture',title=f'CAPACITY {case} {key}'))
        assert response.status_code==201,f'Create Conversation HTTP {response.status_code}'
        cid=response.json()['id'];record['conversation_id']=cid
    else:
        cid=conversation_id;record['conversation_id']=cid
    ready=[asyncio.Event(),asyncio.Event()];admitted=asyncio.Event();stop_started=asyncio.Event()
    async with httpx.AsyncClient(base_url=args.base_url,headers=headers,timeout=None) as reader:
        consumers=[asyncio.create_task(consume(reader,cid,i,record,ready[i],admitted,output,stop_started)) for i in range(2)]
        auto_approval=None
        try:
            async with asyncio.timeout(120):
                await wait_handshakes(ready,consumers)
                record['ready_monotonic']=time.monotonic()
                await wait_admission(admission_not_before)
                record['submitted_monotonic']=time.monotonic()
                record['deliberate_wait_seconds']=record['submitted_monotonic']-record['ready_monotonic'] if admission_not_before is not None else 0
                response=await http.post(f'/api/tasks/conversations/{cid}/tasks',json=dict(message=f'fixture-case:{case} {key}',approval_profile='strict' if case=='approval' else 'standard'))
                record['http_status']=response.status_code
                if response.status_code==429:
                    record.update(outcome='overload_rejected',offered=True,accepted=False)
                    return record
                assert response.status_code==202,f'Submit Task HTTP {response.status_code}'
                record.update(offered=True,accepted=True,admitted_monotonic=time.monotonic())
                record['id']=response.json()['task']['id'];admitted.set()
                if case=='approval':
                    approval=await approve_once(http,record['id'],{},delay=5,tool='Bash' if case=='approval' else 'mcp__fixture__generate')
                    record['approval_evidence']=approval;record['human_wait_seconds']=approval['wait_seconds']
                if case in ('file','file20','skill','stop','mcp'):
                    auto_approval=asyncio.create_task(approve_available(http,record['id'],consumers,record,tool='mcp__fixture__generate' if case=='mcp' else 'Bash'))
                if case=='stop':
                    # Standard SSE may buffer tool stdout until completion. Observe the
                    # authenticated read-only fixture endpoint's actual workspace instead.
                    record['stop_marker_before']=await wait_stop_marker(http,key)
                    record['stop_marker_observed_monotonic']=time.monotonic()
                    response=await http.post('/api/tasks/'+record['id']+'/stop')
                    assert response.status_code in (200,202),f'Stop HTTP {response.status_code}'
                streams=await asyncio.gather(*consumers)
                if auto_approval is not None:
                    await auto_approval
                assert all(s['events'][-1]['type'] in (('RUN_CANCELLED','RUN_ERROR') if case=='stop' else ('RUN_FINISHED',)) for s in streams),'Unexpected terminal'
                replies={e['runId'] for s in streams for e in s['events'] if e.get('runId')}
                assert len(replies)==1,'Reply identity changed'
                record['reply_id']=next(iter(replies))
                # Each concurrent verifier owns its own observer connection.
                check=await asyncpg.connect(args.dsn)
                try:
                    while not await check.fetchval('SELECT active_product_task_id IS NULL FROM conversations WHERE id=$1',cid):await asyncio.sleep(.05)
                    final=await check.fetchrow("SELECT content,snapshot_phase FROM task_messages WHERE task_id=$1 AND role='assistant'",record['id'])
                    status=await check.fetchval('SELECT status FROM tasks WHERE id=$1',record['id'])
                    body=streams[0]['body'];assert all(s['body']==body for s in streams),'SSE consumers disagree'
                    if case=='stop':
                        assert status in ('cancelled','interrupted'),status
                        reason=await check.fetchval("SELECT payload->>'finished_reason' FROM runtime_events WHERE task_id=$1 AND payload->>'type'='REPLY_END' ORDER BY runtime_seq DESC LIMIT 1",record['id'])
                        assert reason=='interrupted',f'Stop Reply reason: {reason}'
                        record['stop_marker_after_terminal']=await wait_stop_marker(http,key)
                        assert final is not None and final['snapshot_phase']=='interrupted',dict(final) if final else None
                    else:
                        assert body==''.join(chunk(key,i) for i in range(600)),'Text mismatch'
                        assert final is not None and final['snapshot_phase']=='final'
                    content=json.loads(final['content']) if isinstance(final['content'],str) else final['content']
                    verify_snapshot_body(content,final['snapshot_phase'],body)
                    if case=='rag':
                        from scripts.reliability.load_support.rag_fixture import TEXT
                        sources=await check.fetch("SELECT payload FROM task_events WHERE task_id=$1 AND event_type='knowledge_sources'",record['id'])
                        encoded=json.dumps([dict(s) for s in sources],default=str)
                        assert sources and 'fixture-doc' in encoded and TEXT in encoded,'Missing knowledge sources'
                        record['rag_evidence']=dict(source_document='fixture-doc',excerpt=TEXT)
                finally:await check.close()
                if case in ('file','file20','approval','skill','mcp'):
                    response=await http.get('/api/artifacts',params={'task_id':record['id']});assert response.status_code==200
                    artifacts=response.json();matches=[a for a in artifacts if a['name']==key+'.txt'];assert len(matches)==1
                    download=await http.get('/api/artifacts/'+matches[0]['id']+'/download')
                    assert download.status_code==200 and download.content==(expected_file(key,20*1024*1024) if case=='file20' else file_content(key,1048576).encode()),'Artifact bytes mismatch'
                    record['file_evidence']=dict(download_bytes=len(download.content),sha256=hashlib.sha256(download.content).hexdigest(),task_id=record['id'],key=key,remote_attempt_verification='pending')
                    if case=='mcp':
                        counter=[a for a in artifacts if a['name']==key+'.count'];assert len(counter)==1
                        download=await http.get('/api/artifacts/'+counter[0]['id']+'/download')
                        assert download.status_code==200 and download.content==b'1','MCP invocation counter mismatch'
                        record['file_evidence'].update(write_attempt_count=1,remote_attempt_verification='MCP counter verified')
                record.update(reader_delivery_metrics(streams))
                record.update(correct=True,admitted_to_run_started_seconds=max((s['started']-record['admitted_monotonic'] for s in streams if s.get('started') is not None),default=None),first_body_seconds=max((s['first']-record['submitted_monotonic'] for s in streams if s['first'] is not None),default=None),
                              elapsed_seconds=(time.monotonic() if case=='file20' else max(s['terminal'] for s in streams))-record['submitted_monotonic'],body_bytes=len(body.encode()),terminal_status=status)
        except Exception as error:record.update(error_type=type(error).__name__,error=str(error))
        finally:
            if auto_approval is not None:
                auto_approval.cancel()
                await asyncio.gather(auto_approval,return_exceptions=True)
            for consumer in consumers:consumer.cancel()
            await asyncio.gather(*consumers,return_exceptions=True)
    return record


async def run(args):
    """Run against already-provisioned isolated tunnel endpoints; return summary."""
    for url in (args.base_url,args.dsn,args.redis_url):loopback_url(url)
    if not args.namespace or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in args.namespace):
        raise ValueError('Explicit isolated namespace required')
    if not 1<=args.tasks<=10000 or not 1<=args.repeat<=100:raise ValueError('Unbounded task/repetition count')
    boundary=getattr(args,'case',None)
    if boundary and args.mode!='smoke':raise ValueError('Explicit file20/stop cases are smoke-only')
    if args.mode=='sse' and args.workload!='text':raise ValueError('SSE connection matrix requires text workload')
    if not 0<args.duration<=900 or not 0<args.arrival_rate<=100:raise ValueError('Invalid duration/rate budget')
    import asyncpg
    import httpx
    import jwt
    from redis.asyncio import Redis
    from scripts.reliability.load_support.common import JWT_SECRET,chunk,wait_handshakes
    from scripts.reliability.load_support.approval import approve_once
    from scripts.reliability.load_support.file_workload import file_content

    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    if (output/'summary.json').exists():raise ValueError('Output already contains summary; choose fresh directory')
    token=jwt.encode(dict(sub='fixture-user',ver=0,iat=int(time.time()),exp=int(time.time())+86400),os.getenv('JWT_SECRET',JWT_SECRET),algorithm='HS256')
    headers={'Authorization':'Bearer '+token}
    report=dict(mode=args.mode,workload=args.workload,boundary_case=boundary,concurrency=args.concurrency,runs=[],passed=False,
                fixture=dict(chunks=600,chunk_bytes=64,interval_seconds=.02,file_bytes=1048576),
                timing_semantics=dict(clock='sender-local time.monotonic seconds only; never subtract remote host monotonic values',
                                      first_body_to_terminal='Maximum per-reader body-first-to-terminal duration; includes controlled generation and product delivery, not pure network latency',
                                      readers_terminal_spread='Latest minus earliest terminal observed by local readers of the same Task'),
                limits=dict(mixed_first_body_p95_seconds=10,mixed_completion_without_human_wait_p95_seconds=60,text_each_task_seconds=32,file20_seconds=120),
                limitations=['Remote file/skill/approval Write attempts require orchestrator evidence join by task_id/key',
                             'Deterministic model; no provider capacity claim',
                             'Text uses each-Task 32s only; first-body 10s and completion p95 60s apply to mixed only; SSE connection continuity and text Task deadlines are separate checks'])
    samples=[];halt=asyncio.Event();sampler=asyncio.create_task(sample_client(halt,output,samples))
    redis=Redis.from_url(args.redis_url,decode_responses=True)
    db=None
    try:
        db=await asyncpg.connect(args.dsn)
        async with httpx.AsyncClient(base_url=args.base_url,headers=headers,timeout=15) as http:
            async def execute(index):
                return await execute_case(args,http,headers,output,index)
            for repetition in range(args.repeat):
                if args.mode=='sse':
                    result=await sse_round(args,http,headers,db,output)
                    result.update(repetition=repetition,closure=await drain(db,redis,args.namespace))
                    report['runs'].append(result);write(output/f'run-{repetition}.json',result)
                    if not result['closure']['passed']:break
                    continue
                if args.mode=='open':
                    rows=await open_schedule(args.tasks,args.arrival_rate,args.duration,execute)
                else:
                    rows=await closed_schedule(args.tasks,args.concurrency,execute)
                closure=await drain(db,redis,args.namespace)
                sent=[t for t in rows if t.get('submitted_monotonic') is not None]
                window=max((t['submitted_monotonic']+t.get('elapsed_seconds',0) for t in sent),default=0)-min((t['submitted_monotonic'] for t in sent),default=0)
                result=dict(repetition=repetition,tasks=rows,budgets=budgets(rows,workload=boundary or args.workload),closure=closure,
                            offered=len(rows),accepted=sum(bool(t.get('accepted')) for t in rows),
                            completed=sum(bool(t.get('correct')) for t in rows),
                            overload_rejected=sum(t.get('outcome')=='overload_rejected' for t in rows),
                            completed_per_second=sum(bool(t.get('correct')) for t in rows)/window if window>0 else 0,
                            queue_observation='Local admitted-to-RUN_STARTED upper bound, includes transport/projection delay',
                            invalid=any(t.get('invalid') for t in rows))
                report['runs'].append(result);write(output/f'run-{repetition}.json',result)
                if not closure['passed']:break
        report['client_checks_passed']=len(report['runs'])==args.repeat and all(r['budgets']['passed'] and r['closure']['passed'] for r in report['runs'])
        report['remote_effects_pending']=any(t.get('file_evidence',{}).get('remote_attempt_verification')=='pending' for r in report['runs'] for t in r['tasks'])
        report['passed']=report['client_checks_passed'] and not report['remote_effects_pending']
    except Exception as error:report.update(error_type=type(error).__name__,error=str(error))
    finally:
        halt.set();await sampler
        if db is not None:await db.close()
        await redis.aclose()
        report['client_sample_summary']=dict(samples=len(samples),cpu_seconds=time.process_time(),max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                            loop_lag_p95_seconds=percentile([s['loop_lag_seconds'] for s in samples],.95))
        all_rows=[row for run_result in report['runs'] for row in run_result['tasks']]
        validity=sample_validity(all_rows,report['client_sample_summary']['loop_lag_p95_seconds'],mode=args.mode)
        report['sample_validity']=validity
        report['client_checks_passed']=bool(report.get('client_checks_passed')) and validity['valid']
        report['passed']=bool(report.get('passed')) and validity['valid']
        write(output/'summary.json',report)
    return report


def main():
    report=asyncio.run(run(build_parser().parse_args()))
    print(json.dumps({'passed':report['passed'],'client_checks_passed':report.get('client_checks_passed')}))
    raise SystemExit(0 if report.get('client_checks_passed') else 1)


if __name__=='__main__':main()
