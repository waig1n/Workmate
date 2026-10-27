"""Three process deaths at observed production boundaries on one POSIX node."""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
import uuid
import asyncpg
import httpx
import jwt
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from common import guard,JWT_SECRET
from isolation import record_isolation
from file_workload import verify_file

E=Path('/evidence')
DELIVERY_POLICY={
 'preparing':{'StartTask':{'command':['claimed'],'delivery':[['published',None],['quarantined','execution_owner_lost']]}},
 'release-before':{'StartTask':{'command':['completed'],'delivery':[['settled',None]]},'ResumeInteraction':{'command':['accepted'],'delivery':[['published',None]]}},
 'release-after':{'StartTask':{'command':['completed'],'delivery':[['settled',None]]},'ResumeInteraction':{'command':['completed'],'delivery':[['settled',None]]}},
}
POLICY={'worker_lease_seconds':6,'barrier_deadline_seconds':90,'recovery_deadline_seconds':45,
        'settlement_deadline_seconds':5,'unsafe_observation_seconds':9,'fault':'SIGKILL exact observed Worker PID'}

def write(name,value): (E/name).write_text(json.dumps(value,default=str,indent=2))

async def main():
    record_isolation();guard('boundary-parent');write('boundary-plan.json',{'timing':POLICY,'delivery_policy':DELIVERY_POLICY})
    dburl=os.environ['DATABASE_URL'];dsn=dburl.replace('postgresql+asyncpg://','postgresql://')
    admin=await asyncpg.connect(dsn)
    redis=Redis.from_url(os.environ['WORKMATE_COORDINATION_REDIS_URL'],decode_responses=True)
    engine=create_async_engine(dburl);sessions=async_sessionmaker(engine,expire_on_commit=False)
    children=[];outputs=[];http=None;results=[];allowed_blocked={}
    async def launch(script,*args,pass_fds=()):
        output=open(E/(script+'-'+('-'.join(args) or 'role')+'.log'),'wb');outputs.append(output)
        proc=await asyncio.create_subprocess_exec(sys.executable,'/support/'+script+'.py',*args,
            stdout=output,stderr=asyncio.subprocess.STDOUT,pass_fds=pass_fds)
        children.append(proc);return proc
    async def wait_file(path,process):
        async with asyncio.timeout(POLICY['barrier_deadline_seconds']):
            while not path.exists():
                assert process.returncode is None,('early worker exit',process.returncode)
                await asyncio.sleep(.05)
        return json.loads(path.read_text())
    async def state(tid):
        return dict(await admin.fetchrow('''SELECT t.status AS task_status,s.status,s.owner_id,s.execution_epoch,
            s.reply_id,s.id AS session_id,s.lease_until,s.snapshot_id FROM tasks t JOIN runtime_task_executions x ON x.task_id=t.id
            JOIN runtime_sessions s ON s.id=x.session_id WHERE t.id=$1''',tid))
    try:
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();sock.set_inheritable(True)
        url='http://127.0.0.1:'+str(sock.getsockname()[1])
        gateway=await launch('gateway',str(sock.fileno()),pass_fds=(sock.fileno(),));sock.close()
        http=httpx.AsyncClient(base_url=url,timeout=15)
        async with asyncio.timeout(60):
            while True:
                try:
                    if (await http.get('/health')).status_code==200:break
                except httpx.HTTPError:pass
                assert gateway.returncode is None
                await asyncio.sleep(.1)
        from workmate_product.persistence.models import User,Scope,ScopeClosure,ScopeMembership,Agent
        async with sessions.begin() as db:
            db.add(User(id='fixture-user',email='fault-boundary@example.test',password_hash='x',display_name='FAULT'));await db.flush()
            db.add(Scope(id='fixture-scope',kind='personal',owner_user_id='fixture-user',name='FAULT'));await db.flush()
            db.add_all([ScopeClosure(ancestor_id='fixture-scope',descendant_id='fixture-scope',depth=0),
                ScopeMembership(scope_id='fixture-scope',user_id='fixture-user'),
                Agent(id='fixture-agent',slug='fault-boundary',display_name='FAULT',scope_id='fixture-scope',created_by='fixture-user',visibility='private',system_prompt='Return fixture text')])
        headers={'Authorization':'Bearer '+jwt.encode({'sub':'fixture-user','ver':0,'iat':int(time.time()),'exp':int(time.time())+3600},JWT_SECRET,algorithm='HS256')}
        for mode in ('preparing','release-before','release-after'):
            name='a-'+mode
            first=await launch('worker',name);await wait_file(E/('worker-'+name+'-ready.json'),first)
            response=await http.post('/api/conversations',headers=headers,json={'agent_id':'fixture-agent','model_profile':'fixture','title':'FAULT '+mode})
            assert response.status_code==201,response.text
            cid=response.json()['id'];key=uuid.uuid4().hex
            response=await http.post('/api/tasks/conversations/'+cid+'/tasks',headers=headers,
                json={'message':'fixture-case:approval '+key,'approval_profile':'strict'})
            assert response.status_code==202,response.text
            tid=response.json()['task']['id']
            barrier=await wait_file(E/(name+'-barrier.json'),first)
            before=await state(tid)
            start_status=await admin.fetchval("SELECT status FROM runtime_commands WHERE task_id=$1 AND kind='StartTask'",tid)
            suspension=await admin.fetchrow('SELECT status,cleanup_receipt FROM runtime_suspensions WHERE task_id=$1',tid)
            assert before['status']==('preparing' if mode=='preparing' else 'awaiting_interaction'),before
            if mode=='release-before':assert suspension['status']=='releasing' and suspension['cleanup_receipt'] is None
            if mode=='release-after':assert suspension['status']=='released' and before['owner_id'] is None,(dict(suspension),before)
            if mode!='release-after':
                allowed_blocked[before['session_id']]=(['blocked_expired','blocked_inconsistent'] if mode=='preparing' else ['blocked_suspension'])
                write('allowed-blocked.json',allowed_blocked)
            started=time.monotonic();first.kill();await asyncio.wait_for(first.wait(),5);assert first.returncode==-signal.SIGKILL
            approval=None
            if mode!='preparing':
                pending=await http.get('/api/tasks/'+tid+'/pending-confirm',headers=headers)
                assert pending.status_code==200,pending.text
                approval=pending.json();calls=approval['toolCalls'];assert len(calls)==1 and calls[0]['name']=='Write'
                response=await http.post('/api/tasks/'+tid+'/confirm',headers=headers,json={'decision_version':approval['decisionVersion'],
                    'decisions':[{'tool_call_id':calls[0]['id'],'approved':True}]})
                assert response.status_code==200,response.text
            second_name='b-'+mode;second=await launch('worker',second_name)
            second_ready=await wait_file(E/('worker-'+second_name+'-ready.json'),second)
            row={'case':mode,'task_id':tid,'before':before,'barrier':barrier,'approval':approval,'killed_pid':first.pid,
                 'worker_b':second_ready,'signal_exit':first.returncode}
            write('boundary-'+mode+'.json',row)
            if mode=='release-after':
                async with asyncio.timeout(POLICY['recovery_deadline_seconds']):
                    while (await state(tid))['task_status']!='completed':
                        assert second.returncode is None
                        await asyncio.sleep(.1)
                after=await state(tid)
                assert after['reply_id']==before['reply_id'] and after['session_id']==before['session_id']
                assert after['execution_epoch']==before['execution_epoch']+1
                initial=json.loads((E/(name+'-workspace.json')).read_text())
                resumed=json.loads((E/(second_name+'-resume.json')).read_text())
                for field in ('identity','generation','path','inode','provider'):assert initial[field]==resumed[field],field
                assert resumed['owner']['worker_id']!=initial['owner']['worker_id']
                assert resumed['owner']['worker_id']==second_ready['worker_id']
                assert resumed['owner']['task_id']==initial['owner']['task_id']==tid
                async with asyncio.timeout(POLICY['settlement_deadline_seconds']):
                    while True:
                        artifacts=await http.get('/api/artifacts',headers=headers,params={'task_id':tid})
                        assert artifacts.status_code==200
                        matches=[a for a in artifacts.json() if a['name']==key+'.txt']
                        if matches:break
                        await asyncio.sleep(.05)
                assert len(matches)==1,artifacts.text
                downloaded=await http.get('/api/artifacts/'+matches[0]['id']+'/download',headers=headers)
                assert downloaded.status_code==200
                row['file']=verify_file(key,downloaded.content,1024,E)
                row['recovered_workspace']=resumed
            else:
                await asyncio.sleep(POLICY['unsafe_observation_seconds'])
                after=await state(tid)
                for field in ('owner_id','execution_epoch','session_id','reply_id'):assert after[field]==before[field],(field,after,before)
                assert await admin.fetchval('SELECT lease_until<clock_timestamp() FROM runtime_sessions WHERE id=$1',before['session_id'])
                assert not (E/(second_name+'-resume.json')).exists()
                effects=[line for path in E.glob('write-effects-*.jsonl') for line in path.read_text().splitlines() if key in line]
                assert effects==[]
                assert await admin.fetchval("SELECT status FROM runtime_commands WHERE task_id=$1 AND kind='StartTask'",tid)==start_status
                row['effect_count']=0;row['manual_recovery_required']=True
            if mode=='release-after':
                async with asyncio.timeout(POLICY['settlement_deadline_seconds']):
                    while await admin.fetchval("SELECT count(*) FROM command_deliveries d JOIN runtime_commands c ON c.id=d.command_id WHERE c.task_id=$1 AND d.status<>'settled'",tid):await asyncio.sleep(.05)
            row['commands']=[dict(r) for r in await admin.fetch('SELECT id,kind,status,claimed_by,claimed_epoch FROM runtime_commands WHERE task_id=$1 ORDER BY created_at',tid)]
            row['deliveries']=[dict(r) for r in await admin.fetch('SELECT command_id,delivery_generation,status,error_code FROM command_deliveries WHERE command_id IN (SELECT id FROM runtime_commands WHERE task_id=$1)',tid)]
            expected=DELIVERY_POLICY[mode]
            assert {c['kind'] for c in row['commands']}==set(expected)
            by_id={c['id']:c for c in row['commands']}
            for command in row['commands']:assert command['status'] in expected[command['kind']]['command'],command
            for delivery in row['deliveries']:
                assert delivery['delivery_generation']==1,delivery
                assert [delivery['status'],delivery['error_code']] in expected[by_id[delivery['command_id']]['kind']]['delivery'],delivery
            allowed_ids={c['id'] for old in results for c in old['commands']} | set(by_id)
            unsafe_ids={c['id'] for old in results if old['case']!='release-after' for c in old['commands']}
            if mode!='release-after':unsafe_ids.update(by_id)
            row['redis_command_streams']=[]
            async for stream in redis.scan_iter(match='workmate:'+os.environ['WORKMATE_LOAD_NAMESPACE']+':*',_type='stream'):
                if stream.rsplit(':',1)[-1] not in {'ready','execute','control'}:continue
                entries=await redis.xrange(stream);mapping={}
                for mid,fields in entries:
                    envelope=json.loads(fields['payload']);assert envelope['command_id'] in allowed_ids,envelope
                    mapping[mid]=envelope
                pending=await redis.xpending_range(stream,'workers','-','+',100)
                for entry in pending:assert mapping[entry['message_id']]['command_id'] in unsafe_ids,entry
                row['redis_command_streams'].append({'stream':stream,'entries':mapping,'pending':pending})
            row['expected_pending_command_ids']=sorted(unsafe_ids)
            row['after']=after;row['elapsed_seconds']=time.monotonic()-started;row['passed']=True
            write('boundary-'+mode+'.json',row)
            results.append(row);write('boundary-results.json',{'passed':False,'cases':results})
            second.terminate();await asyncio.wait_for(second.wait(),45);assert second.returncode==0
        write('boundary-results.json',{'passed':True,'cases':results,'policy':POLICY,'model_calls_external':0})
    finally:
        for proc in reversed(children):
            if proc.returncode is None:proc.terminate()
        for proc in reversed(children):
            if proc.returncode is None:
                try:await asyncio.wait_for(proc.wait(),45)
                except TimeoutError:proc.kill();await proc.wait()
        if http:await http.aclose()
        await engine.dispose();await admin.close();await redis.aclose()
        for output in outputs:output.close()

asyncio.run(main())
