"""Native HTTP admission preserves Product facts using real PostgreSQL locks."""
import asyncio
import json

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from workmate_product.persistence.models import Conversation, Task, TaskMessage, User
from app.config_file import ModelsFileConfig
from app.services.execution_admission import TaskAdmission, selected_model
from test_durable_projection import projection_database


@pytest_asyncio.fixture
async def admission_database(projection_database, monkeypatch):
    sessions, admin = projection_database
    await admin.execute("UPDATE agents SET created_by='u', visibility='private'")
    async with sessions.begin() as db:
        db.add(Conversation(id='new', user_id='u', agent_id='a'))
    catalog = ModelsFileConfig.model_validate({'default':'test','profiles':{'test':{
        'type':'openai','model':'fixture-model','api_key_env':'FIXTURE_MODEL_KEY'}}})
    monkeypatch.setattr('app.services.execution_admission.models_file_config', lambda: catalog)
    monkeypatch.setattr('app.services.usage.model_profiles', lambda: catalog.profiles)
    return sessions, admin


async def create(sessions, service, **kwargs):
    async with sessions() as db:
        return await service.create_task(db, await db.get(User,'u'),
            await db.get(Conversation,'new'), 'hello', **kwargs)


async def test_create_atomically_freezes_command_spec_and_product_input(admission_database):
    sessions, admin = admission_database
    from workmate_product.coordination import WakeupBus
    bus = WakeupBus('unused')
    sessions.configure(info={'wakeup_bus': bus})
    service = TaskAdmission(sessions, storage_node_id='node')
    attachments = [{'name':'a.txt','path':'input/one/a.txt','size':3,'mime_type':'text/plain'}]
    task, queued = await create(sessions, service, task_id='one', attachments=attachments)
    assert not queued and task.status == 'created'
    assert 'commands' in bus._pending
    assert task.usage_model_id == 'fixture-model'
    assert await admin.fetchval("SELECT active_product_task_id FROM conversations WHERE id='new'") == 'one'
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE task_id='one'") == 1
    spec = json.loads(await admin.fetchval("SELECT payload FROM runtime_execution_specs WHERE task_id='one'"))
    assert spec['selected_model']['profile_name'] == 'test'
    assert spec['selected_model']['api_key_env'] == 'FIXTURE_MODEL_KEY'
    async with sessions() as db:
        message = await db.scalar(select(TaskMessage).where(TaskMessage.task_id=='one'))
        assert message.content == {'text':'hello','attachments':attachments}


async def test_competing_api_admissions_have_one_start_and_fifo_queue(admission_database):
    sessions, admin = admission_database
    service = TaskAdmission(sessions, storage_node_id='node')
    results = await asyncio.gather(*(create(sessions,service,task_id=f'task-{i}') for i in range(4)))
    assert sum(not queued for _, queued in results) == 1
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE conversation_id='new'") == 1
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE conversation_id='new'") == 4
    async with sessions.begin() as db:
        assert await service.admit_next(db,'new') is None


async def test_invalid_model_rolls_back_task_message_and_command(admission_database, monkeypatch):
    sessions, admin = admission_database
    monkeypatch.setattr('app.services.execution_admission.models_file_config',lambda: ModelsFileConfig())
    from app.services import ModelConfigError
    with pytest.raises(ModelConfigError):
        await create(sessions,TaskAdmission(sessions,storage_node_id='node'),task_id='bad')
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE id='bad'") == 0
    assert await admin.fetchval("SELECT count(*) FROM task_messages WHERE task_id='bad'") == 0


async def test_background_admission_rechecks_revoked_actor(admission_database):
    sessions, admin = admission_database
    service = TaskAdmission(sessions,storage_node_id='node')
    await create(sessions,service,task_id='first')
    await create(sessions,service,task_id='second')
    await admin.execute("UPDATE conversations SET active_product_task_id=NULL WHERE id='new'")
    await admin.execute("UPDATE users SET status='disabled' WHERE id='u'")
    with pytest.raises(Exception,match='access|active|权限'):
        async with sessions.begin() as db:
            await service.admit_next(db,'new')
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE task_id='second'") == 0


def test_media_descriptor_contains_no_resolved_secret(monkeypatch):
    catalog = ModelsFileConfig.model_validate({'default':'vision','media_profiles':{
        'vision':{'model_inputs':['text','image'],'pdf_strategy':'native'}},'profiles':{
        'vision':{'type':'openai','model':'vision-model','media_profile':'vision'}}})
    monkeypatch.setattr('app.services.execution_admission.models_file_config', lambda: catalog)
    descriptor = selected_model(None)
    assert descriptor.model_input_types == ('text/plain','image/*','application/pdf')
    assert 'base_url' not in descriptor.model_dump()


async def test_restarted_admission_preserves_node_and_skips_no_queue_order(admission_database):
    sessions, admin = admission_database
    first = TaskAdmission(sessions,storage_node_id='original-node')
    await create(sessions,first,task_id='first')
    await create(sessions,first,task_id='second')
    await create(sessions,first,task_id='third')
    await admin.execute("UPDATE tasks SET status='completed' WHERE id='first'")
    await admin.execute("UPDATE conversations SET active_product_task_id=NULL WHERE id='new'")
    restarted = TaskAdmission(sessions,storage_node_id='different-default')
    async def admit():
        async with sessions.begin() as db:
            return await restarted.admit_next(db,'new')
    receipts = await asyncio.gather(admit(),admit())
    assert sum(result is not None for result in receipts) == 1
    assert next(result for result in receipts if result).product_task_id == 'second'
    assert await admin.fetchval("SELECT storage_node_id FROM runtime_sessions WHERE conversation_id='new'") == 'original-node'
    assert await admin.fetchval("SELECT count(*) FROM runtime_execution_specs WHERE task_id='second'") == 1


async def test_full_access_admission_records_intent_without_claiming_enabled(admission_database):
    sessions, admin = admission_database
    service = TaskAdmission(sessions,storage_node_id='node')
    await create(sessions,service,task_id='full',approval_profile='full_access')
    async with sessions.begin() as db:
        assert await service.admit_next(db,'new') is None
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='task.full_access_enabled'") == 0
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE task_id='full' AND event_type='permission_mode_changed'") == 0
    payload = json.loads(await admin.fetchval("SELECT payload FROM runtime_execution_specs WHERE task_id='full'"))
    assert payload['approval_profile'] == 'full_access'


async def test_capacity_rejection_keeps_database_unchanged(admission_database,monkeypatch):
    sessions, admin = admission_database
    from types import SimpleNamespace
    from fastapi import HTTPException
    monkeypatch.setattr('app.services.task_admission.get_settings',lambda: SimpleNamespace(
        task_max_outstanding=1,task_max_outstanding_per_user=1,
        task_max_pending_approval=256,task_max_pending_approval_per_user=32,approval_timeout_secs=86400))
    with pytest.raises(HTTPException) as rejected:
        await create(sessions,TaskAdmission(sessions,storage_node_id='node'),task_id='over')
    assert rejected.value.status_code == 429
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE id='over'") == 0


async def test_failed_spec_freeze_rolls_back_all_admission_facts(admission_database,monkeypatch):
    sessions, admin = admission_database
    async def unavailable(*args,**kwargs):
        raise ValueError('Immutable skill artifact unavailable')
    monkeypatch.setattr('app.services.execution_admission.record_execution_spec',unavailable)
    with pytest.raises(ValueError,match='artifact unavailable'):
        await create(sessions,TaskAdmission(sessions,storage_node_id='node'),task_id='bad-spec')
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE id='bad-spec'") == 0
    assert await admin.fetchval("SELECT count(*) FROM runtime_sessions WHERE conversation_id='new'") == 0
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE conversation_id='new'") == 0


async def test_empty_queue_and_nonterminal_runtime_gate_do_not_admit(admission_database):
    sessions, admin = admission_database
    service = TaskAdmission(sessions,storage_node_id='node')
    async with sessions.begin() as db:
        assert await service.admit_next(db,'new') is None
    await create(sessions,service,task_id='first')
    await create(sessions,service,task_id='second')
    await admin.execute("UPDATE conversations SET active_product_task_id=NULL WHERE id='new'")
    await admin.execute("UPDATE runtime_sessions SET status='running',active_task_id='first' WHERE conversation_id='new'")
    async with sessions.begin() as db:
        assert await service.admit_next(db,'new') is None
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE task_id='second'") == 0


@pytest.mark.parametrize('node',[None,'',' ', 'x'*129])
def test_explicit_storage_identity_required(node):
    with pytest.raises(ValueError):
        TaskAdmission(None,storage_node_id=node)


async def test_cached_membership_cannot_bypass_committed_revocation(admission_database):
    sessions, admin = admission_database
    from workmate_product.persistence.models import ScopeMembership
    await admin.execute("UPDATE agents SET visibility='public',created_by=NULL")
    async with sessions.begin() as db:
        db.add(ScopeMembership(scope_id='scope',user_id='u'))
    service = TaskAdmission(sessions,storage_node_id='node')
    from fastapi import HTTPException
    async with sessions() as db:
        cached = await db.get(ScopeMembership,('scope','u'))
        user = await db.get(User,'u')
        conversation = await db.get(Conversation,'new')
        assert cached is not None
        await admin.execute("DELETE FROM scope_memberships WHERE user_id='u'")
        with pytest.raises(HTTPException) as denied:
            await service.create_task(db,user,conversation,'denied',task_id='stale')
        assert denied.value.status_code == 403
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE id='stale'") == 0
