"""Native management and execution keep separate resource grants."""
import uuid

import pytest
from sqlalchemy import select, delete

from app.config_file import KnowledgeFileConfig
from workmate_product.persistence.models import (
    Agent,
    ResourceShare,
    Scope,
    ScopeKind,
    ScopeMembership,
)
from workmate_product.persistence.models import KnowledgeBase
from app.services.bootstrap import create_scope
from workmate_product.application.authorization import AccessDecisionService
from workmate_product.application.knowledge_access import KnowledgeBindingError, validate_knowledge_bindings
from test_native_agents import native_agents, projection_database  # noqa: F401


async def test_native_manager_binding_does_not_grant_retrieval(native_agents, monkeypatch):
    client, sessions, _ = native_agents
    monkeypatch.setattr('app.api.routers.agents.knowledge_settings',
                        lambda: KnowledgeFileConfig(enabled=True))
    async with sessions.begin() as db:
        root = await db.get(Scope, 'scope')
        target = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Native target', parent=root)
        kb = KnowledgeBase(id=str(uuid.uuid4()), scope_id=root.id, name='Native grant',
                           runtime_id='native-grant', runtime_owner_id='scope:' + root.id)
        db.add(kb)
        await db.flush()
        db.add(ResourceShare(id=str(uuid.uuid4()), knowledge_base_id=kb.id, target_scope_id=target.id))
        kb_id, target_id = kb.id, target.id
    created = await client.post('/api/agents', json={
        'slug': 'native-scope-grant', 'display_name': 'Native scope grant',
        'scope_id': target_id, 'knowledge_config': {'knowledge_base_ids': [kb_id]},
    })
    assert created.status_code == 201, created.text
    async with sessions() as db:
        agent = await db.get(Agent, created.json()['id'])
        assert agent.knowledge_config['knowledge_base_ids'] == [kb_id]
        with pytest.raises(KnowledgeBindingError):
            await validate_knowledge_bindings(db, 'u', agent, agent.knowledge_config,
                                             enabled=True, access=AccessDecisionService(db))
        db.add(ScopeMembership(scope_id=target_id, user_id='u'))
        await db.commit()
        rows = await validate_knowledge_bindings(db, 'u', agent, agent.knowledge_config,
                                                enabled=True, access=AccessDecisionService(db))
        assert [row.id for row in rows] == [kb_id]
        await db.execute(delete(ResourceShare).where(ResourceShare.knowledge_base_id == kb_id))
        await db.commit()
        with pytest.raises(KnowledgeBindingError):
            await validate_knowledge_bindings(db, 'u', agent, agent.knowledge_config,
                                             enabled=True, access=AccessDecisionService(db))


async def test_native_privatization_revokes_explicit_usage(native_agents):
    client, sessions, _ = native_agents
    created = await client.post('/api/agents', json={
        'slug': 'native-private-grant', 'display_name': 'Native grant',
        'scope_id': 'scope', 'visibility': 'public',
    })
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    async with sessions.begin() as db:
        db.add(ResourceShare(id=str(uuid.uuid4()), agent_id=identifier, target_scope_id='scope',
                             include_descendants=True))
    changed = await client.patch('/api/agents/' + identifier, json={'visibility': 'private'})
    assert changed.status_code == 200, changed.text
    async with sessions() as db:
        assert await db.scalar(select(ResourceShare.id).where(ResourceShare.agent_id == identifier)) is None
