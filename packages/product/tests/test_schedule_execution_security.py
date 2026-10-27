"""Schedule source and unattended authority are immutable execution inputs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from workmate_product.application.execution_context import (
    ExecutionSpec, ExecutionContextRejected, SelectedModel, ToolPolicy,
)
from workmate_product.application.full_access import stage_full_access, effective_permission_profile
from workmate_product.application.commands import CommandRejected


def make_spec(**overrides):
    return ExecutionSpec(**dict(task_id='t', session_id='s', conversation_id='c',
        actor_user_id='u', agent_id='a', approval_profile='standard',
        tool_policy=ToolPolicy(), platform_tool_profile='none', capability_ids=(),
        knowledge_base_ids=(), selected_model=SelectedModel(profile_name='chat',
            model_type='openai', model='test', api_key_env='KEY'), **overrides))


def test_user_spec_retains_default_origin():
    assert make_spec().origin == 'user'
    assert make_spec().schedule_occurrence_id is None


@pytest.mark.parametrize('origin,profile,occurrence', [
    ('user','unattended',None), ('user','standard','occ'),
    ('schedule','standard','occ'), ('schedule','unattended',None),
])
def test_execution_spec_rejects_inconsistent_schedule_source(origin, profile, occurrence):
    values = make_spec().model_dump()
    values.update(origin=origin, approval_profile=profile, schedule_occurrence_id=occurrence)
    with pytest.raises(ValidationError):
        ExecutionSpec.model_validate(values)


@pytest.mark.asyncio
async def test_full_access_rejects_unattended_before_accessing_upgrade_proof():
    with pytest.raises(CommandRejected, match='[Uu]nattended'):
        await stage_full_access(None, None, {'approval_profile':'unattended'}, None, record=True)


@pytest.mark.asyncio
async def test_existing_upgrade_cannot_elevate_unattended(monkeypatch):
    from workmate_product.application import full_access
    monkeypatch.setattr(full_access.persistence, 'latest_upgrade', AsyncMock(return_value={'command_id':'bad'}))
    with pytest.raises(CommandRejected, match='[Uu]nattended'):
        await effective_permission_profile(None, SimpleNamespace(id='t'), None,
            SimpleNamespace(approval_profile='unattended'))


def schedule_facts():
    task = SimpleNamespace(id='t', origin='schedule', schedule_occurrence_id='occ',
        approval_profile='unattended', conversation_id='c', channel_inbox_id=None)
    conversation = SimpleNamespace(id='c', user_id='u', agent_id='a', model_profile='chat')
    occurrence = SimpleNamespace(id='occ', task_id='t', conversation_id='c', schedule_version=2, outcome='task_created',
        snapshot={'owner_user_id':'u', 'agent_id':'a', 'version':2, 'prompt':'hello',
                  'model_profile':'chat', 'approval_profile':'unattended'})
    message = SimpleNamespace(content={'text':'hello'})
    db = SimpleNamespace(get=AsyncMock(return_value=occurrence),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [message])))
    return db, task, conversation, occurrence, message


@pytest.mark.asyncio
async def test_schedule_source_uses_occurrence_snapshot_without_live_schedule():
    from workmate_product.application.execution_context import _schedule_source
    db, task, conversation, occurrence, message = schedule_facts()
    assert await _schedule_source(db, task, conversation) == {
        'origin':'schedule', 'schedule_occurrence_id':'occ', 'channel_inbox_id':None}
    assert db.get.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('owner_user_id','other'),('agent_id','other'),
    ('version',3),('model_profile','other'),('approval_profile','full_access'),('prompt','forged')])
async def test_schedule_source_rejects_forged_snapshot(field,value):
    from workmate_product.application.execution_context import _schedule_source
    db, task, conversation, occurrence, message = schedule_facts()
    occurrence.snapshot[field] = value
    with pytest.raises(ExecutionContextRejected, match='Schedule'):
        await _schedule_source(db, task, conversation)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['missing', 'other_task', 'other_conversation', 'missing_message', 'wrong_origin', 'blocked'])
async def test_schedule_source_requires_exact_occurrence_binding(change):
    from workmate_product.application.execution_context import _schedule_source
    db, task, conversation, occurrence, message = schedule_facts()
    if change == 'missing':
        db.get.return_value = None
    elif change == 'other_task':
        occurrence.task_id = 'other'
    elif change == 'other_conversation':
        occurrence.conversation_id = 'other'
    elif change == 'missing_message':
        db.scalars.return_value = SimpleNamespace(all=lambda: [])
    elif change == 'blocked':
        occurrence.outcome = 'blocked'
    else:
        task.origin = 'user'
    with pytest.raises(ExecutionContextRejected, match='Schedule'):
        await _schedule_source(db, task, conversation)


def test_schedule_spec_records_unattended_occurrence():
    values = make_spec().model_dump()
    values.update(origin='schedule', approval_profile='unattended', schedule_occurrence_id='occ')
    assert ExecutionSpec.model_validate(values).schedule_occurrence_id == 'occ'


@pytest.mark.asyncio
async def test_unattended_without_upgrade_keeps_profile(monkeypatch):
    from workmate_product.application import full_access
    monkeypatch.setattr(full_access.persistence, 'latest_upgrade', AsyncMock(return_value=None))
    assert await effective_permission_profile(None, SimpleNamespace(id='t'), None,
        SimpleNamespace(approval_profile='unattended')) == ('unattended', None)


@pytest.mark.asyncio
async def test_regular_task_source_has_no_occurrence_lookup():
    from workmate_product.application.execution_context import _schedule_source
    db, task, conversation, occurrence, message = schedule_facts()
    task.origin, task.approval_profile, task.schedule_occurrence_id = 'user', 'standard', None
    assert await _schedule_source(db, task, conversation) == {'origin':'user','schedule_occurrence_id':None,'channel_inbox_id':None}
    db.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', ['user', 'schedule'])
async def test_non_channel_task_rejects_channel_identity_before_occurrence_lookup(origin):
    from workmate_product.application.execution_context import _schedule_source
    db, task, conversation, _, _ = schedule_facts()
    task.origin = origin
    task.channel_inbox_id = 'forged-channel'
    with pytest.raises(ExecutionContextRejected, match='Non-channel task'):
        await _schedule_source(db, task, conversation)
    db.get.assert_not_awaited()
