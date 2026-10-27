"""One real once-Schedule admission; no synthetic Task insertion or retries."""
import asyncio
from datetime import timedelta
import json
import math
from pathlib import Path
import time
import uuid


def registry(output):
    path=Path(output)/'scheduled-sources.json'
    return path,json.loads(path.read_text()) if path.exists() else []


def save_sources(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(rows,indent=2,default=str));temporary.replace(path)


async def cleanup_scheduled_sources(http,output):
    path,rows=registry(output);results=[]
    for row in rows:
        if row.get('settled'):continue
        if not row.get('creation_started',True):
            row['settled']=True;save_sources(path,rows);continue
        sid=row.get('id')
        if not sid:
            # A lost create response has an exact fixture name recorded before POST.
            matches=[]
            for offset in range(0,1000,100):
                response=await http.get('/api/schedules',params={'limit':100,'offset':offset});response.raise_for_status()
                page=response.json();matches.extend(x for x in page['items'] if x['name']==row['name'])
                if offset+100>=page['total']:break
            assert len(matches)<=1,'Ambiguous scheduled source'
            if not matches:
                raise AssertionError('Unknown Schedule creation outcome; cannot certify source cleanup')
            sid=matches[0]['id'];row['id']=sid;save_sources(path,rows)
        response=await http.get('/api/schedules/'+sid);response.raise_for_status();value=response.json()
        if value.get('next_fire_at') is not None and value['status']=='active':
            response=await http.post('/api/schedules/'+sid+'/pause',json={'version':value['version']});response.raise_for_status()
            value=response.json()
        assert value.get('next_fire_at') is None or value['status']!='active','Future Schedule remains live'
        row['settled']=True;row['cleanup_status']=value['status'];save_sources(path,rows)
        results.append({'schedule_id':sid,'future_disabled':True})
    return results


async def pause_sources(args,http,headers=None):
    return await cleanup_scheduled_sources(http,args.output)


def apply_snapshot(snapshot,task_id,expected):
    if snapshot.get('task_id')!=task_id:return None
    assert snapshot.get('phase') not in ('approval','interrupted'),'Unexpected scheduled snapshot phase'
    body=''.join(block['text'] for block in snapshot['blocks'] if block['kind']=='text')
    assert expected.startswith(body),'Scheduled snapshot text mismatch'
    if snapshot.get('phase')=='final':assert body==expected and not snapshot.get('incomplete'),'Incomplete final scheduled snapshot'
    return body,snapshot.get('phase')=='final'


async def consume_scheduled(http,cid,tid,expected,output,index):
    body='';first=None;started=None;resets=0
    with (Path(output)/f'schedule-sse-{tid}-{index}.jsonl').open('w') as trace:
        async with http.stream('GET',f'/api/tasks/conversations/{cid}/events') as response:
            assert response.status_code==200
            async for line in response.aiter_lines():
                if not line.startswith('data:'):continue
                frame=json.loads(line[5:].lstrip());now=time.monotonic()
                trace.write(json.dumps({'monotonic':now,'frame':frame})+'\n')
                name=frame.get('name');value=frame.get('value',{})
                snapshots=value.get('snapshots',[]) if name=='REALTIME_RESET' else [value] if name=='REALTIME_SNAPSHOT' else []
                if name=='REALTIME_RESET':resets+=1
                for snapshot in snapshots:
                    updated=apply_snapshot(snapshot,tid,expected)
                    if updated is None:continue
                    body,final=updated
                    if body and first is None:first=now
                    if final:return {'body':body,'first':first,'started':started,'terminal':now,'reset_count':resets,'terminal_source':'final_snapshot'}
                if name=='REALTIME_EVENT':
                    if value.get('task_id')!=tid:continue
                    frame=value['payload']
                    if frame.get('type')=='TEXT_MESSAGE_CONTENT':assert value['text']['from_offset']==len(body.encode()),'Scheduled SSE offset mismatch'
                elif name in ('REALTIME_RESET','REALTIME_SNAPSHOT','REPLAY_DONE'):continue
                kind=frame.get('type')
                assert frame.get('name')!='HITL_APPROVAL_REQUEST','Unattended Schedule requested ASK'
                if kind=='RUN_STARTED' and started is None:started=now
                if kind=='TEXT_MESSAGE_CONTENT':
                    if first is None:first=now
                    body+=frame['delta'];assert expected.startswith(body),'Scheduled SSE body mismatch'
                if kind in ('RUN_FINISHED','RUN_ERROR','RUN_CANCELLED'):
                    assert kind=='RUN_FINISHED' and body==expected,'Scheduled terminal mismatch'
                    return {'body':body,'first':first,'started':started,'terminal':now,'reset_count':resets,'terminal_source':'event'}
    raise AssertionError('Scheduled SSE ended without terminal')


async def execute_scheduled_case(args,http,headers,output,index,*,admission_not_before=None,**kwargs):
    import asyncpg
    import httpx
    from scripts.reliability.load_support.common import chunk
    from scripts.reliability.capacity_support.client import reader_delivery_metrics,verify_snapshot_body,loopback_url,wait_admission
    loopback_url(args.dsn);loopback_url(args.base_url)
    if admission_not_before is not None and (not isinstance(admission_not_before,(int,float)) or not math.isfinite(admission_not_before)):
        raise ValueError('Admission deadline must be a finite local monotonic time')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    path,rows=registry(args.output)
    assert not any(not row.get('settled') for row in rows),'Only one live scheduled source permitted'
    key=uuid.uuid4().hex;name='soak-schedule-'+key
    record=dict(index=index,key=key,workload='text',source='schedule',correct=False,offered=True,accepted=False,
                planned_monotonic=None,send_lateness_seconds=0,preparation_started_monotonic=time.monotonic())
    entry={'name':name,'key':key,'settled':False,'creation_started':False};rows.append(entry);save_sources(path,rows)
    db=None;consumers=[]
    try:
        async with asyncio.timeout(120):
            db=await asyncpg.connect(args.dsn,timeout=10,command_timeout=10)
            record['source_ready_monotonic']=time.monotonic()
            await wait_admission(admission_not_before)
            # Compute due only after the planned admission wait; never age run_at
            # while preparing ten seconds ahead of an open-loop arrival.
            now=await db.fetchval('SELECT clock_timestamp()');due=now+timedelta(seconds=2)
            record.update(schedule_creation_pg=now.isoformat(),scheduled_for=due.isoformat())
            body={'name':name,'agent_id':'capacity-text','prompt':f'fixture-case:text {key}','timezone':'UTC',
                  'rule':{'kind':'once','run_at':due.isoformat()},'model_profile':'fixture','notification':None}
            if admission_not_before is not None and time.monotonic()-admission_not_before>.5:
                raise TimeoutError('Schedule admission deadline missed; no catch-up creation')
            entry['creation_started']=True;save_sources(path,rows)
            record['source_submitted_monotonic']=time.monotonic()
            record['submitted_monotonic']=record['source_submitted_monotonic']
            record['deliberate_wait_seconds']=record['source_submitted_monotonic']-record['source_ready_monotonic'] if admission_not_before is not None else 0
            creation=asyncio.create_task(asyncio.wait_for(http.post('/api/schedules',json=body),20));cancelled=False
            while not creation.done():
                try:await asyncio.shield(creation)
                except asyncio.CancelledError:cancelled=True
            response=creation.result();assert response.status_code==201,f'Schedule create HTTP {response.status_code}'
            definition=response.json();entry.update(id=definition['id'],version=definition['version']);save_sources(path,rows)
            record.update(schedule_id=entry['id'],creation_response_monotonic=time.monotonic())
            if cancelled:raise asyncio.CancelledError
            while True:
                response=await http.get('/api/schedules/'+entry['id']+'/occurrences');response.raise_for_status();history=response.json()
                assert history['total']<=1,'Duplicate scheduled occurrence'
                if history['items']:
                    occurrence=history['items'][0];assert occurrence['outcome']=='task_created',occurrence
                    assert occurrence['task_id'] and occurrence['conversation_id'];break
                await asyncio.sleep(.2)
            tid=occurrence['task_id'];cid=occurrence['conversation_id']
            record.update(id=tid,conversation_id=cid,occurrence_id=occurrence['id'],accepted=True,admitted_monotonic=time.monotonic())
            expected=''.join(chunk(key,i) for i in range(600))
            trace_directory=output/f'trace-{index}';trace_directory.mkdir(exist_ok=True)
            record['trace_directory']=str(trace_directory);record['conversation_turn']=1
            async with httpx.AsyncClient(base_url=args.base_url,headers=headers,timeout=None) as reader:
                consumers=[asyncio.create_task(consume_scheduled(reader,cid,tid,expected,trace_directory,i)) for i in range(2)]
                streams=await asyncio.gather(*consumers)
            while not await db.fetchval('SELECT active_product_task_id IS NULL FROM conversations WHERE id=$1',cid):await asyncio.sleep(.05)
            task=await db.fetchrow('SELECT status,origin,approval_profile,created_at,run_id FROM tasks WHERE id=$1',tid)
            assert task['status']=='completed' and task['origin']=='schedule' and task['approval_profile']=='unattended',dict(task)
            count=await db.fetchval('SELECT count(*) FROM tasks WHERE schedule_occurrence_id=$1',occurrence['id']);assert count==1
            count=await db.fetchval('SELECT count(*) FROM schedule_occurrences WHERE schedule_id=$1',entry['id']);assert count==1
            asks=await db.fetchval("SELECT count(*) FROM runtime_events WHERE task_id=$1 AND payload->>'type' IN ('REQUIRE_USER_CONFIRM','REQUIRE_USER_INPUT')",tid);assert asks==0
            final=await db.fetchrow("SELECT content,snapshot_phase FROM task_messages WHERE task_id=$1 AND role='assistant'",tid)
            assert final and final['snapshot_phase']=='final'
            content=json.loads(final['content']) if isinstance(final['content'],str) else final['content']
            verify_snapshot_body(content,'final',expected)
            assert all(s['body']==expected for s in streams)
            record.update(reader_delivery_metrics(streams))
            record.update(correct=True,terminal_status='completed',reply_id=task['run_id'],task_created_at_pg=task['created_at'].isoformat(),
                          body_bytes=len(expected.encode()),elapsed_seconds=max(s['terminal'] for s in streams)-record['submitted_monotonic'],
                          first_body_seconds=max(s['first'] for s in streams)-record['submitted_monotonic'],
                          timing_scope='Local schedule creation submission to observed delivery; scheduling delay included; DB Task interval reported separately',
                          stream_receipts=[{k:v for k,v in s.items() if k!='body'} for s in streams])
    except Exception as error:record.update(error_type=type(error).__name__,error=str(error))
    finally:
        for consumer in consumers:consumer.cancel()
        await asyncio.gather(*consumers,return_exceptions=True)
        if db is not None:await db.close()
        try:record['source_cleanup']=await cleanup_scheduled_sources(http,args.output)
        except Exception as error:record.update(correct=False,source_cleanup_error=type(error).__name__)
    return record
