"""Product HTTP-facing controls preserve durable original Reply and ToolCall facts."""
import json

import pytest
from workmate_product.persistence.models import User, Conversation, Task
from app.services.product_runtime import ProductRuntime
from app.services.runtime_errors import ApprovalConflict
from test_durable_projection import projection_database  # noqa: F401
from test_control_commands import parked


async def allow(*args,**kwargs): return True


def runtime(sessions,monkeypatch):
    value=ProductRuntime(sessions,None)
    monkeypatch.setattr(value,'_authorize',allow)
    return value


async def loaded(db,task_id='t'):
    return await db.get(User,'u'),await db.get(Conversation,'c'),await db.get(Task,task_id)


async def confirm(service,sessions,version,decisions=None):
    async with sessions() as db:
        user,conversation,task=await loaded(db)
        await service.confirm(db,user,conversation,task,decisions or [{'tool_call_id':'a','approved':True}],
            decision_version=version)
        return task.status


async def test_native_confirmation_is_durable_and_duplicate_is_same_command(projection_database,monkeypatch):
    sessions,admin=projection_database
    from workmate_product.coordination import WakeupBus
    bus = WakeupBus('unused')
    sessions.configure(info={'wakeup_bus': bus})
    original=await parked(sessions,admin)
    service=runtime(sessions,monkeypatch)
    assert await confirm(service,sessions,original.request_version)=='awaiting_permission'
    first=await admin.fetchrow('SELECT * FROM runtime_commands')
    await confirm(service,sessions,original.request_version)
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==1
    assert await admin.fetchval('SELECT id FROM runtime_commands')==first['id']
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_decision'")==1
    assert 'commands' in bus._pending
    assert 'conversation:c' in bus._pending
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
    assert await admin.fetchval('SELECT reply_id FROM runtime_sessions')=='reply'


@pytest.mark.parametrize('version',['stale-version',None])
async def test_native_stale_or_missing_decision_version_is_rejected(projection_database,monkeypatch,version):
    sessions,admin=projection_database
    await parked(sessions,admin)
    service=runtime(sessions,monkeypatch)
    with pytest.raises(ApprovalConflict): await confirm(service,sessions,version)
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0


async def test_native_confirmation_never_accepts_http_tool_parameter_replacement(projection_database,monkeypatch):
    sessions,admin=projection_database
    original=await parked(sessions,admin)
    service=runtime(sessions,monkeypatch)
    await confirm(service,sessions,original.request_version,[{'tool_call_id':'a','approved':True,
        'tool_call_name':'Bash','tool_call_input':{'command':'injected operation'}}])
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==1
    command=await admin.fetchrow('SELECT * FROM runtime_commands')
    assert 'injected operation' not in json.dumps(dict(command),default=str)
    request=json.loads(await admin.fetchval("SELECT payload FROM task_events WHERE event_type='hitl_request'"))
    assert 'injected operation' not in json.dumps(request)


async def test_native_active_stop_keeps_execution_gate_until_worker_terminal(projection_database,monkeypatch):
    sessions,admin=projection_database
    from workmate_product.coordination import WakeupBus
    bus = WakeupBus('unused')
    sessions.configure(info={'wakeup_bus': bus})
    await parked(sessions,admin)
    service=runtime(sessions,monkeypatch)
    async with sessions() as db:
        user,conversation,task=await loaded(db)
        await service.stop(db,user,conversation,task)
        assert task.status=='cancelling'
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE kind='StopTask' AND status='accepted'")==1
    assert 'commands' in bus._pending
    assert 'conversation:c' in bus._pending
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
    async with sessions() as db:
        user,conversation,task=await loaded(db)
        await service.stop(db,user,conversation,task)
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==1


async def test_native_queued_unadmitted_stop_cancels_without_runtime_command(projection_database,monkeypatch):
    sessions,admin=projection_database
    async with sessions.begin() as db:
        db.add(Task(id='queued',conversation_id='c',title='Queued'))
    service=runtime(sessions,monkeypatch)
    async with sessions() as db:
        user,conversation,task=await loaded(db,'queued')
        await service.stop(db,user,conversation,task)
        assert task.status=='cancelled'
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='queued'")=='cancelled'
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0
    assert await admin.fetchval("SELECT count(*) FROM runtime_task_executions WHERE task_id='queued'")==0
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'


async def test_native_confirm_rechecks_real_agent_access(projection_database):
    sessions,admin=projection_database
    original=await parked(sessions,admin)
    service=ProductRuntime(sessions,None)
    async with sessions() as db:
        assert await service._authorize(db,'u','a') is False
    with pytest.raises(ApprovalConflict): await confirm(service,sessions,original.request_version)
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_decision'")==0


async def test_native_full_access_requires_durable_sandbox_attestation(projection_database,monkeypatch):
    sessions,admin=projection_database
    original=await parked(sessions,admin)
    # Keep a live owner so this case reaches sandbox attestation validation;
    # ownerless sessions are separately gated by a proven released boundary.
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=clock_timestamp()+interval '30 seconds'")
    service=runtime(sessions,monkeypatch)
    async with sessions() as db:
        user,conversation,task=await loaded(db)
        with pytest.raises(ApprovalConflict,match='proven sandbox isolation'):
            await service.confirm(db,user,conversation,task,[{'tool_call_id':'a','approved':True}],
                decision_version=original.request_version,full_access=True)
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0
    assert await admin.fetchval('SELECT count(*) FROM runtime_permission_upgrades')==0


async def test_http_confirm_and_stop_use_native_runtime_without_app_http(projection_database,monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api.routers.tasks import router
    from app.auth import get_current_user
    from app.db import get_db
    sessions,admin=projection_database
    original=await parked(sessions,admin)
    application=FastAPI()
    application.include_router(router,prefix='/api')
    application.state.product_runtime=runtime(sessions,monkeypatch)
    class ForbiddenRuntime:
        def __getattr__(self,name): raise AssertionError('legacy Runtime HTTP must not be used')
    application.state.runtime_client=ForbiddenRuntime()
    async def session_dependency():
        async with sessions() as db: yield db
    async def user_dependency():
        async with sessions() as db: return await db.get(User,'u')
    application.dependency_overrides[get_db]=session_dependency
    application.dependency_overrides[get_current_user]=user_dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://test') as client:
        response=await client.post('/api/tasks/t/confirm',json={'decision_version':original.request_version,
            'decisions':[{'tool_call_id':'a','approved':True,'tool_call_name':'Bash','tool_call_input':{'command':'injected'}}]})
        assert response.status_code==200,response.text
        assert response.json()['status']=='awaiting_permission'
        stale=await client.post('/api/tasks/t/confirm',json={'decision_version':'stale','decisions':[{'tool_call_id':'a','approved':True}]})
        assert stale.status_code==409,stale.text
        stopped=await client.post('/api/tasks/t/stop')
        assert stopped.status_code==200,stopped.text
        assert stopped.json()['status']=='cancelling'
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE kind='StopTask'")==1
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
