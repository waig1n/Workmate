"""Agent authority readers share rows while mutations and conversation gates fence."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_durable_projection import projection_database
from test_execution_context import database, record, claim
from test_platform_mutations import platform_database, request
from workmate_product.persistence.repositories import start_admission, control_admission, task_controls, node_recovery


async def agent_read(db, entry):
    if entry=='start':return await start_admission.lock_agent(db,'a')
    if entry=='control':return await control_admission.lock_agent(db,id='a')
    if entry=='disabled_stop':return await control_admission.lock_agent_identity(db,{'agent_id':'a'})
    if entry=='task_control':return await task_controls.lock_agent(db,'a')
    if entry=='node_evidence':return await node_recovery.lock_execution_owner(db,agent_id='a',conversation_id='c',task_id='t')
    raise AssertionError(entry)


@pytest.mark.parametrize('entry',['start','control','disabled_stop','task_control','node_evidence'])
async def test_agent_readers_do_not_serialize_other_conversations(projection_database,entry):
    sessions,_=projection_database
    async with sessions.begin() as first:
        await first.execute(text("SELECT id FROM agents WHERE id='a' FOR SHARE"))
        async with sessions.begin() as second:
            await second.execute(text("SET LOCAL lock_timeout='200ms'"))
            await agent_read(second,entry)


@pytest.mark.parametrize('statement',[
    "UPDATE agents SET display_name='changed' WHERE id='a'",
    "UPDATE agents SET deleted_at=clock_timestamp() WHERE id='a'",
    "DELETE FROM agents WHERE id='a'",
])
async def test_shared_authority_still_blocks_agent_mutation(projection_database,statement):
    sessions,_=projection_database
    async with sessions.begin() as first:
        await start_admission.lock_agent(first,'a')
        async with sessions() as second:
            await second.execute(text("SET LOCAL lock_timeout='200ms'"))
            with pytest.raises(DBAPIError) as error:
                await second.execute(text(statement))
            assert getattr(error.value.orig,'sqlstate',None)=='55P03'
            await second.rollback()


async def test_same_conversation_lock_remains_exclusive(projection_database):
    sessions,_=projection_database
    async with sessions.begin() as first:
        await control_admission.lock_agent(first,id='a')
        await control_admission.lock_conversation(first,id='c')
        async with sessions() as second:
            await second.execute(text("SET LOCAL lock_timeout='200ms'"))
            # Compatible Agent reads must not weaken the actual execution gate.
            await control_admission.lock_agent(second,id='a')
            with pytest.raises(DBAPIError) as error:
                await control_admission.lock_conversation(second,id='c')
            assert getattr(error.value.orig,'sqlstate',None)=='55P03'
            await second.rollback()


async def test_execution_context_reads_under_another_agent_reader(database):
    from workmate_product.application.execution_context import read_execution_context
    sessions,admin=database
    await record(sessions)
    await claim(admin)
    async with sessions.begin() as first:
        await first.execute(text("SELECT id FROM agents WHERE id='a' FOR SHARE"))
        async with sessions.begin() as second:
            await second.execute(text("SET LOCAL lock_timeout='200ms'"))
            context=await read_execution_context(second,session_id='s',task_id='t',worker_id='worker',
                execution_epoch=1,storage_node_id='node',knowledge_enabled=False)
            assert context.agent_id=='a'


async def test_readonly_platform_call_does_not_upgrade_agent_lock(platform_database):
    from workmate_product.application.platform_reads import execute_platform_read
    sessions,_=platform_database
    async with sessions.begin() as first:
        await first.execute(text("SELECT id FROM agents WHERE id='a' FOR SHARE"))
        async with sessions.begin() as second:
            await second.execute(text("SET LOCAL lock_timeout='200ms'"))
            result=await execute_platform_read(second,request().model_copy(update={
                'tool_name':'platform_get_context','arguments':{}}),model_profiles=[])
            assert result['calling_agent_id']=='a'
