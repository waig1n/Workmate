"""Read tools preserve Product scope/Agent/capability views without Web imports."""
import pytest
from test_platform_mutations import platform_database,request
from test_execution_context import database,bind_capability
from workmate_product.application.platform_reads import execute_platform_read


async def read(sessions,name,args=None):
    async with sessions.begin() as db:
        return await execute_platform_read(db,request().model_copy(update={'tool_name':name,'arguments':args or {}}),model_profiles=[])


@pytest.mark.asyncio
async def test_all_platform_reads_preserve_context_identity_and_business_views(platform_database):
    sessions,admin=platform_database
    await admin.execute("UPDATE tasks SET status='created',run_id=NULL")
    result=await read(sessions,'platform_get_context')
    assert result['user_id']=='u' and result['calling_agent_id']=='a'
    assert result['scopes'][0]['kind']=='personal'
    assert await read(sessions,'model_list')==[]
    agents=await read(sessions,'agent_list')
    assert agents[0]['id']=='a' and agents[0]['actions']['update'] is True
    assert (await read(sessions,'agent_get',{'agent_id':'a'}))==agents[0]
    assert await read(sessions,'capability_list')==[]
    validated=await read(sessions,'agent_validate',{'operation':'create','draft':{'slug':'new','display_name':'New'}})
    assert validated=={'valid':True,'operation':'create','scope_id':'scope','agent_id':None,'binding_ids':[]}
    assert await admin.fetchval('SELECT count(*) FROM agents')==1
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts')==0


@pytest.mark.asyncio
async def test_capability_read_does_not_expose_mcp_connection_data(platform_database):
    sessions,admin=platform_database
    await bind_capability(sessions,'skill')
    capabilities=await read(sessions,'capability_list')
    assert capabilities[0]['id']=='skill' and 'config' not in capabilities[0]
    agent=await read(sessions,'agent_get',{'agent_id':'a'})
    assert agent['skill_binding_ids']==['skill']


@pytest.mark.asyncio
async def test_platform_boundary_honors_current_knowledge_configuration(platform_database):
    from sqlalchemy import text
    from workmate_product.persistence.models import KnowledgeBase
    from workmate_product.application.platform_types import PlatformToolError
    sessions,admin=platform_database
    async with sessions.begin() as db:
        db.add(KnowledgeBase(id='kb',scope_id='scope',name='Knowledge',runtime_id='physical',runtime_owner_id='u'))
        await db.execute(text('UPDATE agents SET knowledge_config=CAST(:config AS json) WHERE id=\'a\''),
            {'config':'{"knowledge_base_ids":["kb"]}'})
    # A newly added KB never broadens the admitted Task ceiling, but current
    # bindings must remain usable before any platform data is read.
    with pytest.raises(PlatformToolError):
        await read(sessions,'agent_list')
    async with sessions.begin() as db:
        result=await execute_platform_read(db,request().model_copy(update={'tool_name':'agent_list','arguments':{}}),
            model_profiles=[],knowledge_enabled=True)
        assert result[0]['unavailable_knowledge_base_ids']==[]
    await admin.execute("UPDATE knowledge_bases SET enabled=false WHERE id='kb'")
    with pytest.raises(PlatformToolError):
        async with sessions.begin() as db:
            await execute_platform_read(db,request().model_copy(update={'tool_name':'agent_list','arguments':{}}),
                model_profiles=[],knowledge_enabled=True)


@pytest.mark.asyncio
async def test_validate_update_is_versioned_and_read_does_not_write(platform_database):
    sessions,admin=platform_database
    before=await read(sessions,'agent_get',{'agent_id':'a'})
    result=await read(sessions,'agent_validate',{'operation':'update','agent_id':'a',
        'expected_updated_at':before['updated_at'],'draft':{'display_name':'Proposed'}})
    assert result=={'valid':True,'operation':'update','scope_id':'scope','agent_id':'a','binding_ids':[]}
    assert (await read(sessions,'agent_get',{'agent_id':'a'}))['display_name']==before['display_name']
    assert await read(sessions,'agent_list',{'scope':'organization'})==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args,code',[
    ('agent_get',{'agent_id':'missing'},404),('unknown',{},404),('model_list',{'extra':True},422),
    ('capability_list',{'scope_id':'missing'},404),('capability_list',{'scope_id':'scope','agent_id':'a'},422),
    ('agent_validate',{'operation':'update','draft':{}},422),
    ('agent_validate',{'operation':'create','draft':{'slug':'new','display_name':'New','skill_binding_ids':['missing']}},422),
])
async def test_read_tools_keep_api_error_semantics(platform_database,name,args,code):
    from workmate_product.application.platform_types import PlatformToolError
    sessions,admin=platform_database
    with pytest.raises(PlatformToolError) as failure:
        await read(sessions,name,args)
    assert failure.value.status_code==code
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts')==0


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    "UPDATE runtime_sessions SET owner_id='superseded'",
    "UPDATE runtime_sessions SET execution_epoch=2",
    "UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'",
    "UPDATE tasks SET status='cancelling'",
    "UPDATE agents SET tool_policy='{\"denied_tools\":[\"agent_list\"]}'",
])
async def test_read_shared_authority_rejects_stale_execution_and_revocation(platform_database, mutation):
    from workmate_product.application.platform_types import PlatformToolError
    sessions, admin = platform_database
    await admin.execute(mutation)
    with pytest.raises(PlatformToolError):
        await read(sessions, 'agent_list')
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts') == 0
