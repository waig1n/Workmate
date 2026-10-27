"""Native retirement hides Product history without deleting execution evidence."""
import asyncio
import json
import pytest
from workmate_product.persistence.models import Conversation, User, TaskMessage, Artifact
from test_native_agents import native_agents,projection_database  # noqa: F401


async def idle(admin):
    await admin.execute("""UPDATE tasks SET status='completed';
        UPDATE conversations SET active_product_task_id=NULL;
        UPDATE runtime_sessions SET active_task_id=NULL,status='completed';
        UPDATE runtime_task_executions SET status='completed';""")


async def test_empty_conversation_retirement_is_idempotent_and_owner_only(native_agents):
    client,sessions,admin=native_agents
    async with sessions.begin() as db:
        db.add(User(id='other',email='other@example.invalid',password_hash='fixture',display_name='Other'))
        await db.flush()
        db.add(Conversation(id='empty',user_id='u',agent_id='a'))
        db.add(Conversation(id='foreign',user_id='other',agent_id='a'))
    assert (await client.delete('/api/conversations/empty')).status_code==204
    assert (await client.delete('/api/conversations/empty')).status_code==204
    assert (await client.delete('/api/conversations/foreign')).status_code==404
    assert (await client.delete('/api/conversations/missing')).status_code==404
    assert await admin.fetchval("SELECT deleted_at FROM conversations WHERE id='empty'") is not None
    assert await admin.fetchval("SELECT completed_at FROM conversation_retirements WHERE conversation_id='empty'") is not None
    assert await admin.fetchval('SELECT count(*) FROM conversation_retirements')==1
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='conversation.delete'")==1


async def test_retired_history_is_preserved_but_hidden_from_all_product_read_surfaces(native_agents):
    from app.auth import get_current_user_id
    from workmate_contracts import snapshot_digest
    from workmate_product.application.execution_context import ExecutionSpec,SelectedModel,ToolPolicy
    client,sessions,admin=native_agents
    await idle(admin)
    async with sessions.begin() as db:
        db.add(TaskMessage(id='message',task_id='t',conversation_id='c',role='user',content={'text':'retain history'}))
        db.add(Artifact(id='artifact',task_id='t',conversation_id='c',name='history.txt',mime_type='text/plain',
            storage_key='immutable-history',size=3,change_type='created'))
    spec=ExecutionSpec(task_id='t',session_id='s',conversation_id='c',actor_user_id='u',agent_id='a',
        approval_profile='standard',tool_policy=ToolPolicy(),platform_tool_profile='none',capability_ids=(),knowledge_base_ids=(),
        selected_model=SelectedModel(profile_name='chat',model_type='openai',model='fixture',api_key_env='CHAT_MODEL_API_KEY'))
    payload=spec.model_dump(mode='json')
    await admin.execute("INSERT INTO runtime_execution_specs(task_id,session_id,schema_version,payload,digest) VALUES ('t','s',1,$1::jsonb,$2)",json.dumps(payload),snapshot_digest(payload))
    original=await admin.fetchval('SELECT payload FROM runtime_execution_specs')
    client._transport.app.dependency_overrides[get_current_user_id]=lambda:'u'
    class ForbiddenStream:
        def __getattr__(self,name): raise AssertionError('Retired Conversation must not open a stream')
    client._transport.app.state.product_stream=ForbiddenStream()
    response=await client.delete('/api/conversations/c')
    assert response.status_code==204,response.text
    for path in ('/api/conversations/c','/api/conversations/c/messages','/api/tasks/t',
                 '/api/tasks/t/events','/api/tasks/t/pending-confirm','/api/artifacts/artifact/download',
                 '/api/tasks/conversations/c/events'):
        hidden=await client.get(path)
        assert hidden.status_code==404,(path,hidden.text)
    for path in ('/api/conversations','/api/tasks','/api/tasks?conversation_id=c',
                 '/api/artifacts','/api/artifacts?task_id=t'):
        listed=await client.get(path)
        assert listed.status_code==200,(path,listed.text)
        assert listed.json()==[]
    history=await client.get('/api/conversations/history')
    assert history.status_code==200,history.text
    assert history.json()['items']==[]
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'")=='completed'
    assert await admin.fetchval('SELECT payload FROM runtime_execution_specs')==original
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==1
    assert await admin.fetchval('SELECT count(*) FROM artifacts')==1
    retirement=await admin.fetchrow("SELECT * FROM conversation_retirements WHERE conversation_id='c'")
    assert retirement['session_id']=='s' and retirement['storage_node_id']=='node'
    assert retirement['completed_at'] is None


@pytest.mark.parametrize('blocker',['task','runtime_gate','product_gate','accepted_command'])
async def test_retirement_requires_closed_execution_and_delivery_gates(native_agents,blocker):
    client,sessions,admin=native_agents
    await idle(admin)
    if blocker=='task': await admin.execute("UPDATE tasks SET status='awaiting_permission'")
    elif blocker=='runtime_gate': await admin.execute("UPDATE runtime_sessions SET active_task_id='t'")
    elif blocker=='product_gate': await admin.execute("UPDATE conversations SET active_product_task_id='t'")
    else:
        await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
            VALUES ('pending','s','c','t','u','StopTask','{}',repeat('a',64))""")
    response=await client.delete('/api/conversations/c')
    assert response.status_code==409,response.text
    assert await admin.fetchval('SELECT deleted_at FROM conversations') is None
    assert await admin.fetchval('SELECT count(*) FROM conversation_retirements')==0


async def test_task_admission_and_retirement_serialize_without_hidden_new_execution(native_agents,monkeypatch):
    from app.config_file import ModelsFileConfig
    from app.services.execution_admission import TaskAdmission
    from app.services.product_runtime import ProductRuntime
    client,sessions,admin=native_agents
    async with sessions.begin() as db:
        db.add(Conversation(id='race',user_id='u',agent_id='a',model_profile='chat'))
    catalog=ModelsFileConfig.model_validate({'default':'chat','profiles':{'chat':{
        'type':'openai','model':'fixture','api_key_env':'CHAT_MODEL_API_KEY'}}})
    monkeypatch.setattr('app.services.execution_admission.models_file_config',lambda:catalog)
    monkeypatch.setattr('app.services.usage.model_profiles',lambda:catalog.profiles)
    client._transport.app.state.product_runtime=ProductRuntime(sessions,TaskAdmission(sessions,storage_node_id='node'))
    admitted,retired=await asyncio.wait_for(asyncio.gather(
        client.post('/api/tasks/conversations/race/tasks',json={'message':'race'}),
        client.delete('/api/conversations/race')),10)
    assert (admitted.status_code,retired.status_code) in {(202,409),(403,204),(404,204)},(admitted.text,retired.text)
    count=await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE conversation_id='race'")
    tombstone=await admin.fetchval("SELECT deleted_at FROM conversations WHERE id='race'")
    if retired.status_code==204:
        assert count==0 and tombstone is not None
    else:
        assert count==1 and tombstone is None
        assert admitted.json()['queued'] is False


@pytest.mark.parametrize('pending_command', [False, True])
async def test_failed_quarantined_conversation_can_be_hidden_without_releasing_execution(native_agents, pending_command):
    from test_execution_quarantine import seed, quarantine
    client, sessions, admin = native_agents
    await seed(admin)
    await quarantine(sessions)
    if pending_command:
        await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
            VALUES ('pending','s','c','t','u','StopTask','{}',repeat('a',64))""")
    before = dict(await admin.fetchrow("SELECT * FROM runtime_sessions WHERE id='s'"))
    response = await client.delete('/api/conversations/c')
    assert response.status_code == 204, response.text
    assert (await client.delete('/api/conversations/c')).status_code == 204
    assert (await client.get('/api/conversations/c')).status_code == 404
    assert (await client.get('/api/conversations/history')).json()['items'] == []
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == 'failed'
    assert await admin.fetchval("SELECT active_product_task_id FROM conversations WHERE id='c'") == 't'
    assert dict(await admin.fetchrow("SELECT * FROM runtime_sessions WHERE id='s'")) == before
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == int(pending_command)
    assert await admin.fetchval('SELECT completed_at FROM conversation_retirements') is None
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='conversation.delete'") == 1


async def test_failed_history_does_not_allow_deleting_another_active_task(native_agents):
    from workmate_product.persistence.models import Task
    client, sessions, admin = native_agents
    await admin.execute("UPDATE tasks SET status='failed'")
    async with sessions.begin() as db:
        db.add(Task(id='queued', conversation_id='c', title='Next task'))
    assert (await client.delete('/api/conversations/c')).status_code == 409
    assert await admin.fetchval('SELECT deleted_at FROM conversations') is None


async def test_failed_task_with_closed_gates_can_be_deleted(native_agents):
    client, sessions, admin = native_agents
    await idle(admin)
    await admin.execute("UPDATE tasks SET status='failed'")
    assert (await client.delete('/api/conversations/c')).status_code == 204


@pytest.mark.parametrize('blocker', ['product_gate', 'runtime_gate', 'accepted_command'])
async def test_failed_task_does_not_bypass_a_nonfailed_tasks_residual_ownership(native_agents, blocker):
    from workmate_product.persistence.models import Task
    client, sessions, admin = native_agents
    await admin.execute("UPDATE tasks SET status='failed'")
    async with sessions.begin() as db:
        db.add(Task(id='other', conversation_id='c', title='Other', status='completed'))
    if blocker == 'product_gate':
        await admin.execute("UPDATE conversations SET active_product_task_id='other'")
    elif blocker == 'runtime_gate':
        await admin.execute("UPDATE runtime_sessions SET active_task_id='other'")
    else:
        await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
            VALUES ('pending','s','c','other','u','StopTask','{}',repeat('a',64))""")
    assert (await client.delete('/api/conversations/c')).status_code == 409
    assert await admin.fetchval('SELECT count(*) FROM conversation_retirements') == 0


async def test_recovered_conversation_hides_without_retiring_old_execution(native_agents):
    client, sessions, admin = native_agents
    await admin.execute("""UPDATE tasks SET status='failed';
        UPDATE conversations SET active_product_task_id=NULL;
        UPDATE runtime_sessions SET superseded_at=clock_timestamp();
        INSERT INTO runtime_sessions(id,conversation_id,storage_node_id) VALUES ('new','c','other-node');""")
    response = await client.delete('/api/conversations/c')
    assert response.status_code == 204, response.text
    row = await admin.fetchrow("SELECT session_id,completed_at FROM conversation_retirements WHERE conversation_id='c'")
    assert row['session_id'] == 'new'
    assert row['completed_at'] is None
    assert await admin.fetchval("SELECT active_task_id FROM runtime_sessions WHERE id='s'") == 't'
