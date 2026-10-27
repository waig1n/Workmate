import asyncio
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from workmate_product.persistence.models import Conversation, User
from app.services.execution_admission import TaskAdmission
from test_native_admission import admission_database, create
from test_durable_projection import projection_database


class Directory:
    def __init__(self, ready=('node','two'), enabled=('node','two')):
        self.ready, self.enabled = set(ready), set(enabled)
    def available(self, node):
        return node in self.ready
    def candidates(self):
        return tuple(sorted(self.ready & self.enabled))


async def test_existing_namespace_pins_new_conversations_and_accepts_draining(admission_database):
    sessions, admin = admission_database
    directory = Directory(enabled=('two',))
    service = TaskAdmission(sessions,storage_node_id='unused',node_directory=directory)
    await create(sessions,service,task_id='first')
    assert await admin.fetchval("SELECT storage_node_id FROM runtime_sessions WHERE conversation_id='new'") == 'node'
    assert await admin.fetchval("SELECT storage_node_id FROM runtime_memory_namespaces WHERE user_id='u' AND agent_id='a'") == 'node'


async def test_offline_owner_rejects_before_writes_and_never_moves(admission_database):
    sessions, admin = admission_database
    service = TaskAdmission(sessions,storage_node_id='unused',node_directory=Directory(ready=('two',)))
    with pytest.raises(HTTPException) as error:
        await create(sessions,service,task_id='rejected')
    assert error.value.status_code == 503
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE id='rejected'") == 0
    assert await admin.fetchval('SELECT count(*) FROM runtime_memory_namespaces') == 0


async def test_queued_task_waits_for_original_node_recovery(admission_database):
    sessions, admin = admission_database
    directory = Directory()
    service = TaskAdmission(sessions,storage_node_id='unused',node_directory=directory)
    await create(sessions,service,task_id='first')
    await create(sessions,service,task_id='second')
    await admin.execute("UPDATE conversations SET active_product_task_id=NULL WHERE id='new'")
    directory.ready = {'two'}
    async with sessions.begin() as db:
        assert await service.admit_next(db,'new') is None
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE task_id='second'") == 0
    directory.ready.add('node')
    async with sessions.begin() as db:
        assert (await service.admit_next(db,'new')).product_task_id == 'second'


async def test_historical_node_conflict_requires_review(admission_database):
    sessions, admin = admission_database
    await admin.execute("INSERT INTO runtime_memory_namespaces(user_id,agent_id,storage_node_id) VALUES ('u','a','two')")
    with pytest.raises(HTTPException) as error:
        await create(sessions,TaskAdmission(sessions,storage_node_id='unused',node_directory=Directory()),task_id='rejected')
    assert error.value.status_code == 503
    assert 'conflicting' in error.value.detail


async def test_new_namespace_concurrent_conversations_choose_one_enabled_node(admission_database):
    sessions, admin = admission_database
    # Remove fixture execution only; this pair has no historical placement.
    await admin.execute("DELETE FROM runtime_task_executions")
    await admin.execute("DELETE FROM runtime_sessions")
    async with sessions.begin() as db:
        db.add(Conversation(id='other',user_id='u',agent_id='a'))
    service = TaskAdmission(sessions,storage_node_id='unused',node_directory=Directory(enabled=('two',)))
    async def submit(cid, tid):
        async with sessions() as db:
            return await service.create_task(db,await db.get(User,'u'),await db.get(Conversation,cid),'hello',task_id=tid)
    await asyncio.gather(submit('new','first'),submit('other','second'))
    assert await admin.fetchval("SELECT count(*) FROM runtime_sessions WHERE storage_node_id='two'") == 2
    assert await admin.fetchval('SELECT count(*) FROM runtime_memory_namespaces') == 1
