"""Actual HTTP admission and approval, with a host-owned PostgreSQL pause."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import time

import asyncpg
import httpx
import jwt
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from common import guard,JWT_SECRET
from isolation import record_isolation
from proof import disappearance,identity,verify_failure,ALLOWED_FAILURES

E=Path('/evidence')
def write(name,value):(E/name).write_text(json.dumps(value,default=str,indent=2))

async def main():
    record_isolation();guard('pg-tool-parent')
    write('pg-tool-plan.json',{'lease_seconds':9,'pause_seconds':15,'tool_sleep_seconds':30,
        'tool_observation_seconds':35,'quarantine_deadline_seconds':90,'real_model_calls':0,
        'allowed_shutdown_failure':{'worker_exit':1,'classification':'expired_residency_release',
            'reason':'Expired owner must not release durable residency; exact single ResidencyConflict only'},
        'allowed_failure_pairs':ALLOWED_FAILURES,
        'scope':'dedicated PG pause only; real in-flight Bubblewrap Bash'})
    admin=await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://'))
    engine=create_async_engine(os.environ['DATABASE_URL']);sessions=async_sessionmaker(engine,expire_on_commit=False)
    children=[];logs=[];report={'passed':False}
    async def launch(script,*args,pass_fds=()):
        log=open(E/(script+'.log'),'wb');logs.append(log)
        child=await asyncio.create_subprocess_exec(sys.executable,'/support/'+script+'.py',*args,
            stdout=log,stderr=asyncio.subprocess.STDOUT,pass_fds=pass_fds,
            env={**os.environ,'PYTHONFAULTHANDLER':'1'})
        children.append(child);return child
    async def wait(predicate,seconds=90):
        async with asyncio.timeout(seconds):
            while not await predicate():
                assert all(child.returncode is None for child in children),'role exited early'
                await asyncio.sleep(.1)
    async def file_exists(path):return path.exists()
    try:
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();sock.set_inheritable(True)
        url='http://127.0.0.1:'+str(sock.getsockname()[1])
        await launch('gateway',str(sock.fileno()),pass_fds=(sock.fileno(),));sock.close()
        headers={'Authorization':'Bearer '+jwt.encode({'sub':'fixture-user','ver':0,'iat':int(time.time()),'exp':int(time.time())+3600},JWT_SECRET,algorithm='HS256')}
        async with httpx.AsyncClient(base_url=url,headers=headers,timeout=15) as http:
            async def healthy():
                try:return (await http.get('/health')).status_code==200
                except httpx.HTTPError:return False
            await wait(healthy,60)
            from workmate_product.persistence.models import User,Scope,ScopeClosure,ScopeMembership,Agent
            async with sessions.begin() as db:
                db.add(User(id='fixture-user',email='pg-tool@example.test',password_hash='x',display_name='FAULT'));await db.flush()
                db.add(Scope(id='fixture-scope',kind='personal',owner_user_id='fixture-user',name='FAULT'));await db.flush()
                db.add_all([ScopeClosure(ancestor_id='fixture-scope',descendant_id='fixture-scope',depth=0),
                    ScopeMembership(scope_id='fixture-scope',user_id='fixture-user'),
                    Agent(id='fixture-agent',slug='fault-pg-tool',display_name='FAULT',scope_id='fixture-scope',created_by='fixture-user',visibility='private',system_prompt='Use fixture tool')])
            await launch('worker');await wait(lambda:file_exists(E/'pg-worker-ready.json'))
            response=await http.post('/api/conversations',json={'agent_id':'fixture-agent','model_profile':'fixture','title':'FAULT PG in-flight'})
            assert response.status_code==201,response.text
            cid=response.json()['id']
            response=await http.post(f'/api/tasks/conversations/{cid}/tasks',json={'message':'FAULT PG tool','approval_profile':'strict'})
            assert response.status_code==202,response.text
            tid=response.json()['task']['id']
            async def approve():
                pending=await http.get('/api/tasks/'+tid+'/pending-confirm')
                if pending.status_code==404:return False
                assert pending.status_code==200,pending.text
                value=pending.json();assert len(value['toolCalls'])==1 and value['toolCalls'][0]['id']=='pg-tool'
                result=await http.post('/api/tasks/'+tid+'/confirm',json={'decision_version':value['decisionVersion'],
                    'decisions':[{'tool_call_id':'pg-tool','approved':True}]})
                assert result.status_code==200,result.text
                return True
            await wait(approve)
            workspace=json.loads((E/'pg-workspace.json').read_text());root=Path(workspace['path'])
            started_file=root/'working/pg-started.txt';completed_file=root/'working/pg-completed.txt'
            await wait(lambda:file_exists(started_file));begun=time.monotonic()
            assert started_file.read_text()=='started\n'
            processes=[json.loads(line) for line in (E/'pg-tool-processes.jsonl').read_text().splitlines()]
            assert processes and all(p['task_id']==tid and identity(p['pid'])==
                {key:p[key] for key in ('pid','starttime','pgrp')} for p in processes)
            write('pg-tool-processes-before.json',processes)
            before=dict(await admin.fetchrow('SELECT * FROM runtime_sessions WHERE id=$1',workspace['session_id']))
            assert before['status']=='running'
            write('pg-pause-request.json',{'task_id':tid,'workspace':workspace,'before':before,
                'allowed_worker_failures':{'task_id':tid,'session_id':workspace['session_id'],
                    'error_cause_pairs':ALLOWED_FAILURES},
                'allowed_shutdown_classification':'expired_residency_release'})
            await wait(lambda:file_exists(E/'pg-restored.json'),30)
            receipt=json.loads((E/'pg-restored.json').read_text());assert receipt['pause_seconds']>=15
            # Observe past the actual shell sleep; merely seeing a failed Task
            # does not establish that its operating-system subprocess stopped.
            await asyncio.sleep(max(0,35-(time.monotonic()-begun)))
            returned=(E/'pg-tool-returned.json').exists()
            effects=(E/'pg-tool-invocations.jsonl').read_text().splitlines()
            after=dict(await admin.fetchrow('SELECT * FROM runtime_sessions WHERE id=$1',workspace['session_id']))
            task_state=dict(await admin.fetchrow('SELECT status,error FROM tasks WHERE id=$1',tid))
            report.update(tool_returned=returned,tool_completed=completed_file.exists(),
                started_bytes=started_file.read_text(),invocations=len(effects),pause=receipt,
                before=before,after_observation=after,task=task_state)
            report['process_disappearance']=[disappearance(p) for p in processes]
            write('pg-tool-result.json',report)
            assert not completed_file.exists(),'Old Bash completed after authority expired'
            assert returned,'Tool invocation has not quiesced'
            assert all(p['gone'] for p in report['process_disappearance']),'Original process or process group survives'
            assert len(effects)==1 and started_file.read_text()=='started\n','Tool replayed'
            async def quarantined():
                row=await admin.fetchrow('SELECT status,error FROM tasks WHERE id=$1',tid)
                return row['status']=='failed' and json.loads(row['error'] or '{}').get('code')=='runtime_execution_quarantined'
            await wait(quarantined,max(.1,90-(time.monotonic()-begun)))
            after=dict(await admin.fetchrow('SELECT * FROM runtime_sessions WHERE id=$1',workspace['session_id']))
            for key in ('owner_id','execution_epoch','active_task_id'):assert after[key]==before[key],key
            rejected=await http.post(f'/api/tasks/conversations/{cid}/tasks',json={'message':'must not rerun','approval_profile':'strict'})
            assert rejected.status_code==409,rejected.text
            assert (await http.get('/health')).status_code==200
            report.update(passed=True,after=after,manual_handling_required=True,readmission_status=409,
                quarantine_elapsed_seconds=time.monotonic()-begun)
    except BaseException as error:
        report['error']=type(error).__name__+': '+str(error)
        raise
    finally:
        scenario_passed=report['passed'];report['passed']=False
        write('pg-tool-result.json',report)
        for child in reversed(children):
            if child.returncode is None:child.terminate()
        for child in reversed(children):
            try:await asyncio.wait_for(child.wait(),45)
            except TimeoutError:child.kill();await child.wait()
        for log in logs:log.close()
        cleanup_errors=[]
        report['role_exit_codes']=[child.returncode for child in children]
        for index,child in enumerate(children):
            if child.returncode==0:continue
            receipt=E/'pg-worker-exit.json'
            expected=(index==1 and child.returncode==1 and receipt.exists()
                and json.loads(receipt.read_text())['classification']=='expired_residency_release')
            if not expected:cleanup_errors.append('Unexpected role exit '+str(index)+': '+str(child.returncode))
        worker_state=json.loads((E/'pg-worker-state.json').read_text()) if (E/'pg-worker-state.json').exists() else {}
        if worker_state.get('ownership_remaining')!=0:cleanup_errors.append('Workspace ownership not cleaned')
        worker_log=(E/'worker.log').read_text()
        if scenario_passed:
            try:
                durable=dict(await admin.fetchrow('SELECT status,error FROM tasks WHERE id=$1',tid))
                report['failure_proof']=verify_failure(durable,worker_state.get('failures'),worker_log,
                    task_id=tid,session_id=workspace['session_id'])
            except (AssertionError,KeyError,TypeError,ValueError) as error:
                cleanup_errors.append('Worker failure proof mismatch: '+str(error))
        for text in ('Exception terminating connection','non-checked-in connection','ResourceCleanupUnproven'):
            if text in worker_log:cleanup_errors.append(text)
        report['cleanup_errors']=cleanup_errors
        report['passed']=scenario_passed and not cleanup_errors
        write('pg-tool-result.json',report)
        await engine.dispose();await admin.close()
        assert not cleanup_errors,cleanup_errors

asyncio.run(main())
