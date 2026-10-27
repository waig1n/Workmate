from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from workmate_product.application.execution_context import _schedule_source, ExecutionContextRejected
from workmate_product.authorization.permissions import CHANNEL_MANAGE, ROOT_ONLY_PERMISSIONS, ALL_PERMISSIONS

def test_channel_permission_root_only():
    assert CHANNEL_MANAGE in ROOT_ONLY_PERMISSIONS and CHANNEL_MANAGE in ALL_PERMISSIONS

@pytest.mark.parametrize('field,value',[('task_id','other'),('conversation_id','other'),('user_id','other')])
async def test_channel_source_rejects_identity_mismatch(field,value):
    inbox=SimpleNamespace(task_id='t',conversation_id='c',user_id='u')
    setattr(inbox,field,value)
    db=SimpleNamespace(get=AsyncMock(return_value=inbox))
    task=SimpleNamespace(id='t',origin='channel',channel_inbox_id='i',schedule_occurrence_id=None,approval_profile='standard',conversation_id='c')
    with pytest.raises(ExecutionContextRejected):
        await _schedule_source(db,task,SimpleNamespace(id='c',user_id='u'))

async def test_channel_source_does_not_require_current_connection_or_binding():
    db=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(task_id='t',conversation_id='c',user_id='u')))
    task=SimpleNamespace(id='t',origin='channel',channel_inbox_id='i',schedule_occurrence_id=None,approval_profile='standard',conversation_id='c')
    assert await _schedule_source(db,task,SimpleNamespace(id='c',user_id='u')) == dict(origin='channel',channel_inbox_id='i',schedule_occurrence_id=None)
    assert db.get.await_count == 1

@pytest.mark.parametrize('origin,inbox,profile,valid', [
    ('channel','i','standard',True), ('channel',None,'standard',False),
    ('user','i','standard',False), ('channel','i','full_access',False),
    ('channel','i','unattended',False),
])
def test_execution_spec_requires_trusted_channel_source(origin,inbox,profile,valid):
    from workmate_product.application.execution_context import ExecutionSpec, SelectedModel, ToolPolicy
    from pydantic import ValidationError
    payload=dict(origin=origin,channel_inbox_id=inbox,task_id='t',session_id='s',conversation_id='c',
        actor_user_id='u',agent_id='a',approval_profile=profile,tool_policy=ToolPolicy(),
        platform_tool_profile='standard',capability_ids=(),knowledge_base_ids=(),
        selected_model=SelectedModel(profile_name='chat',model_type='openai',model='model',api_key_env='CHAT_MODEL_API_KEY'))
    if valid:
        assert ExecutionSpec(**payload).channel_inbox_id == 'i'
    else:
        with pytest.raises(ValidationError): ExecutionSpec(**payload)
