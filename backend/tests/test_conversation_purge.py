"""Real PostgreSQL purge: immutable guards, shared bytes, retries and audit."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone

import pytest
from workmate_product.persistence.models import (Conversation, Task, TaskMessage, Artifact,
    RuntimeSession, RuntimeSnapshot, ConversationRetirement, RuntimeDeliverySeal)
from workmate_product.application.conversation_purge import purge_conversation
from workmate_product.persistence.repositories.conversation_purge import ARTIFACT_STORAGE_LOCK
from test_durable_projection import projection_database


def publish(root, text):
    digest = hashlib.sha256(text.encode()).hexdigest()
    key = f'.objects/sha256/{digest[:2]}/{digest}'
    target = root/key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return key


async def prepare(sessions, admin, root):
    await admin.execute("""UPDATE tasks SET status='completed';
        UPDATE conversations SET active_product_task_id=NULL,deleted_at=clock_timestamp();
        UPDATE runtime_sessions SET active_task_id=NULL,status='completed';
        UPDATE runtime_task_executions SET status='completed';""")
    own, shared = publish(root, 'private bytes'), publish(root, 'shared bytes')
    async with sessions.begin() as db:
        db.add(Conversation(id='other', user_id='u', agent_id='a'))
        await db.flush()
        db.add(Task(id='other-task', conversation_id='other', title='keep', status='completed'))
        db.add(RuntimeSession(id='other-session', conversation_id='other', storage_node_id='node'))
        await db.flush()
        db.add(RuntimeDeliverySeal(task_id='other-task', session_id='other-session', execution_epoch=1,
            worker_id='other-worker', storage_node_id='node', digest='a'*64,
            payload={'capture': {'files': [{'storage_key': shared}]}}))
        db.add(TaskMessage(id='msg', task_id='t', conversation_id='c', role='user', content={'text': 'private'}))
        for index,key in enumerate((own,shared)):
            db.add(Artifact(id=f'art-{index}', task_id='t', conversation_id='c', name=f'{index}.txt',
                mime_type='text/plain', storage_key=key, size=13, change_type='created'))
        db.add(RuntimeSnapshot(id='snapshot', session_id='s', task_id='t', state_version=1,
            runtime_seq=1, execution_epoch=1, status='completed', reply_id='reply', payload={'secret': 'text'}))
        db.add(ConversationRetirement(conversation_id='c', session_id='s', storage_node_id='node',
            execution_epoch=1, requested_by='u', completed_at=datetime.now(timezone.utc), completed_by='worker'))
    inputs=root/'.inputs/c/t'; inputs.mkdir(parents=True); (inputs/'file').write_text('upload')
    return own, shared


async def run_io(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def purge(sessions, root, workspace=None, io=run_io):
    async def noop(*args): pass
    async with sessions.begin() as db:
        return await purge_conversation(db, conversation_id='c', storage_root=root, worker_id='worker',
            purge_workspace=workspace or noop, run_io=io)


async def test_purge_deletes_all_content_keeps_other_seal_and_audit(projection_database, tmp_path):
    sessions, admin = projection_database
    own, shared = await prepare(sessions, admin, tmp_path)
    assert await purge(sessions, tmp_path)
    assert not await purge(sessions, tmp_path)
    assert not (tmp_path/own).exists()
    assert (tmp_path/shared).read_text() == 'shared bytes'
    assert not (tmp_path/'.inputs/c').exists()
    assert await admin.fetchval('SELECT count(*) FROM task_messages') == 0
    assert await admin.fetchval('SELECT count(*) FROM artifacts') == 0
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_seals') == 1
    assert await admin.fetchval("SELECT purged_at FROM conversation_retirements WHERE conversation_id='c'")
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='conversation.purge'") == 1
    assert await admin.fetchval("SELECT title FROM tasks WHERE id='other-task'") == 'keep'


async def test_partial_file_failure_rolls_back_metadata_and_retries(projection_database, tmp_path):
    sessions, admin = projection_database
    own, shared = await prepare(sessions, admin, tmp_path)
    count = 0
    async def fail_after_unlink(fn, *args, **kwargs):
        nonlocal count
        result = await run_io(fn, *args, **kwargs)
        count += 1
        if count == 1:
            raise OSError('injected after unlink')
        return result
    with pytest.raises(OSError):
        await purge(sessions, tmp_path, io=fail_after_unlink)
    assert await admin.fetchval('SELECT count(*) FROM artifacts') == 2
    assert await admin.fetchval('SELECT purged_at FROM conversation_retirements') is None
    assert await purge(sessions, tmp_path)
    assert not (tmp_path/own).exists() and (tmp_path/shared).exists()


async def test_purge_checks_gates_before_workspace_or_files(projection_database, tmp_path):
    sessions, admin = projection_database
    own, _ = await prepare(sessions, admin, tmp_path)
    await admin.execute("UPDATE runtime_sessions SET active_task_id='t' WHERE id='s'")
    async def forbidden(*args): raise AssertionError('must not touch Workspace')
    with pytest.raises(RuntimeError, match='closed execution'):
        await purge(sessions, tmp_path, workspace=forbidden)
    assert (tmp_path/own).exists()


async def test_purge_excludes_concurrent_publication(projection_database, tmp_path):
    sessions, admin = projection_database
    await prepare(sessions, admin, tmp_path)
    async def workspace(*args):
        allowed = await admin.fetchval('SELECT pg_try_advisory_lock_shared($1)', ARTIFACT_STORAGE_LOCK)
        try:
            assert allowed is False
        finally:
            if allowed:
                await admin.execute('SELECT pg_advisory_unlock_shared($1)', ARTIFACT_STORAGE_LOCK)
    await purge(sessions, tmp_path, workspace=workspace)


async def test_immutable_snapshot_delete_requires_retirement(projection_database):
    import asyncpg
    sessions, admin = projection_database
    await admin.execute("""INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,runtime_seq,
        execution_epoch,status,payload) VALUES ('snap','s','t',1,1,1,'completed','{}')""")
    with pytest.raises(asyncpg.PostgresError):
        await admin.execute('DELETE FROM runtime_snapshots')




async def test_purge_clears_snapshot_pointers_commands_and_preserves_audit_facts(projection_database, tmp_path):
    sessions, admin = projection_database
    await prepare(sessions, admin, tmp_path)
    await admin.execute("""UPDATE runtime_sessions SET snapshot_id='snapshot',snapshot='{"secret":"old"}' WHERE id='s';
        UPDATE runtime_task_executions SET snapshot_id='snapshot',input_snapshot_id='snapshot',state_version=1 WHERE task_id='t';
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,status,payload,content_digest)
        VALUES ('cmd','s','c','t','u','StartTask','completed','{"input":"private"}',repeat('a',64));
        INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
        VALUES ('t','c',1,'tool','{"arguments":"private"}');
        INSERT INTO runtime_execution_specs(task_id,session_id,schema_version,payload,digest)
        VALUES ('t','s',1,'{"content":"private"}',repeat('a',64));
        INSERT INTO runtime_delivery_captures(task_id,session_id,execution_epoch,worker_id,storage_node_id,payload,digest,snapshot_id)
        VALUES ('t','s',1,'worker','node','{"files":[]}',repeat('a',64),'snapshot');""")
    await purge(sessions, tmp_path)
    assert await admin.fetchval("SELECT snapshot FROM runtime_sessions WHERE id='s'") is None
    assert await admin.fetchval("SELECT snapshot_id FROM runtime_task_executions WHERE task_id='t'") is None
    assert json.loads(await admin.fetchval('SELECT payload FROM runtime_commands')) == {'purged': True}
    assert json.loads(await admin.fetchval('SELECT payload FROM task_events')) == {'purged': True}
    assert await admin.fetchval('SELECT seq FROM task_events') == 1
    assert await admin.fetchval('SELECT count(*) FROM runtime_execution_specs') == 0
    assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures') == 0



def test_purge_retains_approval_and_tool_identity_but_not_original_arguments():
    from workmate_product.application.conversation_purge import retained_audit
    assert retained_audit({'actorUserId': 'u', 'decisionVersion': 'v',
        'decisions': [{'toolCallId': 'call', 'approved': False, 'reason': 'private'}],
        'toolCalls': [{'id': 'call', 'name': 'Bash', 'input': 'private command'}],
        'arguments': 'private', 'text': 'private', 'unknown': {'text': 'private'}}) == {
        'purged': True, 'actorUserId': 'u', 'decisionVersion': 'v',
        'decisions': [{'toolCallId': 'call', 'approved': False}],
        'toolCalls': [{'id': 'call', 'name': 'Bash'}]}


async def test_s3_purge_deletes_only_unshared_objects_without_local_volume(projection_database,tmp_path):
    sessions,admin = projection_database
    own,shared = await prepare(sessions,admin,tmp_path)
    calls = []
    class Store:
        def delete(self,key): calls.append(('object',key))
        def delete_inputs(self,conversation): calls.append(('inputs',conversation))
    async def noop(*args): pass
    async with sessions.begin() as db:
        assert await purge_conversation(db,conversation_id='c',storage_root='/not-mounted',worker_id='worker',
            purge_workspace=noop,run_io=run_io,object_store=Store())
    assert calls == [('object',own),('inputs','c')]
    assert shared not in [value for _,value in calls]
    assert (tmp_path/own).exists()  # local content was not silently selected


async def test_purge_removes_historical_suspension_before_snapshot(projection_database, tmp_path):
    sessions, admin = projection_database
    await prepare(sessions, admin, tmp_path)
    await admin.execute('''INSERT INTO runtime_suspensions
        (snapshot_id,session_id,task_id,reply_id,storage_node_id,source_worker_id,source_epoch,
         state_version,runtime_seq,resource_generation,snapshot_digest,status)
        VALUES ('snapshot','s','t','reply','node','old-worker',1,1,1,'generation',repeat('a',64),'blocked')''')
    assert await purge(sessions, tmp_path)
    assert await admin.fetchval('SELECT count(*) FROM runtime_suspensions') == 0
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots') == 0


@pytest.mark.parametrize('status',['held','releasing','blocked','released'])
async def test_purge_requires_released_workspace_and_removes_only_closed_evidence(projection_database,tmp_path,status):
    sessions,admin=projection_database
    own,_=await prepare(sessions,admin,tmp_path)
    await admin.execute("""INSERT INTO workspace_residencies(session_id,storage_node_id,workspace_generation,
        holder_worker_id,hold_version,status,release_request_id,last_task_id,last_execution_epoch)
        VALUES ('s','node','generation','worker',1,$1::varchar,
        CASE WHEN $1::varchar='held' THEN NULL ELSE 'release' END,'t',1)""",status)
    await admin.execute("""INSERT INTO workspace_release_requests(id,session_id,storage_node_id,expected_worker_id,
        expected_hold_version,workspace_generation,status,cleanup_receipt,completed_at)
        VALUES ('release','s','node','worker',1,'generation','released','{}',now())""")
    if status=='released':
        assert await purge(sessions,tmp_path)
        assert await admin.fetchval('SELECT count(*) FROM workspace_residencies')==0
        assert await admin.fetchval('SELECT count(*) FROM workspace_release_requests')==0
    else:
        async def forbidden(*args): raise AssertionError('must not touch live Workspace')
        with pytest.raises(RuntimeError,match='Workspace'):
            await purge(sessions,tmp_path,workspace=forbidden)
        assert (tmp_path/own).exists()
        assert await admin.fetchval('SELECT count(*) FROM workspace_residencies')==1
        assert await admin.fetchval('SELECT count(*) FROM workspace_release_requests')==1


async def test_purge_preserves_orphan_unresolved_release_evidence(projection_database,tmp_path):
    sessions,admin=projection_database
    own,_=await prepare(sessions,admin,tmp_path)
    await admin.execute("""INSERT INTO workspace_release_requests(id,session_id,storage_node_id,expected_worker_id,
        expected_hold_version,workspace_generation) VALUES ('release','s','node','worker',1,'generation')""")
    with pytest.raises(RuntimeError,match='Workspace release evidence'):
        await purge(sessions,tmp_path)
    assert (tmp_path/own).exists()
    assert await admin.fetchval('SELECT count(*) FROM workspace_release_requests')==1


async def stage_realtime(sessions,admin):
    from uuid import uuid4
    from workmate_product.projections.store import append_business_events,ProjectedEvent
    from workmate_product.persistence.repositories import realtime_outbox
    generation,incarnation=str(uuid4()),str(uuid4())
    await admin.execute('''INSERT INTO conversation_event_logs
        (conversation_id,generation,incarnation,version,status,initialization_token)
        VALUES ('c',$1,$2,1,'active',$3)''',generation,incarnation,str(uuid4()))
    async with sessions.begin() as db:
        await append_business_events(db,'c',operation_key='realtime-test',items=[ProjectedEvent(
            key='terminal',event_type='CUSTOM',payload={'type':'CUSTOM','name':'TASK_COMPLETED'})],
            task_id='t',reply_id='reply')
    async with sessions.begin() as db:
        claims=await realtime_outbox.lease_due(db)
    assert len(claims)==1
    return claims[0],generation,incarnation


async def test_purge_removes_log_and_outbox_before_referenced_events(projection_database,tmp_path):
    sessions,admin=projection_database
    await prepare(sessions,admin,tmp_path)
    await stage_realtime(sessions,admin)
    assert await purge(sessions,tmp_path)
    for table in ('conversation_event_logs','realtime_business_outbox','product_events'):
        assert await admin.fetchval(f'SELECT count(*) FROM {table}')==0
    # Identities retained for audit must never be reused, even after content purge.
    assert await admin.fetchval("SELECT business_seq FROM conversations WHERE id='c'")==1


async def test_purge_waits_for_log_before_outbox_publication_receipt(projection_database,tmp_path):
    from sqlalchemy import text
    from workmate_product.persistence.repositories import realtime_outbox
    sessions,admin=projection_database
    await prepare(sessions,admin,tmp_path)
    claim,generation,incarnation=await stage_realtime(sessions,admin)
    entered=asyncio.Event()
    purge_pid=None
    async def concurrent_purge():
        nonlocal purge_pid
        async with sessions.begin() as db:
            purge_pid=await db.scalar(text('SELECT pg_backend_pid()'))
            entered.set()
            async def noop(*args): pass
            return await purge_conversation(db,conversation_id='c',storage_root=tmp_path,
                worker_id='worker',purge_workspace=noop,run_io=run_io)
    task=None
    try:
        async with sessions.begin() as receipt:
            await receipt.execute(text("SELECT * FROM conversation_event_logs WHERE conversation_id='c' FOR SHARE"))
            task=asyncio.create_task(concurrent_purge())
            await asyncio.wait_for(entered.wait(),5)
            for _ in range(200):
                if await admin.fetchval('SELECT cardinality(pg_blocking_pids($1))>0',purge_pid):
                    break
                await asyncio.sleep(.01)
            else:
                raise AssertionError('Purge did not wait for the publication log lock')
            assert not task.done()
            # A reverse lock order would make this wait on purge's outbox lock.
            assert await asyncio.wait_for(realtime_outbox.published(receipt,claim,
                generation=generation,incarnation=incarnation,stream_id='1-0'),3)
        assert await asyncio.wait_for(task,5)
        assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox')==0
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task,return_exceptions=True)


async def test_purge_checkpoint_keeps_fork_owned_files(projection_database, tmp_path):
    from workmate_product.persistence.models import WorkspaceCheckpoint
    from workmate_product.persistence.recovery_forks import RecoveryFork
    sessions, admin = projection_database
    await prepare(sessions, admin, tmp_path)
    shared = publish(tmp_path, 'checkpoint used by recovered conversation')
    private = publish(tmp_path, 'checkpoint only')
    async with sessions.begin() as db:
        db.add(WorkspaceCheckpoint(id='cp', conversation_id='c',task_id='t',session_id='s',
            execution_epoch=1,storage_node_id='node',boundary='terminal',status='available',
            manifest={'files':[{'storage_key':shared},{'storage_key':private}]},digest='a'*64))
        await db.flush()
        db.add(RecoveryFork(id='fork', conversation_id='other',source_conversation_id='c',
            source_session_id='s',target_session_id='other-session',context_text='',
            source_task_id='t',target_task_id='other-task',target_node_id='node',actor_user_id='u',
            request_key='request',checkpoint_id='cp',manifest={'files':[{'storage_key':shared}]}))
    assert await purge(sessions,tmp_path)
    assert (tmp_path/shared).exists()
    assert not (tmp_path/private).exists()
    assert await admin.fetchval('SELECT count(*) FROM workspace_checkpoints') == 0
    row=await admin.fetchrow("SELECT checkpoint_id,manifest FROM recovery_forks WHERE id='fork'")
    assert row['checkpoint_id'] is None
    assert shared in row['manifest']


async def test_historical_workspace_blocks_purge_before_storage_io(projection_database, tmp_path):
    from workmate_product.persistence.repositories.conversation_purge import lock_purge
    sessions, admin = projection_database
    await prepare(sessions, admin, tmp_path)
    await admin.execute("""UPDATE runtime_sessions SET superseded_at=clock_timestamp() WHERE id='s';
        INSERT INTO runtime_sessions(id,conversation_id,storage_node_id) VALUES ('new','c','node');""")
    async with sessions.begin() as db:
        with pytest.raises(RuntimeError, match='every historical Workspace'):
            await lock_purge(db, 'c')
    assert await admin.fetchval("SELECT count(*) FROM runtime_sessions WHERE conversation_id='c'") == 2
    assert list(tmp_path.rglob('*'))
