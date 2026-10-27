"""Governed MCP definition mutations, ready pointers and private version storage."""
import uuid
from sqlalchemy import select
from .definitions import MCPDefinitionError,RenderedMCP,render_mcp
from .secret_store import PostgresMCPSecretStore
from ..application.authorization import AccessDecisionService
from ..application.execution_context import _fresh_identity_map
from ..persistence.models import (AgentCapabilityBinding,CapabilityInstallation,CapabilityMCPRevision,
    MCPSecretRevision,Scope,User,SecurityAuditEvent)


async def _managed_scope(db,actor_user_id,scope_id):
    root=await db.scalar(select(Scope).where(Scope.kind=='organization',Scope.parent_id.is_(None)))
    if root is None:
        raise MCPDefinitionError('Organization scope not found',404)
    if scope_id is not None and scope_id!=root.id:
        raise MCPDefinitionError('MCP connections must be installed in the organization scope',422)
    scope_id=root.id
    scope=await db.scalar(select(Scope).where(Scope.id==scope_id).with_for_update())
    actor=await db.get(User,actor_user_id)
    if scope is None or actor is None or actor.status!='active':
        raise MCPDefinitionError('MCP scope not found',404)
    if scope.kind=='personal':
        raise MCPDefinitionError('Capabilities cannot be installed in Personal scope',422)
    access=AccessDecisionService(db)
    if not await access.has_permission(actor_user_id,scope.id,'mcp.manage'):
        visible=scope.id in await access.accessible_capability_scope_ids(actor_user_id)
        raise MCPDefinitionError('Permission required in this scope: mcp.manage',403 if visible else 404)
    return scope


async def _installation(db,actor_user_id,installation_id):
    scope_id=await db.scalar(select(CapabilityInstallation.scope_id).where(
        CapabilityInstallation.id==installation_id,CapabilityInstallation.capability_type=='mcp'))
    scope=await _managed_scope(db,actor_user_id,scope_id)
    installation=await db.scalar(select(CapabilityInstallation).where(
        CapabilityInstallation.id==installation_id,CapabilityInstallation.capability_type=='mcp').with_for_update())
    if installation is None or installation.scope_id!=scope.id:
        raise MCPDefinitionError('MCP not found',404)
    return installation


async def _available_name(db,scope_id,name,excluding_id=None):
    query=select(CapabilityInstallation.id).where(
        CapabilityInstallation.scope_id==scope_id,CapabilityInstallation.capability_type=='mcp',
        CapabilityInstallation.name==name)
    if excluding_id is not None:
        query=query.where(CapabilityInstallation.id!=excluding_id)
    conflict=await db.scalar(query.limit(1))
    if conflict:
        raise MCPDefinitionError('MCP name already installed in this scope',409)


def _audit(db,event,actor_user_id,installation,**details):
    db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type=event,actor_user_id=actor_user_id,
        scope_id=installation.scope_id,metadata_={'capability_id':installation.id,'capability_type':'mcp',**details}))


async def _new_revision(db,installation,rendered,card,store):
    version=str(uuid.uuid4())
    await store.put(db,capability_id=installation.id,scope_id=installation.scope_id,
                    secret_version=version,rendered=rendered,card=card)
    pointer=await db.get(CapabilityMCPRevision,installation.id)
    if pointer is None:
        db.add(CapabilityMCPRevision(capability_id=installation.id,secret_version=version))
    else:
        pointer.secret_version=version
    return version


@_fresh_identity_map
async def authorize_mcp_install(db,*,actor_user_id,scope_id):
    return (await _managed_scope(db,actor_user_id,scope_id)).id


@_fresh_identity_map
async def authorize_mcp_update(db, *, actor_user_id, installation_id):
    """Return only public source identity after current management authorization."""
    installed = await _installation(db, actor_user_id, installation_id)
    return installed.metadata_.get('hub_id'), installed.metadata_.get('card_id')


@_fresh_identity_map
async def managed_mcp_definition(db, *, actor_user_id, installation_id, secret_store=None):
    """Read private data only after current source management authorization."""
    installed = await _installation(db, actor_user_id, installation_id)
    pointer = await db.get(CapabilityMCPRevision, installed.id)
    if pointer is None:
        raise MCPDefinitionError('MCP configuration is not ready', 409)
    private = await (secret_store or PostgresMCPSecretStore()).get(db,
        capability_id=installed.id, scope_id=installed.scope_id, secret_version=pointer.secret_version)
    return installed, private


@_fresh_identity_map
async def install_mcp(db,*,actor_user_id,scope_id,card,values,name=None,secret_store=None):
    """Ready means a validated durable definition, matching the current install API.

    Remote connection/session initialization occurs at execution time, as before.
    No private value is put in installation metadata, audit, or an execution spec.
    Caller commits once; no network, subprocess or internal commit occurs here.
    """
    scope=await _managed_scope(db,actor_user_id,scope_id)
    rendered=render_mcp(card,values,name=name)
    await _available_name(db,scope.id,rendered.client.name)
    if card.display_name is not None and len(card.display_name)>200:
        raise MCPDefinitionError('MCP display name exceeds 200 characters',422)
    identifier=str(uuid.uuid4())
    installed=CapabilityInstallation(id=identifier,capability_type='mcp',scope_id=scope.id,
        name=rendered.client.name,display_name=card.display_name,enabled=True,created_by=actor_user_id,
        metadata_={'description':card.description,'tags':card.tags,'hub_id':card.hub_id,'card_id':card.id,
                   'version':card.version,'is_stateful':card.is_stateful})
    db.add(installed)
    await db.flush()
    version=await _new_revision(db,installed,rendered,card,secret_store or PostgresMCPSecretStore())
    _audit(db,'capability.install',actor_user_id,installed,secret_version=version)
    await db.flush()
    return installed


@_fresh_identity_map
async def update_mcp(db,*,actor_user_id,installation_id,card=None,values=None,name=None,enabled=None,secret_store=None):
    installed=await _installation(db,actor_user_id,installation_id)
    if enabled is False and await db.scalar(select(AgentCapabilityBinding.agent_id).where(
            AgentCapabilityBinding.capability_installation_id==installation_id).limit(1)):
        raise MCPDefinitionError('Capability is bound to agents; remove the bindings before disabling',409)
    store=secret_store or PostgresMCPSecretStore()
    pointer=await db.get(CapabilityMCPRevision,installed.id)
    if pointer is None:
        raise MCPDefinitionError('MCP configuration is not ready',409)
    previous=await store.get(db,capability_id=installed.id,scope_id=installed.scope_id,secret_version=pointer.secret_version)
    if values is not None:
        # Rotate with the saved template even when the public Hub is disabled.
        card = card or previous.card
        if card.hub_id!=previous.card.hub_id or card.id!=previous.card.id:
            raise MCPDefinitionError('MCP source card is required for a credential update',400)
        rendered=render_mcp(card,{**previous.values,**values},name=name or installed.name)
    else:
        card=previous.card
        client=previous.client.model_copy(deep=True)
        if name is not None:
            # Revalidate assignment; Pydantic model_copy does not validate updates.
            from .definitions import MCPConfiguration
            try:
                client=MCPConfiguration.model_validate({**client.model_dump(mode='json'),'name':name})
            except Exception:
                raise MCPDefinitionError('Invalid MCP name',422) from None
        rendered=RenderedMCP(client,previous.values)
    if rendered.client.name != installed.name:
        await _available_name(db,installed.scope_id,rendered.client.name,installed.id)
    installed.name=rendered.client.name
    if enabled is not None:
        installed.enabled=enabled
    if values is not None or name is not None:
        version=await _new_revision(db,installed,rendered,card,store)
        installed.metadata_={**installed.metadata_,'version':card.version,'is_stateful':card.is_stateful,
                             'description':card.description}
        installed.display_name=card.display_name
    else:
        version=pointer.secret_version
    _audit(db,'capability.update',actor_user_id,installed,secret_version=version,
           values_updated=values is not None,name_updated=name is not None,enabled=installed.enabled)
    await db.flush()
    return installed


@_fresh_identity_map
async def uninstall_mcp(db,*,actor_user_id,installation_id):
    installed=await _installation(db,actor_user_id,installation_id)
    if await db.scalar(select(AgentCapabilityBinding.agent_id).where(
            AgentCapabilityBinding.capability_installation_id==installation_id).limit(1)):
        raise MCPDefinitionError('Capability is bound to agents; remove the bindings first',409)
    _audit(db,'capability.delete',actor_user_id,installed)
    await db.delete(installed)
    await db.flush()


async def ready_mcp_revisions(db,capabilities):
    identifiers={c.id for c in capabilities if c.capability_type=='mcp'}
    if not identifiers:
        return {}
    rows=(await db.execute(select(CapabilityMCPRevision.capability_id,CapabilityMCPRevision.secret_version)
        .join(MCPSecretRevision,MCPSecretRevision.id==CapabilityMCPRevision.secret_version)
        .where(CapabilityMCPRevision.capability_id.in_(identifiers),
               MCPSecretRevision.capability_id==CapabilityMCPRevision.capability_id))).all()
    result=dict(rows)
    if set(result)!=identifiers:
        raise MCPDefinitionError('A bound MCP has no ready configuration revision',409)
    return result


@_fresh_identity_map
async def load_execution_mcp_definitions(db,*,session_id,task_id,worker_id,execution_epoch,
                                         storage_node_id,knowledge_enabled=False,secret_store=None):
    """Resolve secrets only after current owner/fence/lease and Product authority.

    The version comes from the immutable trusted Task spec, never tool arguments
    or a browser-supplied digest. Holding these locks prevents an owner change
    during the private read; subsequent invocation still needs the Worker guard.
    """
    from ..application.execution_context import read_execution_context
    context=await read_execution_context(db,session_id=session_id,task_id=task_id,worker_id=worker_id,
        execution_epoch=execution_epoch,storage_node_id=storage_node_id,knowledge_enabled=knowledge_enabled)
    capabilities={c.id:c for c in context.capabilities if c.capability_type=='mcp'}
    store=secret_store or PostgresMCPSecretStore()
    definitions=[]
    for ref in context.initial_spec.mcp_configurations:
        cap=capabilities.get(ref.capability_id)
        if cap is None:
            raise MCPDefinitionError('MCP execution capability is unavailable',409)
        definitions.append(await store.get(db,capability_id=cap.id,scope_id=cap.scope_id,secret_version=ref.secret_version))
    return context,tuple(definitions)
