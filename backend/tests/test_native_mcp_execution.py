"""HTTP MCP revisions reach the Worker's fenced private resolver unchanged."""
import pytest
from test_native_mcps import native_mcps, install, share_mcp, TOKEN, NEW_TOKEN
from test_native_agents import native_agents, agent_body
from test_durable_projection import projection_database


@pytest.mark.parametrize('migrated', [False, True])
async def test_http_mcp_update_keeps_existing_execution_pinned_and_fenced(native_mcps, migrated):
    from workmate_product.application.execution_context import record_execution_spec, SelectedModel, ExecutionContextRejected
    from workmate_product.mcp.repository import load_execution_mcp_definitions
    from workmate_product.persistence.models import TaskMessage
    client, sessions, admin, _ = native_mcps
    identifier = await install(client)
    await share_mcp(client, identifier)
    agent = await client.post('/api/agents', json=agent_body(mcp_binding_ids=[identifier]))
    assert agent.status_code == 201, agent.text
    from workmate_product.persistence.models import ResourceShare
    async with sessions.begin() as db:
        db.add(ResourceShare(id='execution-agent-grant', agent_id=agent.json()['id'], target_scope_id='scope'))
    await admin.execute("UPDATE conversations SET agent_id=$1,model_profile='chat'", agent.json()['id'])
    async with sessions.begin() as db:
        db.add(TaskMessage(id='input',task_id='t',conversation_id='c',role='user',content={'text':'work'}))
    await admin.execute("""UPDATE runtime_sessions SET status='accepted',execution_epoch=0,active_task_id=NULL,reply_id=NULL;
        UPDATE runtime_task_executions SET status='accepted',execution_epoch=0,reply_id=NULL;
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
        VALUES ('start','s','c','t','u','StartTask','{}',repeat('a',64));""")
    if migrated:
        import uuid
        from workmate_product.persistence.models import Scope, ScopeClosure
        async with sessions.begin() as db:
            db.add(Scope(id='legacy-dept',kind='department',name='Legacy',parent_id='scope'))
            await db.flush()
            db.add(ScopeClosure(ancestor_id='scope',descendant_id='legacy-dept',depth=1))
            db.add(ScopeClosure(ancestor_id='legacy-dept',descendant_id='legacy-dept',depth=0))
        legacy_version=str(uuid.uuid4())
        await admin.execute("""INSERT INTO mcp_secret_revisions(id,capability_id,scope_id,payload)
            SELECT $1,capability_id,'legacy-dept',payload FROM mcp_secret_revisions
            WHERE id=(SELECT secret_version FROM capability_mcp_revisions WHERE capability_id=$2)""",legacy_version,identifier)
        await admin.execute('UPDATE capability_mcp_revisions SET secret_version=$1 WHERE capability_id=$2',legacy_version,identifier)
        await admin.execute("UPDATE capability_installations SET scope_id='legacy-dept' WHERE id=$1",identifier)
    async with sessions.begin() as db:
        spec = await record_execution_spec(db,task_id='t',session_id='s',actor_user_id='u',
            selected_model=SelectedModel(profile_name='chat',model_type='openai',model='fixture',api_key_env='CHAT_MODEL_API_KEY'))
    assert len(spec.mcp_configurations) == 1
    version = spec.mcp_configurations[0].secret_version
    assert TOKEN not in spec.model_dump_json()
    if migrated:
        # The organization migration changes only public ownership/provenance;
        # the running Task and its private credential revision remain immutable.
        await admin.execute("""UPDATE capability_installations SET scope_id='scope',
            metadata=metadata::jsonb || '{"central_library_origin":{"scope_id":"legacy-dept","scope_name":"Legacy"}}'::jsonb
            WHERE id=$1""",identifier)
    updated = await client.patch('/api/mcps/' + identifier, json={'values':{'token':NEW_TOKEN}})
    assert updated.status_code == 200, updated.text
    assert await admin.fetchval('SELECT secret_version FROM capability_mcp_revisions WHERE capability_id=$1', identifier) != version
    await admin.execute("""UPDATE runtime_sessions SET status='preparing',execution_epoch=1,active_task_id='t',
        owner_id='worker',lease_until=clock_timestamp()+interval '60 seconds';
        UPDATE runtime_task_executions SET status='preparing',execution_epoch=1;
        UPDATE runtime_commands SET status='claimed',claimed_by='worker',claimed_epoch=1;""")
    async def read(owner):
        async with sessions.begin() as db:
            return await load_execution_mcp_definitions(db,session_id='s',task_id='t',worker_id=owner,
                execution_epoch=1,storage_node_id='node')
    context, definitions = await read('worker')
    assert context.initial_spec.mcp_configurations[0].secret_version == version
    assert definitions[0].client.mcp_config.headers == {'Authorization':'Bearer ' + TOKEN}
    assert TOKEN not in repr(definitions[0])
    with pytest.raises(ExecutionContextRejected):
        await read('stale-worker')
    # Revoke use only. Membership and management authority remain in place,
    # proving the private resolver rechecks the current explicit grant.
    revoked = await client.delete('/api/sharing/mcps/' + identifier + '/scope')
    assert revoked.status_code == 204, revoked.text
    assert await admin.fetchval('SELECT count(*) FROM scope_memberships') > 0
    assert await admin.fetchval('SELECT count(*) FROM role_bindings') > 0
    with pytest.raises(ExecutionContextRejected):
        await read('worker')
    # The secret history is intact; its presence alone cannot authorize use.
    assert await admin.fetchval('SELECT count(*) FROM mcp_secret_revisions') == (3 if migrated else 2)
