"""Model selection changes future admission only and never mutates App sessions."""
import importlib
import importlib.util
import json
import pytest
import pytest_asyncio
from app.config_file import ModelProfileFileConfig
from test_native_agents import native_agents,projection_database  # noqa: F401


@pytest_asyncio.fixture
async def native_models(native_agents,monkeypatch):
    client,sessions,admin=native_agents
    from workmate_product.persistence.models import ResourceShare
    await admin.execute("UPDATE agents SET visibility='public' WHERE id='a'")
    async with sessions.begin() as db:
        db.add(ResourceShare(id='model-agent-grant', agent_id='a', target_scope_id='scope'))
    profiles={name:ModelProfileFileConfig(type='openai',model='model-'+name) for name in ('old','new')}
    for name in ('app.config_file','app.api.routers.conversations','app.api.routers.conversations'):
        if importlib.util.find_spec(name) is not None:
            module=importlib.import_module(name)
            monkeypatch.setattr(module,'model_profiles',lambda:profiles,raising=False)
            monkeypatch.setattr(module,'default_model_profile_name',lambda:'new',raising=False)
    await admin.execute("UPDATE conversations SET model_profile='old',agentscope_session_id='legacy-session'")
    yield client,sessions,admin


async def idle(admin):
    await admin.execute("""UPDATE tasks SET status='completed';
        UPDATE conversations SET active_product_task_id=NULL;
        UPDATE runtime_sessions SET active_task_id=NULL,status='completed';
        UPDATE runtime_task_executions SET status='completed';""")


@pytest.mark.parametrize('profile',['new',None])
async def test_native_model_switch_persists_selection_without_touching_historical_spec(native_models,profile):
    from workmate_product.application.execution_context import ExecutionSpec,SelectedModel,ToolPolicy
    from workmate_contracts import snapshot_digest
    client,sessions,admin=native_models
    await idle(admin)
    spec=ExecutionSpec(task_id='t',session_id='s',conversation_id='c',actor_user_id='u',agent_id='a',
        approval_profile='standard',tool_policy=ToolPolicy(),platform_tool_profile='none',capability_ids=(),
        knowledge_base_ids=(),selected_model=SelectedModel(profile_name='old',model_type='openai',model='model-old',api_key_env='CHAT_MODEL_API_KEY'))
    payload=spec.model_dump(mode='json')
    await admin.execute("INSERT INTO runtime_execution_specs(task_id,session_id,schema_version,payload,digest) VALUES ('t','s',1,$1::jsonb,$2)",json.dumps(payload),snapshot_digest(payload))
    original=await admin.fetchval('SELECT payload FROM runtime_execution_specs')
    response=await client.patch('/api/conversations/c/model',json={'model_profile':profile})
    assert response.status_code==200,response.text
    assert response.json()['model_profile']=='new'
    assert await admin.fetchval('SELECT model_profile FROM conversations')=='new'
    assert await admin.fetchval('SELECT payload FROM runtime_execution_specs')==original
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0
    assert await admin.fetchval('SELECT reply_id FROM runtime_task_executions')=='reply'


@pytest.mark.parametrize('blocker',['task','runtime_gate','product_gate'])
async def test_native_model_switch_requires_no_active_task_and_both_gates_empty(native_models,blocker):
    client,sessions,admin=native_models
    await idle(admin)
    if blocker=='task': await admin.execute("UPDATE tasks SET status='awaiting_permission'")
    elif blocker=='runtime_gate': await admin.execute("UPDATE runtime_sessions SET active_task_id='t'")
    else: await admin.execute("UPDATE conversations SET active_product_task_id='t'")
    response=await client.patch('/api/conversations/c/model',json={'model_profile':'new'})
    assert response.status_code==409,response.text
    assert await admin.fetchval('SELECT model_profile FROM conversations')=='old'


@pytest.mark.parametrize('profile',['unknown',''])
async def test_native_model_switch_rejects_unknown_profile_without_mutation(native_models,profile):
    client,sessions,admin=native_models
    await idle(admin)
    response=await client.patch('/api/conversations/c/model',json={'model_profile':profile})
    assert response.status_code==422,response.text
    assert await admin.fetchval('SELECT model_profile FROM conversations')=='old'


async def test_native_model_switch_rechecks_agent_use_permission(native_models):
    client,sessions,admin=native_models
    await idle(admin)
    await admin.execute("UPDATE agents SET deleted_at=clock_timestamp() WHERE id='a'")
    response=await client.patch('/api/conversations/c/model',json={'model_profile':'new'})
    assert response.status_code==403,response.text
    assert await admin.fetchval('SELECT model_profile FROM conversations')=='old'
    history=await client.get('/api/conversations/c')
    assert history.status_code==200,history.text
    assert history.json()['read_only'] is True


async def test_concurrent_api_model_switches_serialize_without_runtime_mutation(native_models):
    import asyncio
    client,sessions,admin=native_models
    await idle(admin)
    first,second=await asyncio.wait_for(asyncio.gather(
        client.patch('/api/conversations/c/model',json={'model_profile':'new'}),
        client.patch('/api/conversations/c/model',json={'model_profile':'old'})),10)
    assert first.status_code==second.status_code==200,(first.text,second.text)
    assert first.json()['model_profile']=='new'
    assert second.json()['model_profile']=='old'
    assert await admin.fetchval('SELECT model_profile FROM conversations') in {'old','new'}
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0


async def test_model_switch_and_http_task_admission_pin_one_serialized_profile(native_models,monkeypatch):
    import asyncio
    from app.config_file import ModelsFileConfig
    from app.services.execution_admission import TaskAdmission
    from app.services.product_runtime import ProductRuntime
    client,sessions,admin=native_models
    await idle(admin)
    catalog=ModelsFileConfig.model_validate({'default':'new','profiles':{
        name:{'type':'openai','model':'model-'+name,'api_key_env':'CHAT_MODEL_API_KEY'} for name in ('old','new')}})
    monkeypatch.setattr('app.services.execution_admission.models_file_config',lambda:catalog)
    monkeypatch.setattr('app.services.usage.model_profiles',lambda:catalog.profiles)
    client._transport.app.state.product_runtime=ProductRuntime(sessions,TaskAdmission(sessions,storage_node_id='node'))
    changed,admitted=await asyncio.wait_for(asyncio.gather(
        client.patch('/api/conversations/c/model',json={'model_profile':'new'}),
        client.post('/api/tasks/conversations/c/tasks',json={'message':'Run with the selected model'})),10)
    assert changed.status_code in (200,409),changed.text
    assert admitted.status_code==202,admitted.text
    assert admitted.json()['queued'] is False
    task_id=admitted.json()['task']['id']
    spec=json.loads(await admin.fetchval('SELECT payload FROM runtime_execution_specs WHERE task_id=$1',task_id))
    expected='new' if changed.status_code==200 else 'old'
    assert spec['selected_model']['profile_name']==expected
    assert await admin.fetchval('SELECT model_profile FROM conversations')==expected
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')==task_id
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands WHERE task_id=$1',task_id)==1


@pytest.mark.parametrize('target',['foreign','missing'])
async def test_native_model_switch_hides_unowned_or_missing_conversation(native_models,target):
    from workmate_product.persistence.models import User
    client,sessions,admin=native_models
    await idle(admin)
    if target=='foreign':
        async with sessions.begin() as db:
            db.add(User(id='other',email='other@example.invalid',password_hash='fixture',display_name='Other'))
        await admin.execute("UPDATE conversations SET user_id='other' WHERE id='c'")
    response=await client.patch('/api/conversations/'+('c' if target=='foreign' else 'missing')+'/model',
        json={'model_profile':'new'})
    assert response.status_code==404,response.text
    assert await admin.fetchval('SELECT model_profile FROM conversations')=='old'


async def test_native_model_switch_rejects_disabled_bound_capability(native_models):
    from workmate_product.persistence.models import CapabilityInstallation, AgentCapabilityBinding
    client,sessions,admin=native_models
    await idle(admin)
    async with sessions.begin() as db:
        db.add(CapabilityInstallation(id='disabled-skill',capability_type='skill',scope_id='scope',
            name='disabled',enabled=False,metadata_={}))
        await db.flush()
        db.add(AgentCapabilityBinding(agent_id='a',capability_installation_id='disabled-skill'))
    response=await client.patch('/api/conversations/c/model',json={'model_profile':'new'})
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']=='unavailable_capabilities'
    assert await admin.fetchval('SELECT model_profile FROM conversations')=='old'
