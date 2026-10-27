"""Independent production roles; parent only drives HTTP and records observations."""
import asyncio,json,os,socket,sys,time,hashlib,uuid
from pathlib import Path
import asyncpg,httpx,jwt
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from common import guard,JWT_SECRET,chunk,parameters,wait_handshakes
from load_results import validate_report
from file_workload import verify_file
from approval import approve_once
from safety import sample as safety_sample, safety_errors
from window import Window
from mix import case_for

from isolation import record_isolation
record_isolation()
guard('parent')
E=Path('/evidence')
def write(name,value): (E/name).write_text(json.dumps(value,default=str,indent=2))
def procstats(pid):
    raw=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    return {'pid':pid,'cpu_seconds':(int(raw[11])+int(raw[12]))/os.sysconf('SC_CLK_TCK'),'rss_bytes':int(raw[21])*os.sysconf('SC_PAGE_SIZE')}

async def main():
    dburl=os.environ['DATABASE_URL']; dsn=dburl.replace('postgresql+asyncpg://','postgresql://')
    seed=await asyncpg.connect(dsn)
    await seed.execute('CREATE EXTENSION IF NOT EXISTS pg_stat_statements')
    await seed.execute("CREATE ROLE fixture_observe LOGIN SUPERUSER PASSWORD 'fixture_observe'")
    obsdsn=dsn.replace('fixture:fixture@','fixture_observe:fixture_observe@')
    obs=await asyncpg.connect(obsdsn,server_settings={'application_name':'fixture_observer'})
    poll=await asyncpg.connect(obsdsn,server_settings={'application_name':'fixture_sampler'})
    engine=create_async_engine(dburl); sessions=async_sessionmaker(engine,expire_on_commit=False)
    redis=Redis.from_url(os.environ['WORKMATE_COORDINATION_REDIS_URL'],decode_responses=True,retry=Retry(NoBackoff(),0),retry_on_timeout=False)
    children=[]; outputs=[]; endpoint=None; http=None
    initial_safety=safety_sample(); admission_errors=[]
    async def launch(script,*args,pass_fds=()):
        out=open(E/(script+'-'+str(len(children))+'.log'),'wb'); outputs.append(out)
        proc=await asyncio.create_subprocess_exec(sys.executable,'/support/'+script+'.py',*args,stdout=out,stderr=asyncio.subprocess.STDOUT,pass_fds=pass_fds)
        children.append(proc); return proc
    def healthy():
        for p in children: assert p.returncode is None,('role exited',p.pid,p.returncode)
    async def wait(predicate,timeout=90):
        async with asyncio.timeout(timeout):
            while not await predicate(): healthy(); await asyncio.sleep(.05)
    async def capture():
        rows=await obs.fetch("SELECT queryid,calls,total_exec_time,rows,wal_bytes,query FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) AND userid=(SELECT oid FROM pg_roles WHERE rolname='fixture')")
        return {'ns':time.monotonic_ns(),'wall_ns':time.time_ns(),'lsn':await obs.fetchval('SELECT pg_current_wal_insert_lsn()'),'pg_sql':[dict(r) for r in rows],'redis':await redis.info()}
    try:
        endpoint=socket.socket(); endpoint.bind(('127.0.0.1',0)); endpoint.listen(); endpoint.set_inheritable(True)
        url='http://127.0.0.1:'+str(endpoint.getsockname()[1])
        await launch('gateway',str(endpoint.fileno()),pass_fds=(endpoint.fileno(),)); endpoint.close(); endpoint=None
        http=httpx.AsyncClient(base_url=url,timeout=15)
        async def ready():
            try: return (await http.get('/health')).status_code==200
            except httpx.HTTPError: return False
        await wait(ready)
        # Product bootstrap is real; seed only the test user and unbound personal Agent.
        from workmate_product.persistence.models import User,Scope,ScopeClosure,ScopeMembership,Agent
        async with sessions.begin() as db:
            db.add(User(id='fixture-user',email='fixture-load@example.test',password_hash='x',display_name='WORKMATE_LOAD')); await db.flush()
            db.add(Scope(id='fixture-scope',kind='personal',owner_user_id='fixture-user',name='WORKMATE_LOAD')); await db.flush()
            db.add_all([ScopeClosure(ancestor_id='fixture-scope',descendant_id='fixture-scope',depth=0),ScopeMembership(scope_id='fixture-scope',user_id='fixture-user'),Agent(id='fixture-agent',slug='fixture-load',display_name='WORKMATE_LOAD',scope_id='fixture-scope',created_by='fixture-user',visibility='private',system_prompt='Return fixture text')])
        if os.environ.get('WORKMATE_LOAD_WORKLOAD') in ('skill','mixed'):
            from skill_fixture import seed_skill
            await seed_skill(sessions)
        if os.environ.get('WORKMATE_LOAD_WORKLOAD') in ('mcp','mixed'):
            from mcp_fixture import seed_mcp
            await seed_mcp(sessions)
        if os.environ.get('WORKMATE_LOAD_WORKLOAD') in ('rag','mixed'):
            from rag_fixture import seed_rag
            await seed_rag(sessions)
        local_workers=('a',) if os.environ.get('WORKMATE_LOAD_WORKERS')=='1' else ('a','b')
        for i in local_workers: await launch('worker',i)
        async def workers_ready(): return all((E/f'worker-{i}-ready.json').exists() for i in (local_workers or ('a',)))
        await wait(workers_ready)
        headers={'Authorization':'Bearer '+jwt.encode({'sub':'fixture-user','ver':0,'iat':int(time.time()),'exp':int(time.time())+7200},JWT_SECRET,algorithm='HS256')}
        count,interval=parameters(); concurrency=int(os.environ['WORKMATE_LOAD_CONCURRENCY']); repeats=int(os.environ['WORKMATE_LOAD_REPEAT']); total=(20 if os.environ['WORKMATE_LOAD_WORKLOAD']=='mixed' else 1) if os.environ['WORKMATE_LOAD_MODE']=='smoke' else int(os.environ['LOAD_TASKS'])
        report={'expected_runs':repeats,'mode':os.environ['WORKMATE_LOAD_MODE'],'workload':os.environ.get('WORKMATE_LOAD_WORKLOAD','text'),'count':count,'chunk_bytes':64,'interval':interval,'concurrency':concurrency,'repeats':repeats,'worker_capacity':int(os.environ.get('WORKMATE_LOAD_WORKER_CAPACITY','2')),'runs':[],'limitations':['deterministic model; no provider latency','same container and host; separate role processes','PG stage timestamps are sampled upper bounds at 250ms','PG stats include role background work; observer user excluded','Redis wire includes 1s observer commands; not subtracted','No proxies, slow client, reconnect, failover or capacity saturation tested']}
        duration=int(os.environ.get('WORKMATE_LOAD_DURATION','0'))
        if duration:
            report['limitations'][2]='PG stage timestamps are sampled upper bounds at 10 seconds'
        window=Window(duration,float(os.environ.get('WORKMATE_LOAD_RATE','1')),burst_rate=float(os.environ.get('WORKMATE_LOAD_BURST_RATE','0'))) if duration else None
        if window:
            report['window']={'passed':False,'state':'running','requested_seconds':duration}
        reused={};reuse_counts={}
        arrival_lock=asyncio.Lock()
        for repetition in range(1000000 if window else repeats):
            if window and not window.admit(time.monotonic()):break
            conversations=[];cases={}
            for i in range(total):
                if os.environ.get('WORKMATE_LOAD_REUSE') and i%2==0 and i in reused and reuse_counts[reused[i]]<2:
                    cid=reused[i];conversations.append(cid)
                    reuse_counts[cid]+=1
                    cases[cid]=case_for(i) if os.environ['WORKMATE_LOAD_WORKLOAD']=='mixed' else os.environ['WORKMATE_LOAD_WORKLOAD']
                    continue
                r=await http.post('/api/conversations',headers=headers,json={'agent_id':'fixture-agent','model_profile':'fixture','title':f'WORKMATE_LOAD {repetition}-{i}'})
                assert r.status_code==201,r.text
                conversations.append(r.json()['id'])
                reused[i]=r.json()['id']
                reuse_counts[r.json()['id']]=1
                cases[r.json()['id']]=case_for(i) if os.environ['WORKMATE_LOAD_WORKLOAD']=='mixed' else os.environ['WORKMATE_LOAD_WORKLOAD']
            records={}; samples=[]; halt_sampling=asyncio.Event()
            async def sample_body():
                last_redis=0; last_safety=0
                while True:
                    started=time.monotonic_ns(); row={'ns':started,'processes':[procstats(os.getpid())]+[procstats(p.pid) for p in children]}
                    if started-last_safety>=10_000_000_000:
                        row['safety']=safety_sample(); last_safety=started
                        admission_errors.extend(safety_errors(row['safety'],initial_safety['oom_kill']))
                        if admission_errors:
                            for tid,record in list(records.items()):
                                if not record.get('correct'):
                                    stopped=await http.post('/api/tasks/'+tid+'/stop',headers=headers)
                                    record['safety_stop_status']=stopped.status_code
                    row['pg_connections']=[dict(r) for r in await poll.fetch("SELECT application_name,state,count(*) AS count FROM pg_stat_activity WHERE datname=current_database() GROUP BY application_name,state")]
                    ids=list(records)
                    if ids:
                        row['tasks']=[dict(r) for r in await poll.fetch("SELECT t.id,t.status,x.status AS execution_status,c.active_product_task_id, m.snapshot_phase,m.snapshot_version, EXISTS(SELECT 1 FROM runtime_events e WHERE e.task_id=t.id AND e.payload->>'type'='REPLY_END') AS reply_end_persisted, EXISTS(SELECT 1 FROM realtime_business_outbox o JOIN product_events p ON p.id=o.event_id WHERE p.task_id=t.id AND p.payload->>'type'='RUN_FINISHED' AND o.status='published') AS finish_published, (SELECT claimed_by FROM runtime_commands r WHERE r.task_id=t.id AND r.kind='StartTask' LIMIT 1) AS owner, (SELECT count(*) FROM realtime_business_outbox o JOIN product_events p ON p.id=o.event_id WHERE p.task_id=t.id AND o.status<>'published') AS unpublished FROM tasks t JOIN runtime_task_executions x ON x.task_id=t.id JOIN conversations c ON c.id=t.conversation_id LEFT JOIN task_messages m ON m.task_id=t.id AND m.role='assistant' WHERE t.id=ANY($1::varchar[])",ids)]
                    if started-last_redis>=1_000_000_000:
                        keys=[k async for k in redis.scan_iter(match='workmate:'+os.environ['WORKMATE_LOAD_NAMESPACE']+':*')]
                        row['redis_namespace_bytes']=sum([(await redis.memory_usage(k)) or 0 for k in keys])
                        row['redis_key_count']=len(keys); last_redis=started
                    row['observer_duration_ns']=time.monotonic_ns()-started; samples.append(row)
                    with (E/f'samples-{repetition}.jsonl').open('a') as trace: trace.write(json.dumps(row,default=str)+'\n')
                    if halt_sampling.is_set(): break
                    try: await asyncio.wait_for(halt_sampling.wait(),10 if window else .25)
                    except TimeoutError: pass
            async def sample():
                try:await sample_body()
                except Exception as error:
                    admission_errors.append('sampler:'+type(error).__name__)
                    raise
            semaphore=asyncio.Semaphore(concurrency)
            async def task(cid):
                case=cases[cid]
                tid=None;admitted_event=asyncio.Event()
                async with semaphore:
                    assert not admission_errors,admission_errors
                    if window:
                        async with arrival_lock:
                            await asyncio.sleep(window.delay(time.monotonic()))
                            if not window.admit(time.monotonic()):return
                            window.sent(time.monotonic())
                    async with httpx.AsyncClient(base_url=url,timeout=None) as reader_http:
                        handshakes=[asyncio.Event(),asyncio.Event()]
                        async def consume(index):
                            body=''; covered=[]; resets=0; first_ns=None; events=[]
                            async with reader_http.stream('GET',f'/api/tasks/conversations/{cid}/events',headers=headers) as response:
                                assert response.status_code==200,await response.aread()
                                async for line in response.aiter_lines():
                                    if not line.startswith('data: '): continue
                                    value=json.loads(line[6:]); now=time.monotonic_ns()
                                    with (E/f'sse-{cid}-{index}.jsonl').open('a') as trace: trace.write(json.dumps({'ns':now,'frame':value,'prior_body_bytes':len(body.encode())})+'\n')
                                    kind=value.get('type'); name=value.get('name')
                                    if name=='REALTIME_RESET':
                                        resets+=1
                                        if handshakes[index].is_set():
                                            assert case in ('approval','mcp') and not body,'unexpected reset after handshake'
                                            pending=value['value']['business_state']['pending_confirm']
                                            assert pending and pending['approvalProfile'] in ('strict','standard'),'reset must describe this approval boundary'
                                        if not os.environ.get('WORKMATE_LOAD_REUSE'):
                                            assert value['value']['snapshots']==[],value
                                        elif handshakes[index].is_set():
                                            assert all(s.get('task_id')!=tid or not any(b.get('text') for b in s['blocks'] if b['kind']=='text') for s in value['value']['snapshots']),value
                                    if name=='REPLAY_DONE':
                                        handshakes[index].set();continue
                                    if not handshakes[index].is_set():continue
                                    if name=='REALTIME_SNAPSHOT':
                                        snapshot=value['value']
                                        await admitted_event.wait()
                                        if snapshot.get('task_id')!=tid:continue
                                        snapshot_body=''.join(b['text'] for b in snapshot['blocks'] if b['kind']=='text')
                                        assert snapshot_body==body,('boundary snapshot differs from consumed deltas',len(snapshot_body),len(body))
                                    if name=='REALTIME_EVENT':
                                        envelope=value['value']
                                        await admitted_event.wait()
                                        if envelope.get('task_id')!=tid:continue
                                        value=envelope['payload']; kind=value['type']
                                        if kind=='TEXT_MESSAGE_CONTENT':
                                            assert envelope['text']['from_offset']==len(body.encode()),envelope['text']
                                    if kind=='TEXT_MESSAGE_CONTENT':
                                        if first_ns is None: first_ns=now
                                        body+=value['delta']; covered.append({'bytes':len(body.encode()),'ns':now})
                                    if kind in ('RUN_STARTED','RUN_FINISHED','RUN_ERROR'): events.append({'event':value,'ns':now})
                                    if kind in ('RUN_FINISHED','RUN_ERROR'):
                                        return {'body':body,'covered':covered,'first_ns':first_ns,'terminal_ns':now,'events':events,'reset_count':resets}
                            raise AssertionError('SSE EOF before terminal')
                        consumers=[asyncio.create_task(consume(i)) for i in range(2)]
                        try:
                            async with asyncio.timeout(90):
                                await wait_handshakes(handshakes, consumers)
                                submitted=time.monotonic_ns()
                                result=await http.post(f'/api/tasks/conversations/{cid}/tasks',headers=headers,json={'message':'fixture-case:'+case+' '+uuid.uuid4().hex,'approval_profile':'strict' if case=='approval' else 'standard'})
                                assert result.status_code==202,result.text
                                tid=result.json()['task']['id']; records[tid]={'conversation_id':cid,'workload':case,'submitted_ns':submitted,'admitted_ns':time.monotonic_ns()}
                                admitted_event.set()
                                if case in ('approval','mcp'):
                                    records[tid]['approval_evidence']=await approve_once(http,tid,headers,tool='mcp__fixture__generate' if case=='mcp' else 'Write')
                                streams=await asyncio.gather(*consumers)
                                assert all(s['events'][-1]['event']['type']=='RUN_FINISHED' for s in streams), 'non-success terminal'
                                if case in ('approval','mcp'):
                                    replies={e['event']['runId'] for stream in streams for e in stream['events'] if e['event'].get('runId')}
                                    assert len(replies)==1,'Approval changed Reply identity'
                                    records[tid]['approval_evidence']['reply_id']=next(iter(replies))
                                # Use own short connection: concurrent task checks cannot share asyncpg connection.
                                c=await asyncpg.connect(obsdsn)
                                try:
                                    async with asyncio.timeout(90):
                                        while not await c.fetchval('SELECT active_product_task_id IS NULL FROM conversations WHERE id=$1',cid): await asyncio.sleep(.05)
                                    final=await c.fetchrow("SELECT content,snapshot_phase FROM task_messages WHERE task_id=$1 AND role='assistant'",tid)
                                finally: await c.close()
                                key=streams[0]['body'][:32]; expected=''.join(chunk(key,i) for i in range(count))
                                assert all(s['body']==expected for s in streams),'body incomplete or duplicate'
                                assert final['snapshot_phase']=='final' and json.loads(final['content'])['text']==expected,dict(final)
                                if case in ('file','approval','skill','mcp'):
                                    artifacts=await http.get('/api/artifacts',headers=headers,params={'task_id':tid})
                                    assert artifacts.status_code==200,artifacts.text
                                    matches=[a for a in artifacts.json() if a['name']==key+'.txt']
                                    assert len(matches)==1,artifacts.text
                                    download=await http.get('/api/artifacts/'+matches[0]['id']+'/download',headers=headers)
                                    assert download.status_code==200,download.status_code
                                    size=1024 if os.environ['WORKMATE_LOAD_MODE']=='smoke' else 1048576
                                    if case=='mcp':
                                        from file_workload import file_content
                                        assert download.content==file_content(key,size).encode()
                                        counters=[a for a in artifacts.json() if a['name']==key+'.count']
                                        assert len(counters)==1
                                        counter=await http.get('/api/artifacts/'+counters[0]['id']+'/download',headers=headers)
                                        assert counter.status_code==200 and counter.content==b'1'
                                        records[tid]['file_evidence']={'write_attempt_count':1,'download_bytes':size,'download_sha256':hashlib.sha256(download.content).hexdigest(),'effect_observer':'MCP server counter'}
                                    else:
                                        records[tid]['file_evidence']=verify_file(key,download.content,size,E)
                                if case=='rag':
                                    from rag_fixture import TEXT
                                    c=await asyncpg.connect(obsdsn)
                                    try:
                                        sources=await c.fetch("SELECT payload FROM task_events WHERE task_id=$1 AND event_type='knowledge_sources'",tid)
                                    finally:await c.close()
                                    encoded=json.dumps([dict(r) for r in sources])
                                    assert sources and 'fixture-doc' in encoded and TEXT in encoded,encoded
                                    records[tid]['rag_evidence']={'source_document':'fixture-doc','excerpt':TEXT}
                                elapsed=max(s['terminal_ns'] for s in streams)-submitted
                                records[tid].update(streams=streams,key=key,delivery_observed_ns=time.monotonic_ns(),correct=True,budget_pass=elapsed<=32_000_000_000,elapsed_seconds=elapsed/1e9,sha256=hashlib.sha256(expected.encode()).hexdigest())
                        except BaseException as error:
                            admission_errors.append(type(error).__name__)
                            if tid:
                                try:
                                    stopped=await http.post('/api/tasks/'+tid+'/stop',headers=headers)
                                    records[tid]['stop_after_failure_status']=stopped.status_code
                                except Exception as stop_error:
                                    records[tid]['stop_after_failure_error']=type(stop_error).__name__
                            write(f'failure-{cid}.json',{'type':type(error).__name__,'text':str(error),'records':records,'children':[{'pid':p.pid,'returncode':p.returncode} for p in children]})
                            raise
                        finally:
                            for consumer in consumers:
                                if not consumer.done(): consumer.cancel()
                            await asyncio.gather(*consumers,return_exceptions=True)
            await obs.execute('CHECKPOINT'); before=await capture(); sampling=asyncio.create_task(sample())
            outcomes=[]
            try:
                outcomes=await asyncio.gather(*(task(cid) for cid in conversations),return_exceptions=True)
            finally:
                halt_sampling.set()
                state={'records':records,'samples':samples,'before':before,'after':None,'wal_bytes':None,'errors':[{'type':type(e).__name__,'text':str(e)} for e in outcomes if isinstance(e,BaseException)],'collection_errors':[]}
                write(f'run-{repetition}.json',state)
                for label,operation in (('sampler',lambda:sampling),('after',capture)):
                    try:
                        value=await operation()
                        if label=='after': state['after']=value
                    except BaseException as error: state['collection_errors'].append({'stage':label,'type':type(error).__name__,'text':str(error)})
                wal=None
                if state['after'] is not None:
                    try:
                        wal=await obs.fetchval('SELECT pg_wal_lsn_diff($1::pg_lsn,$2::pg_lsn)',state['after']['lsn'],before['lsn'])
                        state['wal_bytes']=int(wal)
                    except BaseException as error: state['collection_errors'].append({'stage':'wal','type':type(error).__name__,'text':str(error)})
                write(f'run-{repetition}.json',state)
            # ACK and settlement follow the terminal event asynchronously. Keep
            # consumers alive and allow bounded convergence, never clear a PEL.
            async def drained():
                if await obs.fetchval("SELECT EXISTS(SELECT 1 FROM runtime_commands WHERE status<>'completed') OR EXISTS(SELECT 1 FROM command_deliveries WHERE status<>'settled') OR EXISTS(SELECT 1 FROM realtime_business_outbox WHERE status<>'published')"):
                    return False
                for stream in [k async for k in redis.scan_iter(match='workmate:'+os.environ['WORKMATE_LOAD_NAMESPACE']+':*')]:
                    if await redis.type(stream)=='stream':
                        for group in await redis.xinfo_groups(stream):
                            if group.get('pending')!=0 or group.get('lag')!=0:
                                return False
                return True
            try:
                await wait(drained, timeout=15)
            except TimeoutError:
                state['collection_errors'].append({'stage':'drain','type':'TimeoutError'})
            unfinished=await obs.fetch("SELECT status,count(*) AS count FROM runtime_commands WHERE status<>'completed' GROUP BY status")
            outbox=await obs.fetch("SELECT status,count(*) AS count FROM command_deliveries GROUP BY status")
            business=await obs.fetch("SELECT status,count(*) AS count FROM realtime_business_outbox GROUP BY status")
            pels=[]
            for key in [k async for k in redis.scan_iter(match='workmate:'+os.environ['WORKMATE_LOAD_NAMESPACE']+':*')]:
                if await redis.type(key)=='stream':
                    for group in await redis.xinfo_groups(key):
                        pending=await redis.xpending_range(key,group['name'],'-','+',100)
                        pels.append({'key':key,'group':group,'pending_sample':pending})
            body_rows=await obs.fetchval("SELECT (SELECT count(*) FROM runtime_events WHERE payload->>'type' LIKE 'TEXT_BLOCK_%')+(SELECT count(*) FROM product_events WHERE payload->>'type' LIKE 'TEXT_MESSAGE_%')")
            effects=await obs.fetch("SELECT task_id,count(*) AS count FROM runtime_events WHERE payload->>'type'='REPLY_END' GROUP BY task_id")
            effect_counts={row['task_id']:row['count'] for row in effects}
            run={'expected_tasks':total,'tasks':[{'id':tid,'terminal':bool(record.get('correct')),'effect_count':effect_counts.get(tid,0),'workload':record.get('workload'),'file_evidence':record.get('file_evidence'),'rag_evidence':record.get('rag_evidence'),'approval_evidence':record.get('approval_evidence')} for tid,record in records.items()],'repetition':repetition,'correct':len(records)==total and all(r.get('correct') for r in records.values()) and not state['errors'] and not state['collection_errors'],'budget_pass':all(r.get('budget_pass') for r in records.values()),'unfinished_commands':[dict(r) for r in unfinished],'command_outbox':[dict(r) for r in outbox],'business_outbox':[dict(r) for r in business],'pel':pels,'wal_bytes':state['wal_bytes'],'collection_errors':state['collection_errors'],'pg_body_rows':body_rows}
            if window:
                run['expected_tasks']=len(records)
                run['correct']=bool(records) and all(r.get('correct') for r in records.values()) and not state['errors'] and not state['collection_errors']
            run['correct'] = run['correct'] and not admission_errors and not unfinished and body_rows==0 and all(not g['group']['pending'] for g in pels) and all(r['status']=='published' for r in business)
            timing=[]
            for tid,record in records.items():
                if not record.get('correct'): continue
                model=json.loads((E/('model-'+record['key']+'.json')).read_text())
                marks=model['marks']; per_reader=[]
                for stream in record['streams']:
                    latencies=[]; index=0
                    for mark in marks:
                        while stream['covered'][index]['bytes']<(mark['index']+1)*64: index+=1
                        latencies.append((stream['covered'][index]['ns']-mark['emit_ns'])/1e6)
                    ordered=sorted(latencies)
                    per_reader.append({'emission_to_receive_ms_p50':ordered[len(ordered)//2],'emission_to_receive_ms_p95':ordered[min(len(ordered)-1,int(len(ordered)*.95))],'first_body_from_submit_ms':(stream['first_ns']-record['submitted_ns'])/1e6,'final_model_to_sse_terminal_ms':(stream['terminal_ns']-model['final_emit_ns'])/1e6})
                stage_first={}
                for sample_row in samples:
                    for task_row in sample_row.get('tasks',[]):
                        if task_row['id']!=tid: continue
                        for label,value in [('reply_end',task_row['reply_end_persisted']),('task_terminal',task_row['status']=='completed'),('business_finish_published',task_row['finish_published']),('delivery_closed',task_row['active_product_task_id'] is None)]:
                            if value: stage_first.setdefault(label,sample_row['ns'])
                timing.append({'task_id':tid,'model_key':record['key'],'worker':model['worker'],'workload':record['workload'],'submit_to_terminal_seconds':record['elapsed_seconds'],'budget_pass':record['budget_pass'],'actual_generation_seconds':(marks[-1]['emit_ns']-marks[0]['emit_ns'])/1e9,'yield_backpressure_ms':sum((r.get('resumed_ns',r['emit_ns'])-r['emit_ns']) for r in marks)/1e6,'readers':per_reader,'sampled_stage_first_ns':stage_first})
            write(f'timing-{repetition}.json',timing)
            if window and run['correct'] and repetition%20:
                # Keep full bodies for a fixed 1/20 batch sample and every failure.
                # Task IDs, hashes, timings, effects and resource samples remain.
                for record in records.values():
                    cid=record['conversation_id'];key=record['key']
                    for name in (f'sse-{cid}-0.jsonl',f'sse-{cid}-1.jsonl',f'emission-{key}.jsonl',f'model-{key}.json'):
                        (E/name).unlink(missing_ok=True)
                    record.pop('streams',None)
                state['records']=records
                write(f'run-{repetition}.json',state)
            report['runs'].append(run)
            if window:report['expected_runs']=len(report['runs'])
            write('result.json',report)
            if not run['correct'] or admission_errors: break
        if window:
            for error in admission_errors:window.fail(error)
            report['window']=window.result(time.monotonic())
            report['sample_interval_seconds']=10
            report['full_body_sample_every_batches']=20
            cooldown=int(os.environ.get('WORKMATE_LOAD_COOLDOWN','0'))
            if report['window']['passed'] and cooldown:
                start=time.monotonic();countdown=[]
                while True:
                    healthy();row=safety_sample();row['elapsed_seconds']=time.monotonic()-start
                    countdown.append(row)
                    with (E/'cooldown.jsonl').open('a') as trace:trace.write(json.dumps(row)+'\n')
                    errors=safety_errors(row,initial_safety['oom_kill'])
                    if errors:
                        for error in errors:window.fail(error)
                        break
                    if row['elapsed_seconds']>=cooldown:break
                    await asyncio.sleep(min(10,cooldown-row['elapsed_seconds']))
                report['cooldown']={'requested_seconds':cooldown,'observed_seconds':countdown[-1]['elapsed_seconds'],'passed':not window.errors}
                report['window']=window.result(time.monotonic())
            write('result.json',report)
        healthy()
    finally:
        for proc in reversed(children):
            if proc.returncode is None:
                proc.terminate()
                try: await asyncio.wait_for(proc.wait(),45)
                except TimeoutError: proc.kill(); await proc.wait()
        write('process-exits.json',[{'pid':p.pid,'exit_code':p.returncode} for p in children])
        for output in outputs: output.close()
        if endpoint: endpoint.close()
        if http: await http.aclose()
        await redis.aclose(); await seed.close(); await obs.close(); await poll.close(); await engine.dispose()
    assert all(p.returncode in (0,-15) for p in children),'role shutdown failed'
    for worker in local_workers:
        state=json.loads((E/('worker-'+worker+'-state.json')).read_text())
        assert not state['failures'] and state['ownership_remaining']==0,state
    gateway_state=json.loads((E/'gateway-state.json').read_text())
    for name in ('projection_task','command_publisher_task','business_publisher_task'):
        assert gateway_state[name] and gateway_state[name]['done'] and not gateway_state[name]['exception'],gateway_state[name]
    errors=validate_report(report)
    write('validation.json',{'passed':not errors,'errors':errors})
    assert not errors, errors
asyncio.run(main())
