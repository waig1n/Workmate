"""Private MCP definitions are ready atomically and never projected as public data."""
import asyncio,json
import pytest,pytest_asyncio
from workmate_product.persistence.models import (Role,Permission,RolePermission,RoleBinding,
    CapabilityInstallation,MCPSecretRevision,CapabilityMCPRevision,Scope,ScopeClosure,ScopeMembership,ResourceShare)
from workmate_product.mcp.repository import install_mcp,update_mcp,uninstall_mcp,ready_mcp_revisions
from workmate_product.mcp.definitions import MCPDefinitionError
from workmate_product.mcp.secret_store import PostgresMCPSecretStore
from test_execution_context import database
from test_mcp_definitions import card

@pytest_asyncio.fixture
async def mcp_database(database):
    sessions,admin=database
    async with sessions.begin() as db:
        db.add(Scope(id='org',kind='organization',name='Org'))
        await db.flush()
        db.add_all([ScopeClosure(ancestor_id='org',descendant_id='org',depth=0),ScopeMembership(scope_id='org',user_id='u')])
        db.add_all([Role(id='mr',key='mcp-manager',display_name='MCP manager'),Permission(id='mp',key='mcp.manage')])
        await db.flush()
        db.add_all([RolePermission(id='mrp',role_id='mr',permission_id='mp'),RoleBinding(user_id='u',scope_id='org',role_id='mr')])
    yield sessions,admin

async def install(sessions,name=None):
    async with sessions.begin() as db:
        return await install_mcp(db,actor_user_id='u',scope_id='org',card=card(),values={'TOKEN':'private-token'},name=name)

@pytest.mark.asyncio
async def test_ready_pointer_registry_and_private_revision_are_atomic(mcp_database):
    sessions,admin=mcp_database
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            installed=await install_mcp(db,actor_user_id='u',scope_id='org',card=card(),values={'TOKEN':'private-token'})
            assert await db.get(CapabilityMCPRevision,installed.id)
            assert await admin.fetchval('SELECT count(*) FROM mcp_secret_revisions')==0
            raise RuntimeError('rollback')
    assert await admin.fetchval('SELECT count(*) FROM capability_installations')==0
    installed=await install(sessions)
    assert 'private-token' not in json.dumps(installed.metadata_)
    assert await admin.fetchval('SELECT count(*) FROM resource_shares') == 0
    audits=await admin.fetch('SELECT metadata::text FROM security_audit_events')
    assert 'private-token' not in repr(audits)
    async with sessions.begin() as db:
        refs=await ready_mcp_revisions(db,[installed])
        private=await PostgresMCPSecretStore().get(db,capability_id=installed.id,scope_id='org',secret_version=refs[installed.id])
        assert private.client.mcp_config.env['API_KEY']=='private-token'
        assert 'private-token' not in repr(private)

@pytest.mark.asyncio
async def test_concurrent_installation_and_revoked_authority(mcp_database):
    sessions,admin=mcp_database
    results=await asyncio.gather(install(sessions),install(sessions),return_exceptions=True)
    assert sum(isinstance(r,CapabilityInstallation) for r in results)==1
    error=next(r for r in results if isinstance(r,MCPDefinitionError))
    assert error.status_code==409
    await admin.execute('DELETE FROM role_bindings')
    with pytest.raises(MCPDefinitionError):
        await install(sessions,'other')

@pytest.mark.asyncio
async def test_secret_update_creates_new_version_and_uninstall_retains_private_history(mcp_database):
    sessions,admin=mcp_database
    installed=await install(sessions)
    original=await admin.fetchval('SELECT secret_version FROM capability_mcp_revisions')
    async with sessions.begin() as db:
        await update_mcp(db,actor_user_id='u',installation_id=installed.id,card=card(),values={'TOKEN':'rotated'})
    current=await admin.fetchval('SELECT secret_version FROM capability_mcp_revisions')
    assert current!=original
    async with sessions.begin() as db:
        old=await PostgresMCPSecretStore().get(db,capability_id=installed.id,scope_id='org',secret_version=original)
        assert old.client.mcp_config.env['API_KEY']=='private-token'
        await uninstall_mcp(db,actor_user_id='u',installation_id=installed.id)
    assert await admin.fetchval('SELECT count(*) FROM capability_mcp_revisions')==0
    assert await admin.fetchval('SELECT count(*) FROM mcp_secret_revisions')==2
    import asyncpg
    with pytest.raises(asyncpg.RaiseError,match='immutable'):
        await admin.execute("UPDATE mcp_secret_revisions SET payload='{}'")

@pytest.mark.asyncio
async def test_execution_spec_pins_only_opaque_mcp_revision_and_preserves_it_on_update(mcp_database):
    from workmate_product.persistence.models import AgentCapabilityBinding
    from test_execution_context import record,claim,read
    sessions,admin=mcp_database
    installed=await install(sessions)
    async with sessions.begin() as db:
        db.add(AgentCapabilityBinding(agent_id='a',capability_installation_id=installed.id))
        db.add(ResourceShare(id='mcp-use-grant', capability_id=installed.id, target_scope_id='org'))
    spec=await record(sessions)
    assert len(spec.mcp_configurations)==1
    assert 'private-token' not in spec.model_dump_json()
    old=spec.mcp_configurations[0].secret_version
    async with sessions.begin() as db:
        await update_mcp(db,actor_user_id='u',installation_id=installed.id,card=card(),values={'TOKEN':'new-token'})
    await claim(admin)
    context=await read(sessions)
    assert context.initial_spec.mcp_configurations[0].secret_version==old

@pytest.mark.asyncio
async def test_private_execution_read_requires_current_lease_and_capability_authority(mcp_database):
    from workmate_product.persistence.models import AgentCapabilityBinding
    from workmate_product.mcp.repository import load_execution_mcp_definitions
    from workmate_product.application.execution_context import ExecutionContextRejected
    from test_execution_context import record,claim
    sessions,admin=mcp_database
    installed=await install(sessions)
    async with sessions.begin() as db:
        db.add(AgentCapabilityBinding(agent_id='a',capability_installation_id=installed.id))
        db.add(ResourceShare(id='mcp-use-grant', capability_id=installed.id, target_scope_id='org'))
    await record(sessions);await claim(admin)
    async def load(worker='worker'):
        async with sessions.begin() as db:
            return await load_execution_mcp_definitions(db,session_id='s',task_id='t',worker_id=worker,
                execution_epoch=1,storage_node_id='node')
    context,definitions=await load()
    assert definitions[0].client.mcp_config.env['API_KEY']=='private-token'
    with pytest.raises(ExecutionContextRejected):
        await load('other-worker')
    await admin.execute('DELETE FROM resource_shares WHERE capability_id=$1', installed.id)
    assert await admin.fetchval("SELECT count(*) FROM scope_memberships WHERE scope_id='org'") == 1
    assert await admin.fetchval("SELECT count(*) FROM role_bindings WHERE scope_id='org'") == 1
    with pytest.raises(ExecutionContextRejected):
        await load()

@pytest.mark.asyncio
async def test_name_enable_changes_and_bound_disable_delete_guards(mcp_database):
    from workmate_product.persistence.models import AgentCapabilityBinding
    sessions,admin=mcp_database
    installed=await install(sessions)
    async with sessions.begin() as db:
        renamed=await update_mcp(db,actor_user_id='u',installation_id=installed.id,name='renamed',enabled=False)
        assert renamed.name=='renamed' and renamed.enabled is False
    async with sessions.begin() as db:
        await update_mcp(db,actor_user_id='u',installation_id=installed.id,enabled=True)
        db.add(AgentCapabilityBinding(agent_id='a',capability_installation_id=installed.id))
        db.add(ResourceShare(id='mcp-use-grant', capability_id=installed.id, target_scope_id='org'))
    for operation in ('disable','delete'):
        with pytest.raises(MCPDefinitionError) as error:
            async with sessions.begin() as db:
                if operation=='disable':
                    await update_mcp(db,actor_user_id='u',installation_id=installed.id,enabled=False)
                else:
                    await uninstall_mcp(db,actor_user_id='u',installation_id=installed.id)
        assert error.value.status_code==409

@pytest.mark.asyncio
async def test_missing_ready_or_wrong_secret_binding_fails_closed(mcp_database):
    sessions,admin=mcp_database
    installed=await install(sessions)
    version=await admin.fetchval('SELECT secret_version FROM capability_mcp_revisions')
    async with sessions.begin() as db:
        with pytest.raises(MCPDefinitionError):
            await PostgresMCPSecretStore().get(db,capability_id=installed.id,scope_id='wrong-scope',secret_version=version)
    await admin.execute('DELETE FROM capability_mcp_revisions')
    async with sessions.begin() as db:
        with pytest.raises(MCPDefinitionError):
            await ready_mcp_revisions(db,[installed])
        with pytest.raises(MCPDefinitionError):
            await update_mcp(db,actor_user_id='u',installation_id=installed.id,name='new-name')
