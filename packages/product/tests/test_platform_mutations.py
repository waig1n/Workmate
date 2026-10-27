"""Real Product writes, durable approval and Worker fencing on PostgreSQL."""
import asyncio
import json
import pytest
import pytest_asyncio
from sqlalchemy import text
from test_execution_context import database,record
from workmate_product.application.platform_types import PlatformToolRequest
from workmate_product.application.platform_tools import execute_platform_mutation
from workmate_product.application.platform_types import PlatformToolError


@pytest_asyncio.fixture
async def platform_database(database):
    sessions,admin=database
    await admin.execute("UPDATE agents SET platform_tool_profile='workspace'")
    await record(sessions)
    await admin.execute("""
        UPDATE tasks SET status='running',run_id='reply';
        UPDATE runtime_sessions SET owner_id='worker',execution_epoch=1,active_task_id='t',
            status='running',reply_id='reply',lease_until=clock_timestamp()+interval '60 seconds';
        UPDATE runtime_task_executions SET execution_epoch=1,status='running',reply_id='reply';""")
    yield sessions,admin


def request(**changes):
    return PlatformToolRequest(**dict(actor_user_id='u',agent_id='a',conversation_id='c',
        session_id='s',task_id='t',worker_id='worker',storage_node_id='node',execution_epoch=1,
        reply_id='reply',tool_call_id='call',tool_name='agent_create',
        arguments={'slug':'created','display_name':'Created'},**changes))


async def approve(sessions,req):
    async with sessions.begin() as db:
        await db.execute(text('''INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
            VALUES ('t','c',1,'hitl_request',CAST(:request AS json)),
            ('t','c',2,'hitl_decision',CAST(:decision AS json))'''),
            {'request':json.dumps({'replyId':'reply','decisionVersion':'v','toolCalls':[
                {'id':req.tool_call_id,'name':req.tool_name,'input':req.arguments}]}),
             'decision':json.dumps({'requestVersion':'v','actorUserId':'u','decisions':[
                 {'toolCallId':req.tool_call_id,'approved':True}]})})


async def execute(sessions,req):
    async with sessions.begin() as db:
        return await execute_platform_mutation(db,req)


@pytest.mark.asyncio
async def test_real_create_concurrent_retry_one_agent_and_receipt(platform_database):
    sessions,admin=platform_database
    req=request()
    await approve(sessions,req)
    results=await asyncio.gather(*(execute(sessions,req) for _ in range(3)))
    assert results[0]==results[1]==results[2]
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE slug='created'")==1
    assert await admin.fetchval("SELECT count(*) FROM tool_execution_receipts WHERE status='completed'")==1
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='agent.create'")==1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_sessions SET owner_id='old'",'UPDATE runtime_sessions SET execution_epoch=2',
    "UPDATE runtime_sessions SET lease_until=now()-interval '1 second'",
    "UPDATE runtime_sessions SET reply_id='other'", "UPDATE tasks SET status='cancelling'",
    'UPDATE conversations SET active_product_task_id=NULL',"UPDATE users SET status='disabled'",
    "UPDATE agents SET tool_policy='{\"denied_tools\":[\"agent_create\"]}'",
])
async def test_stale_owner_or_revocation_cannot_write(platform_database,mutation):
    sessions,admin=platform_database
    req=request()
    await approve(sessions,req)
    await admin.execute(mutation)
    with pytest.raises(PlatformToolError):
        await execute(sessions,req)
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE slug='created'")==0
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts')==0


@pytest.mark.asyncio
async def test_approval_digest_and_atomic_rollback(platform_database):
    sessions,admin=platform_database
    req=request()
    with pytest.raises(PlatformToolError):
        await execute(sessions,req)
    await approve(sessions,req)
    with pytest.raises(RuntimeError,match='rollback'):
        async with sessions.begin() as db:
            await execute_platform_mutation(db,req)
            raise RuntimeError('rollback')
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts')==0
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE slug='created'")==0
    await execute(sessions,req)
    altered=req.model_copy(update={'arguments':{**req.arguments,'display_name':'Different'}})
    with pytest.raises(PlatformToolError):
        await execute(sessions,altered)


@pytest.mark.asyncio
async def test_receipt_cannot_authorize_previous_worker(platform_database):
    sessions,admin=platform_database
    req=request()
    await approve(sessions,req)
    await execute(sessions,req)
    await admin.execute("UPDATE runtime_sessions SET owner_id='new-owner',execution_epoch=2")
    with pytest.raises(PlatformToolError,match='authority'):
        await execute(sessions,req)


@pytest.mark.asyncio
async def test_update_uses_original_product_implementation_and_failure_receipt(platform_database):
    sessions,admin=platform_database
    updated=await admin.fetchval("SELECT updated_at FROM agents WHERE id='a'")
    req=request().model_copy(update={'tool_name':'agent_update','arguments':{'agent_id':'a',
        'expected_updated_at':updated.isoformat(),'display_name':'Renamed','is_default':True}})
    await approve(sessions,req)
    result=await execute(sessions,req)
    assert result['updated'] is True
    assert await admin.fetchval("SELECT display_name FROM agents WHERE id='a'")=='Renamed'
    assert await admin.fetchval("SELECT agent_id FROM agent_defaults WHERE scope_id='scope'")=='a'
    assert await execute(sessions,req)==result
    await admin.execute('DELETE FROM task_events')
    stale=req.model_copy(update={'tool_call_id':'call2'})
    await approve(sessions,stale)
    failed=await execute(sessions,stale)
    assert failed['error'] and failed['detail']=='Agent version changed'
    assert await execute(sessions,stale)==failed
    assert await admin.fetchval("SELECT status FROM tool_execution_receipts WHERE tool_call_id='call2'")=='failed'


@pytest.mark.asyncio
async def test_lease_expiry_during_actual_write_rolls_back_everything(platform_database):
    sessions,admin=platform_database
    req=request()
    await approve(sessions,req)
    await admin.execute("""CREATE FUNCTION slow_create() RETURNS trigger AS $$
        BEGIN UPDATE runtime_sessions SET lease_until=clock_timestamp()+interval '.1 second';
        PERFORM pg_sleep(.2); RETURN NEW; END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER slow_create BEFORE INSERT ON agents FOR EACH ROW EXECUTE FUNCTION slow_create();""")
    with pytest.raises(PlatformToolError,match='expired'):
        await execute(sessions,req)
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE slug='created'")==0
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts')==0
    assert await admin.fetchval('SELECT count(*) FROM security_audit_events')==0


@pytest.mark.asyncio
async def test_skill_install_real_archive_pointer_upload_and_receipt_atomic(platform_database):
    import base64,hashlib
    from io import BytesIO
    from zipfile import ZipFile
    from workmate_product.persistence.models import Scope,ScopeClosure,Role,Permission,RolePermission,RoleBinding
    sessions,admin=platform_database
    async with sessions.begin() as db:
        db.add(Scope(id='org',kind='organization',name='Organization'))
        db.add_all([Role(id='r',key='manager',display_name='Manager'),Permission(id='p',key='skill.manage')])
        await db.flush()
        db.add_all([ScopeClosure(ancestor_id='org',descendant_id='org',depth=0),
            RolePermission(id='rp',role_id='r',permission_id='p'),RoleBinding(user_id='u',scope_id='org',role_id='r')])
    output=BytesIO()
    with ZipFile(output,'w') as bundle:
        bundle.writestr('SKILL.md','---\nname: from-platform\ndescription: Example\n---\nBody')
    raw=output.getvalue()
    digest=hashlib.sha256(raw).hexdigest()
    req=request().model_copy(update={'tool_name':'skill_install','arguments':{'scope_id':'org','filename':'example.zip',
        'archive_base64':base64.b64encode(raw).decode(),'archive_sha256':digest,'display_name':None,'description':None}})
    approval=req.model_copy(update={'arguments':{'scope_id':'org','skill_directory':'working/example',
        'expected_sha256':digest,'display_name':None,'description':None}})
    await approve(sessions,approval)
    result=await execute(sessions,req)
    assert result.get('installed') is True,result
    assert await execute(sessions,req)==result
    assert await admin.fetchval('SELECT artifact_digest FROM capability_skill_artifacts WHERE capability_id=$1',result['skill']['id'])==digest
    assert await admin.fetchval('SELECT count(*) FROM skill_upload_sources')==1
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts')==1


@pytest.mark.asyncio
async def test_real_binding_and_default_then_binding_failure_rolls_back(platform_database):
    from test_execution_context import bind_capability
    sessions,admin=platform_database
    await bind_capability(sessions,'cap')
    req=request().model_copy(update={'arguments':{'slug':'bound','display_name':'Bound',
        'skill_binding_ids':['cap'],'is_default':True}})
    await approve(sessions,req)
    result=await execute(sessions,req)
    assert result.get('created') is True,result
    created=result['agent']['id']
    assert await admin.fetchval('SELECT capability_installation_id FROM agent_capability_bindings WHERE agent_id=$1',created)=='cap'
    assert await admin.fetchval("SELECT agent_id FROM agent_defaults WHERE scope_id='scope'")==created
    await admin.execute('DELETE FROM task_events')
    bad=req.model_copy(update={'tool_call_id':'call2','arguments':{'agent_id':created,
        'expected_updated_at':result['agent']['updated_at'],'display_name':'Must rollback','skill_binding_ids':['absent']},
        'tool_name':'agent_update'})
    await approve(sessions,bad)
    failure=await execute(sessions,bad)
    assert failure['error'] is True and failure['status_code']==422
    assert await admin.fetchval('SELECT display_name FROM agents WHERE id=$1',created)=='Bound'
    assert await admin.fetchval('SELECT capability_installation_id FROM agent_capability_bindings WHERE agent_id=$1',created)=='cap'


@pytest.mark.asyncio
@pytest.mark.parametrize('values,code',[
    ({'visibility':'public'},422),({'slug':'INVALID SLUG'},422),({'scope_id':'missing'},404),
])
async def test_existing_product_create_validation_is_preserved(platform_database,values,code):
    sessions,admin=platform_database
    req=request().model_copy(update={'arguments':{**request().arguments,**values}})
    await approve(sessions,req)
    result=await execute(sessions,req)
    assert result['error'] and result['status_code']==code
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE id<>'a'")==0


@pytest.mark.asyncio
async def test_worker_current_reply_can_precede_product_running_projection(platform_database):
    sessions,admin=platform_database
    req=request()
    await approve(sessions,req)
    await admin.execute("UPDATE tasks SET status='created',run_id=NULL")
    assert (await execute(sessions,req))['created'] is True


@pytest.mark.asyncio
async def test_mutation_checks_immutable_execution_spec_digest(platform_database):
    sessions,admin=platform_database
    req=request()
    await approve(sessions,req)
    await admin.execute("ALTER TABLE runtime_execution_specs DISABLE TRIGGER USER")
    await admin.execute("UPDATE runtime_execution_specs SET digest=repeat('0',64)")
    with pytest.raises(PlatformToolError,match='spec'):
        await execute(sessions,req)
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts')==0


@pytest.mark.asyncio
async def test_product_scope_then_agent_update_cannot_deadlock_worker_mutation(platform_database):
    sessions,admin=platform_database
    req=request()
    await approve(sessions,req)
    work=None
    try:
        async with sessions.begin() as db:
            await db.execute(text("SELECT id FROM scopes WHERE id='scope' FOR UPDATE"))
            work=asyncio.create_task(execute(sessions,req))
            await asyncio.sleep(.1)
            await db.execute(text("SET LOCAL lock_timeout='500ms'"))
            await db.execute(text("UPDATE agents SET description='Product concurrent update' WHERE id='a'"))
        assert (await asyncio.wait_for(work,5))['created'] is True
    finally:
        if work is not None:
            await asyncio.gather(work,return_exceptions=True)


@pytest.mark.asyncio
async def test_removed_original_capability_denies_platform_write(database):
    from test_execution_context import bind_capability
    sessions,admin=database
    await bind_capability(sessions,'cap')
    await admin.execute("UPDATE agents SET platform_tool_profile='workspace'")
    await record(sessions)
    await admin.execute("""UPDATE tasks SET status='running',run_id='reply';
        UPDATE runtime_sessions SET owner_id='worker',execution_epoch=1,active_task_id='t',status='running',reply_id='reply',lease_until=now()+interval '30 seconds';
        UPDATE runtime_task_executions SET execution_epoch=1,status='running',reply_id='reply';
        DELETE FROM agent_capability_bindings;""")
    req=request()
    await approve(sessions,req)
    with pytest.raises(PlatformToolError,match='removed'):
        await execute(sessions,req)
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts')==0


@pytest.mark.asyncio
async def test_unready_skill_cannot_be_bound_by_platform_create(platform_database):
    from test_execution_context import bind_capability
    sessions,admin=platform_database
    await bind_capability(sessions,'unready',ready=False)
    req=request().model_copy(update={'arguments':{'slug':'bound','display_name':'Bound','skill_binding_ids':['unready']}})
    await approve(sessions,req)
    result=await execute(sessions,req)
    assert result.get('error') is True and result['status_code']==422
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE slug='bound'")==0
