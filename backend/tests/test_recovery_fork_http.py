"""Real PostgreSQL admission behind the recovery HTTP boundary."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from workmate_product.persistence.models import User
from app.auth import get_current_user
from app.db import get_db
from app.api.routers.recovery import router
from app.services.execution_admission import TaskAdmission
from test_native_admission import admission_database
from test_durable_projection import projection_database
from test_node_placement import Directory

async def prepare(sessions,admin,monkeypatch):
    await admin.execute("UPDATE tasks SET status='failed',error=$1::json WHERE id='t'",json.dumps({'code':'runtime_execution_quarantined'}))
    await admin.execute("INSERT INTO task_messages(id,task_id,conversation_id,role,content) VALUES ('input','t','c','user','{\"text\":\"retry me\"}'::json)")
    from app.config import get_settings
    settings=get_settings().model_copy(deep=True)
    settings.artifact_storage.workspace_checkpoint_enabled=True
    monkeypatch.setattr('app.services.recovery_forks.get_settings',lambda:settings)
    monkeypatch.setattr('app.services.recovery_forks.artifact_store_from_settings',lambda _:object())
    monkeypatch.setattr('app.services.recovery_forks._verified_manifest',lambda *args:{'version':1,'directories':['input','working','output'],'files':[],'total_bytes':0})
    admission=TaskAdmission(sessions,storage_node_id='node',node_directory=Directory())
    app=FastAPI();app.include_router(router)
    app.state.product_runtime=SimpleNamespace(admission=admission)
    async def db_override():
        async with sessions() as db: yield db
    async def user_override():
        return SimpleNamespace(id='u')
    app.dependency_overrides[get_db]=db_override
    app.dependency_overrides[get_current_user]=user_override
    return app,admission


async def test_explicit_ack_and_concurrent_retry_preserve_old_owner(admission_database,monkeypatch):
    sessions,admin=admission_database
    app,admission=await prepare(sessions,admin,monkeypatch)
    old=dict(await admin.fetchrow("SELECT * FROM runtime_sessions WHERE id='s'"))
    request={'request_id':str(uuid4()),'acknowledge_data_loss':True}
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/conversations/c/recovery',json={**request,'acknowledge_data_loss':False})).status_code==422
        results=await asyncio.gather(*(client.post('/conversations/c/recovery',json=request) for _ in range(2)),return_exceptions=True)
        assert all(not isinstance(r,BaseException) for r in results),results
        assert [r.status_code for r in results]==[202,202], [r.text for r in results]
        assert results[0].json()==results[1].json()
        result=results[0].json()
        assert (await client.get('/conversations/'+result['conversation_id']+'/recovery')).json()['memory_disabled'] is True
    previous=dict(await admin.fetchrow("SELECT * FROM runtime_sessions WHERE id='s'"))
    assert previous.pop('superseded_at') is not None
    old.pop('superseded_at')
    assert previous==old
    assert result['conversation_id']=='c'
    assert await admin.fetchval('SELECT count(*) FROM conversations')==2
    assert await admin.fetchval('SELECT count(*) FROM recovery_forks')==1
    assert await admin.fetchval("SELECT content->>'text' FROM task_messages WHERE task_id=$1 AND role='user'",result['task_id'])=='retry me'
    frozen=json.loads(await admin.fetchval('SELECT payload FROM runtime_execution_specs WHERE task_id=$1',result['task_id']))
    assert frozen['recovery_context_text'].startswith('[恢复上下文')
    current_sid=await admin.fetchval('SELECT id FROM runtime_sessions WHERE conversation_id=$1 AND superseded_at IS NULL',result['conversation_id'])
    cursor=await admin.fetchrow('SELECT session_id,last_runtime_seq FROM runtime_projection_cursors WHERE conversation_id=$1',result['conversation_id'])
    assert cursor['session_id']==current_sid and cursor['last_runtime_seq']==0
    assert await admin.fetchval('SELECT storage_node_id FROM runtime_sessions WHERE conversation_id=$1 AND superseded_at IS NULL',result['conversation_id'])=='two'
    assert await admin.fetchval('SELECT count(*) FROM runtime_memory_namespaces')==0
    from workmate_product.persistence.repositories.node_placement import NodePlacementRepository
    async with sessions() as db:
        assert await NodePlacementRepository(db).nodes('u','a')=={'node'}


async def test_admission_rejection_rolls_back_new_fork(admission_database,monkeypatch):
    sessions,admin=admission_database
    app,admission=await prepare(sessions,admin,monkeypatch)
    async def fail(*args,**kwargs):
        from fastapi import HTTPException
        raise HTTPException(429,'capacity')
    monkeypatch.setattr(admission,'create_task',fail)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        result=await client.post('/conversations/c/recovery',json={'request_id':str(uuid4()),'acknowledge_data_loss':True})
        assert result.status_code==429
        assert (await client.get('/conversations/unknown/recovery')).status_code==404
    assert await admin.fetchval('SELECT count(*) FROM recovery_forks')==0
    assert await admin.fetchval('SELECT count(*) FROM conversations')==2
    assert await admin.fetchval("SELECT superseded_at FROM runtime_sessions WHERE id='s'") is None
    assert await admin.fetchval("SELECT active_product_task_id FROM conversations WHERE id='c'")=='t'


async def test_revoked_actor_cannot_fork(admission_database,monkeypatch):
    sessions,admin=admission_database
    app,_=await prepare(sessions,admin,monkeypatch)
    await admin.execute("UPDATE users SET status='disabled' WHERE id='u'")
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        result=await client.post('/conversations/c/recovery',json={'request_id':str(uuid4()),'acknowledge_data_loss':True})
        assert result.status_code==403
    assert await admin.fetchval('SELECT count(*) FROM recovery_forks')==0


async def test_missing_file_never_creates_fork_or_command(admission_database,monkeypatch):
    sessions,admin=admission_database
    app,_=await prepare(sessions,admin,monkeypatch)
    def unavailable(*args):
        raise ValueError('missing object')
    monkeypatch.setattr('app.services.recovery_forks._verified_manifest',unavailable)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        result=await client.post('/conversations/c/recovery',json={'request_id':str(uuid4()),'acknowledge_data_loss':True})
        assert result.status_code==409
    assert await admin.fetchval('SELECT count(*) FROM recovery_forks')==0
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0


async def test_other_user_cannot_preview_or_recover(admission_database,monkeypatch):
    sessions,admin=admission_database
    app,_=await prepare(sessions,admin,monkeypatch)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id='other')
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/conversations/c/recovery')).status_code==404
        result=await client.post('/conversations/c/recovery',json={'request_id':str(uuid4()),'acknowledge_data_loss':True})
        assert result.status_code==404
    assert await admin.fetchval('SELECT count(*) FROM recovery_forks')==0


async def test_http_fork_restores_real_object_bytes_with_source_directory_gone(admission_database,monkeypatch,tmp_path):
    from hashlib import sha256
    import shutil
    from workmate_runtime.delivery.workspace_checkpoint import WorkspaceCheckpoint as Files
    from workmate_runtime.assembly.workspace_recovery import restore_recovery_workspace
    from workmate_product.persistence.checkpoint_models import WorkspaceCheckpoint
    from workmate_product.persistence.models import RuntimeSession
    from workmate_product.persistence.recovery_forks import RecoveryFork
    from sqlalchemy import select
    from app.services.recovery_forks import _verified_manifest
    sessions,admin=admission_database
    app,_=await prepare(sessions,admin,monkeypatch)
    # Use actual object validation, not the transport-only tests' empty-manifest seam.
    monkeypatch.setattr('app.services.recovery_forks._verified_manifest',_verified_manifest)
    objects={}
    class Store:
        def publish_file(self,key,source,digest,size):
            source.seek(0);data=source.read()
            assert len(data)==size and sha256(data).hexdigest()==digest
            objects[key]=data
        def snapshot(self,key,digest,size):
            data=objects[key]
            assert len(data)==size and sha256(data).hexdigest()==digest
            path=tmp_path/str(uuid4());path.write_bytes(data)
            return SimpleNamespace(path=path,close=lambda:path.unlink())
    store=Store()
    monkeypatch.setattr('app.services.recovery_forks.artifact_store_from_settings',lambda _:store)
    source=tmp_path/'old-node';(source/'working').mkdir(parents=True)
    (source/'working'/'report.txt').write_text('上次已保存的报告',encoding='utf-8')
    manifest=Files(store,max_files=10,max_bytes=10000).capture(source)
    digest=sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    async with sessions.begin() as db:
        db.add(WorkspaceCheckpoint(id=str(uuid4()),conversation_id='c',task_id='t',session_id='s',
            execution_epoch=1,storage_node_id='node',boundary='approval',status='available',manifest=manifest,digest=digest))
    shutil.rmtree(source)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        result=await client.post('/conversations/c/recovery',json={'request_id':str(uuid4()),'acknowledge_data_loss':True})
        assert result.status_code==202,result.text
        receipt=result.json()
    async with sessions.begin() as db:
        session=await db.scalar(select(RuntimeSession).where(RuntimeSession.conversation_id==receipt['conversation_id'],RuntimeSession.superseded_at.is_(None)))
        session.owner_id='new-worker';session.execution_epoch=1;session.active_task_id=receipt['task_id']
        session_id=session.id
        fork=await db.scalar(select(RecoveryFork).where(RecoveryFork.target_session_id==session_id))
        fork_id=fork.id
    payload=json.loads(await admin.fetchval('SELECT payload FROM runtime_execution_specs WHERE task_id=$1',receipt['task_id']))
    assert payload['recovery_fork_id']==fork_id
    root=tmp_path/'new-node';root.mkdir()
    for name in ['input','working','output']:(root/name).mkdir()
    context=SimpleNamespace(conversation_id=receipt['conversation_id'],task_id=receipt['task_id'],session_id=session_id,
        actor_user_id='u',initial_spec=SimpleNamespace(**payload))
    binding=SimpleNamespace(owner=SimpleNamespace(task_id=receipt['task_id'],session_id=session_id,worker_id='new-worker',execution_epoch=1),
        workspace=SimpleNamespace(host_workdir=str(root)))
    from app.services.recovery_forks import get_settings
    await restore_recovery_workspace(context,binding,sessions,store,get_settings().artifact_storage,storage_node_id='two')
    assert (root/'working'/'report.txt').read_text()=='上次已保存的报告'
    assert not source.exists()
    # A second failure before its first checkpoint must retain the inherited files.
    await admin.execute("UPDATE tasks SET status='failed',error=$2::json WHERE id=$1",receipt['task_id'],json.dumps({'code':'runtime_execution_quarantined'}))
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        again=await client.post('/conversations/'+receipt['conversation_id']+'/recovery',
            json={'request_id':str(uuid4()),'acknowledge_data_loss':True})
        assert again.status_code==202,again.text
    inherited=json.loads(await admin.fetchval('SELECT manifest FROM recovery_forks WHERE target_task_id=$1',again.json()['task_id']))
    assert inherited==manifest


async def test_queued_tasks_must_be_cancelled_before_recovery(admission_database,monkeypatch):
    sessions,admin=admission_database
    app,_=await prepare(sessions,admin,monkeypatch)
    await admin.execute("INSERT INTO tasks(id,conversation_id,title,status) VALUES ('queued','c','queued','created')")
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        preview=await client.get('/conversations/c/recovery')
        assert preview.status_code==200 and preview.json()['eligible'] is False
        result=await client.post('/conversations/c/recovery',json={'request_id':str(uuid4()),'acknowledge_data_loss':True})
        assert result.status_code==409
    assert await admin.fetchval('SELECT count(*) FROM recovery_forks')==0
    assert await admin.fetchval("SELECT superseded_at FROM runtime_sessions WHERE id='s'") is None


async def test_distinct_concurrent_requests_cannot_recover_one_source_twice(admission_database,monkeypatch):
    sessions,admin=admission_database
    app,_=await prepare(sessions,admin,monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        responses=await asyncio.gather(*(client.post('/conversations/c/recovery',json={
            'request_id':str(uuid4()),'acknowledge_data_loss':True}) for _ in range(2)),return_exceptions=True)
        assert all(not isinstance(r,BaseException) for r in responses),responses
        assert sorted(r.status_code for r in responses)==[202,409]
    assert await admin.fetchval('SELECT count(*) FROM recovery_forks')==1
    assert await admin.fetchval("SELECT count(*) FROM runtime_sessions WHERE conversation_id='c'")==2
