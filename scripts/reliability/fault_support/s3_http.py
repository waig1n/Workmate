"""Real HTTP uploads/downloads and PG metadata during an owned S3 outage."""
import asyncio
import hashlib
import json
import os
import signal
from pathlib import Path
import sys
import time

import asyncpg
import httpx
import jwt
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

E = Path('/evidence')
SECRET = 'fault-fixture-only-no-real-secret-000000000000'
DATA = b'FAULT original artifact\n'
UPLOAD = b'FAULT actual multipart attachment\n'
METADATA_TABLES = ('tasks', 'runtime_commands', 'artifacts', 'task_messages',
                   'task_artifact_deliveries', 'workspace_checkpoints', 'runtime_delivery_seals',
                   'runtime_delivery_captures')


def expected_role_exit(code, terminated):
    # Uvicorn completes lifespan shutdown then re-raises the received SIGTERM.
    return code == 0 or (terminated and code == -signal.SIGTERM)


def write(name, value):
    (E/name).write_text(json.dumps(value, default=str, indent=2))


async def main():
    db = await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://'))
    engine = create_async_engine(os.environ['DATABASE_URL'])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    children, handles = [], []
    async def launch(role):
        handle = (E/(role+'.log')).open('wb'); handles.append(handle)
        children.append(await asyncio.create_subprocess_exec(sys.executable, '/fault/role.py', role,
            env={**os.environ, 'JWT_SECRET':SECRET}, stdout=handle, stderr=asyncio.subprocess.STDOUT))
    async def wait(check, seconds=90):
        async with asyncio.timeout(seconds):
            while not await check():
                assert all(p.returncode is None for p in children), 'Role exited'
                await asyncio.sleep(.1)
    async def marker(name): return (E/name).exists()
    async def counts():
        return {table:await db.fetchval('SELECT count(*) FROM '+table)
            for table in METADATA_TABLES}
    headers={'Authorization':'Bearer '+jwt.encode({'sub':'s3-user','ver':0,'iat':int(time.time()),
        'exp':int(time.time())+3600},SECRET,algorithm='HS256')}
    result = None
    try:
        await launch('worker')
        async with httpx.AsyncClient(timeout=3) as probe:
            async def worker_ready():
                try: return (await probe.get('http://127.0.0.1:8000/health')).status_code==200
                except httpx.HTTPError: return False
            await wait(worker_ready)
        await launch('gateway')
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8080',headers=headers,timeout=30) as http:
            async def healthy():
                try:
                    return (await http.get('/health')).status_code==200 and (await http.get('http://127.0.0.1:8000/health')).status_code==200
                except httpx.HTTPError: return False
            await wait(healthy)
            from workmate_product.persistence.models import User, Scope, ScopeClosure, ScopeMembership, Agent
            async with sessions.begin() as tx:
                tx.add(User(id='s3-user',email='s3@example.test',password_hash='x',display_name='S3'))
                await tx.flush()
                tx.add(Scope(id='s3-scope',kind='personal',owner_user_id='s3-user',name='S3'))
                await tx.flush()
                tx.add_all([ScopeClosure(ancestor_id='s3-scope',descendant_id='s3-scope',depth=0),
                    ScopeMembership(scope_id='s3-scope',user_id='s3-user'),
                    Agent(id='s3-agent',slug='s3-http',display_name='S3',scope_id='s3-scope',
                          created_by='s3-user',visibility='private',system_prompt='Use controlled fixture tool.')])
            response=await http.post('/api/conversations',json={'agent_id':'s3-agent','model_profile':'fixture','title':'S3 HTTP fault'})
            assert response.status_code==201,response.text
            cid=response.json()['id']
            response=await http.post(f'/api/tasks/conversations/{cid}/tasks',json={'message':'FAULT_SAVE'})
            assert response.status_code==202,response.text
            tid=response.json()['task']['id']; approved=set()
            async def complete():
                state=await db.fetchval('SELECT status FROM tasks WHERE id=$1',tid)
                if state=='awaiting_permission':
                    pending=await http.get('/api/tasks/'+tid+'/pending-confirm')
                    if pending.status_code==404: return False
                    assert pending.status_code==200,pending.text
                    body=pending.json(); version=body['decisionVersion']
                    if version not in approved:
                        assert all(c['id']=='fault-tool' and c['name']=='Bash' for c in body['toolCalls']),body
                        confirmation=await http.post('/api/tasks/'+tid+'/confirm',json={'decision_version':version,
                            'decisions':[{'tool_call_id':c['id'],'approved':True} for c in body['toolCalls']]})
                        assert confirmation.status_code==200,confirmation.text
                        approved.add(version)
                assert state not in ('failed','cancelled'),state
                return state=='completed'
            await wait(complete)
            async def visible(): return await db.fetchval('SELECT count(*) FROM artifacts WHERE task_id=$1',tid)>0
            await wait(visible,5)
            artifact=await db.fetchrow('SELECT id,storage_key FROM artifacts WHERE task_id=$1 AND name=$2',tid,'report.txt')
            assert artifact is not None
            url='/api/artifacts/'+artifact['id']+'/download'
            response=await http.get(url); assert response.status_code==200 and response.content==DATA,(response.status_code,response.text)
            before=await counts()
            await asyncio.sleep(.5)
            assert await counts()==before, 'Metadata must settle before outage injection'
            write('before.json',before)
            write('pause-request.json',{'time':time.monotonic()})
            await wait(lambda:marker('paused.json'),10)
            started=time.monotonic()
            upload=await http.post(f'/api/tasks/conversations/{cid}/tasks-with-files',
                data={'message':'FAULT_SAVE'},files={'files':('fault.txt',UPLOAD,'text/plain')})
            upload_seconds=time.monotonic()-started
            assert upload.status_code==502,upload.text
            assert 'publication failed' in upload.text and 'AttributeError' not in upload.text,upload.text
            started=time.monotonic(); download=await http.get(url); download_seconds=time.monotonic()-started
            assert download.status_code==410,download.text
            after=await counts(); assert after==before,(before,after)
            assert upload_seconds<15 and download_seconds<15,(upload_seconds,download_seconds)
            write('outage.json',{'upload_status':upload.status_code,'download_status':download.status_code,
                'upload_seconds':upload_seconds,'download_seconds':download_seconds,'before':before,'after':after,
                'task_id':tid,'artifact_id':artifact['id'],'budget_seconds':15})
            write('resume-request.json',{'time':time.monotonic()}); await wait(lambda:marker('resumed.json'),90)
            response=await http.get(url); assert response.status_code==200 and response.content==DATA,response.text
            upload=await http.post(f'/api/tasks/conversations/{cid}/tasks-with-files',
                data={'message':'FAULT_SAVE'},files={'files':('recovered.txt',UPLOAD,'text/plain')})
            assert upload.status_code==202,upload.text
            new_tid=upload.json()['task']['id']; assert new_tid!=tid
            # Verify bytes from the real S3 object, not only an HTTP admission code.
            import boto3
            s3=boto3.client('s3',endpoint_url=os.environ['ARTIFACT_S3_ENDPOINT'],
                aws_access_key_id=os.environ['ARTIFACT_S3_ACCESS_KEY'],aws_secret_access_key=os.environ['ARTIFACT_S3_SECRET_KEY'])
            objects=s3.list_objects_v2(Bucket='fault-artifacts',Prefix=f'.inputs/{cid}/{new_tid}/').get('Contents',[])
            assert len(objects)==1,objects
            obj=s3.get_object(Bucket='fault-artifacts',Key=objects[0]['Key']); body=obj['Body']
            try: assert body.read()==UPLOAD
            finally: body.close()
            tid=new_tid; approved.clear(); await wait(complete)
            result = {'passed':True,'real_model_calls':0,'upload_failure_http':502,'download_failure_http':410,
                'restored_upload_http':202,'restored_download_sha256':hashlib.sha256(DATA).hexdigest(),
                'uploaded_bytes_sha256':hashlib.sha256(UPLOAD).hexdigest(),'new_task':new_tid}
    finally:
        errors = []
        terminated = set()
        for p in reversed(children):
            if p.returncode is None:
                p.terminate(); terminated.add(p.pid)
        for p in reversed(children):
            try: await asyncio.wait_for(p.wait(),15)
            except TimeoutError:
                p.kill(); await p.wait(); errors.append('role shutdown timed out')
            if not expected_role_exit(p.returncode, p.pid in terminated): errors.append('role exit '+str(p.returncode))
        for handle in handles: handle.close()
        await engine.dispose(); await db.close()
        write('role-cleanup.json', {'errors':errors,'exit_codes':[p.returncode for p in children],'terminated_by_controller':sorted(terminated)})
        if result is not None:
            result['role_cleanup_errors'] = errors
            result['passed'] = not errors
            write('result.json', result)
        assert not errors, errors


if __name__=='__main__': asyncio.run(main())
