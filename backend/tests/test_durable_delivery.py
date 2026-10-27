"""Delivery closure projects immutable versions and releases both gates atomically."""
import asyncio
import json

import pytest
from sqlalchemy import select

from workmate_product.application.delivery import close_delivery
from workmate_product.application.delivery_metadata import DeliveryConflict,_digest
from workmate_product.persistence.models import Artifact, TaskArtifactDelivery
from test_durable_projection import projection_database
from test_durable_lifecycle import stream,boundary,consume,START,END


async def seed(sessions,admin, *, include_pdf=False):
    await stream(sessions,admin,[START,END])
    seq=await boundary(admin)
    await consume(sessions)
    await admin.execute("""UPDATE runtime_sessions SET status='completed',owner_id='worker',snapshot_id='safe',state_version=1;
        UPDATE runtime_task_executions SET status='completed',snapshot_id='safe',state_version=1;""")
    payload={'version':1,'source':'explicit','declared_files':['report.md'],'warnings':[],
        'files':[{'name':'report.md','size':3,'sha256':'a'*64,'storage_key':'.objects/sha256/aa/'+('a'*64),'change_type':'created'}]}
    if include_pdf:
        payload['declared_files'].append('report.pdf')
        payload['files'].append({**payload['files'][0], 'name':'report.pdf', 'sha256':'b'*64,
            'storage_key':'.objects/sha256/bb/'+('b'*64)})
    await admin.execute('''INSERT INTO runtime_delivery_captures(task_id,session_id,execution_epoch,worker_id,
        storage_node_id,payload,digest,snapshot_id) VALUES ('t','s',1,'worker','node',$1::jsonb,$2,'safe')''',json.dumps(payload),_digest(payload))
    return payload


async def close(sessions):
    async with sessions.begin() as db:
        return await close_delivery(db,'c','t')


async def test_competing_closers_project_once_and_release_gates(projection_database):
    sessions,admin=projection_database
    await seed(sessions,admin)
    first,second=await asyncio.gather(close(sessions),close(sessions))
    assert first==second and len(first)==1
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') is None
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') is None
    assert await admin.fetchval('SELECT count(*) FROM artifacts')==1
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE payload->>'name'='ARTIFACT_CREATED'")==1
    async with sessions() as db:
        item=await db.scalar(select(Artifact))
        assert item.name=='report.md' and item.mime_type=='text/markdown'
        assert item.storage_key=='.objects/sha256/aa/'+('a'*64)
        assert item.metadata_['deliverySource']=='explicit'


@pytest.mark.parametrize('blocker',['projection','command','gate','snapshot'])
async def test_delivery_cannot_release_before_all_facts_settle(projection_database,blocker):
    sessions,admin=projection_database
    await seed(sessions,admin)
    if blocker=='projection':
        await admin.execute("UPDATE task_projection_states SET state=state-'terminal_boundary_id'")
    elif blocker=='command':
        await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
            kind,expected_epoch,payload,content_digest) VALUES ('stop','s','c','t','u','StopTask',1,'{}',repeat('d',64))""")
    elif blocker=='gate':
        await admin.execute('UPDATE conversations SET active_product_task_id=NULL')
    else:
        await admin.execute('UPDATE runtime_task_executions SET snapshot_id=NULL')
    with pytest.raises(DeliveryConflict):
        await close(sessions)
    assert await admin.fetchval('SELECT count(*) FROM artifacts')==0
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'


async def test_delivery_failure_rolls_back_artifacts_events_and_both_gates(projection_database):
    sessions,admin=projection_database
    await seed(sessions,admin)
    await admin.execute("ALTER TABLE task_events ADD CHECK(event_type<>'artifact')")
    with pytest.raises(Exception):
        await close(sessions)
    assert await admin.fetchval('SELECT count(*) FROM artifacts')==0
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE payload->>'name'='ARTIFACT_CREATED'")==0


async def test_unstarted_delivery_closes_without_files_snapshots_or_model_usage(projection_database):
    from test_unstarted_projection import seed as seed_unstarted,consume as project_unstarted
    sessions,admin=projection_database
    await seed_unstarted(admin)
    await project_unstarted(sessions)
    assert await close(sessions)==()
    assert await close(sessions)==()
    assert await admin.fetchval('SELECT count(*) FROM artifacts')==0
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots')==0
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') is None
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') is None
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='artifact_delivery'")==1


@pytest.mark.parametrize('previous_hash,previous_size,expected_version', [
    ('a'*64, 3, 1), ('b'*64, 3, 2), (None, 3, 2), ('a'*64, 4, 2),
])
async def test_delivery_compares_latest_content_and_keeps_task_links(
    projection_database, previous_hash, previous_size, expected_version,
):
    from workmate_product.persistence.models import Task, User
    from app.api.routers.artifacts import list_artifacts
    sessions, admin = projection_database
    await seed(sessions, admin)
    async with sessions.begin() as db:
        db.add(Task(id='previous', conversation_id='c', title='Previous', status='completed'))
        await db.flush()
        db.add(Artifact(id='original', task_id='previous', conversation_id='c', name='report.md',
            mime_type='text/markdown', storage_key='original-key', size=previous_size, version=1,
            change_type='created', metadata_={'sha256': previous_hash}))
        await db.flush()
        db.add(TaskArtifactDelivery(task_id='previous', artifact_id='original', change_type='created', metadata_={}))
    ids = await close(sessions)
    assert await close(sessions) == ids
    assert await admin.fetchval('SELECT max(version) FROM artifacts') == expected_version
    assert (ids == ('original',)) == (expected_version == 1)
    assert await admin.fetchval("SELECT count(*) FROM task_artifact_deliveries WHERE task_id='t'") == 1
    async with sessions() as db:
        user = await db.get(User, 'u')
        rows = await list_artifacts(conversation_id='c', task_id='t', user=user, db=db, limit=200, offset=0)
        assert len(rows) == 1 and rows[0].task_id == 't'
        assert rows[0].version == expected_version
        assert rows[0].change_type == ('presented' if expected_version == 1 else 'modified')
        history = await list_artifacts(conversation_id='c', user=user, db=db, limit=200, offset=0)
        assert {row.task_id for row in history} == {'previous', 't'}
        assert len(history) == 2
        original = await db.get(Artifact, 'original')
        assert original.task_id == 'previous' and original.version == 1
        assert original.storage_key == 'original-key'
    value = json.loads(await admin.fetchval("SELECT payload::text FROM product_events WHERE payload->>'name'='ARTIFACT_CREATED'"))['value']
    assert value['artifactId'] == ids[0] and value['taskId'] == 't'
    assert value['version'] == expected_version
    assert value['createdAt'] == rows[0].created_at.isoformat()


@pytest.mark.parametrize('name,cid,version,digest,expected', [
    ('report.md', 'c', 2, 'b'*64, 3),  # A -> B -> A is v3, not reuse of v1.
    ('nested/report.md', 'c', 5, 'a'*64, 1),
    ('report.md', 'other', 5, 'a'*64, 1),
])
async def test_versions_use_latest_revision_and_exact_conversation_path(
    projection_database, name, cid, version, digest, expected,
):
    from workmate_product.persistence.models import Task, Conversation
    sessions, admin = projection_database
    await seed(sessions, admin)
    async with sessions.begin() as db:
        if cid != 'c':
            db.add(Conversation(id=cid, user_id='u', agent_id='a'))
            await db.flush()
        db.add(Task(id='prior', conversation_id=cid, title='Prior'))
        await db.flush()
        for number in [1, version]:
            db.add(Artifact(id=f'old-{number}', task_id='prior', conversation_id=cid, name=name,
                mime_type='text/markdown', storage_key='old', size=3, version=number,
                change_type='created', metadata_={'sha256': 'a'*64 if number == 1 else digest}))
    ids = await close(sessions)
    async with sessions() as db:
        item = await db.get(Artifact, ids[0])
        assert item.version == expected and item.name == 'report.md' and item.conversation_id == 'c'


async def test_md_to_pdf_and_represented_md_keep_original_version(projection_database):
    from workmate_product.persistence.models import Task, User
    from app.api.routers.artifacts import list_artifacts
    sessions, admin = projection_database
    payload = await seed(sessions, admin, include_pdf=True)
    async with sessions.begin() as db:
        db.add(Task(id='md-task', conversation_id='c', title='MD', status='completed'))
        await db.flush()
        db.add(Artifact(id='md-v1', task_id='md-task', conversation_id='c', name='report.md',
            mime_type='text/markdown', storage_key=payload['files'][0]['storage_key'], size=3,
            version=1, change_type='created', metadata_={'sha256':'a'*64}))
        await db.flush()
        db.add(TaskArtifactDelivery(task_id='md-task', artifact_id='md-v1', change_type='created', metadata_={}))
    ids = await close(sessions)
    assert ids[0] == 'md-v1' and len(ids) == 2
    assert await admin.fetchval('SELECT count(*) FROM artifacts') == 2
    assert await admin.fetchval('SELECT count(*) FROM task_artifact_deliveries') == 3
    async with sessions() as db:
        user = await db.get(User, 'u')
        rows = await list_artifacts(conversation_id='c', task_id='t', user=user, db=db, limit=200, offset=0)
        assert {(a.name, a.version, a.change_type) for a in rows} == {
            ('report.md',1,'presented'), ('report.pdf',1,'created')}
        assert await list_artifacts(conversation_id='c', user=User(id='foreign'), db=db, limit=200, offset=0) == []
    await admin.execute("UPDATE conversations SET deleted_at=now() WHERE id='c'")
    async with sessions() as db:
        assert await list_artifacts(conversation_id='c', user=user, db=db, limit=200, offset=0) == []
