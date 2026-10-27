"""Resource visibility policy and page use cases, using the caller's transaction."""
from workmate_product.application.knowledge_access import KnowledgeAccess
from workmate_product.persistence.models import CapabilityType
from workmate_product.persistence.repositories import resource_lists as repository


async def managed_scopes(db, access, user_id, permission):
    ids = await repository.nonpersonal_scope_ids(db)
    matrix = await access.permissions_for_scopes(user_id, ids)
    return {sid for sid, permissions in matrix.items() if permission in permissions}


async def agents(db, access, user_id, *, page=1, page_size=12, q='', scope='all', category='all'):
    managed = await managed_scopes(db, access, user_id, 'agent.update')
    shared = await access.shared_agent_ids(user_id)
    return await repository.agents(db, user_id, managed=managed, shared=shared,
        page=page, page_size=page_size, q=q, scope=scope, category=category)


async def capabilities(db, access, user_id, kind, *, page=1, page_size=12, q='', source='all'):
    permission = 'skill.manage' if kind == CapabilityType.SKILL else 'mcp.manage'
    managed = await managed_scopes(db, access, user_id, permission)
    usable = await access.accessible_capability_ids(user_id)
    items, total = await repository.capabilities(db, kind, managed=managed, usable=usable,
        page=page, page_size=page_size, q=q, source=source)
    return [(item, item.scope_id in managed, item.id in usable) for item in items], total


async def knowledge(db, access, user_id, *, page=1, page_size=12, q='', view='all', allowed=None):
    managed = await managed_scopes(db, access, user_id, 'knowledge.manage')
    usable = await KnowledgeAccess(db).accessible_ids(user_id)
    return await repository.knowledge(db, user_id, managed=managed, usable=usable,
        page=page, page_size=page_size, q=q, view=view, allowed=allowed)
