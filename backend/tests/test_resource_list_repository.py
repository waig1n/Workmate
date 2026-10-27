"""Resource pages retain bounded SQL and use caller-owned transaction state."""
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from workmate_product.application import resource_lists
from workmate_product.persistence.repositories import resource_lists as repository


def database(items=()):
    db = Mock()
    db.scalar = AsyncMock(return_value=3)
    db.scalars = AsyncMock(return_value=Mock(all=Mock(return_value=list(items))))
    return db


async def test_agent_page_has_count_and_page_only_and_never_owns_transaction():
    db = database()
    assert await repository.agents(db, 'reader', managed={'team'}, shared={'shared'},
        page=2, page_size=2, q='100%_') == ([], 3)
    assert db.scalar.await_count == db.scalars.await_count == 1
    count = db.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
    page = db.scalars.call_args.args[0].compile(dialect=postgresql.dialect())
    assert 'LIMIT' not in str(count) and 'OFFSET' not in str(count)
    assert 'ORDER BY agents.created_at DESC, agents.id' in str(page)
    assert list(page.params.values())[-2:] == [2, 2]
    assert '%100\\%\\_%' in page.params.values()
    assert 'reader' in page.params.values()
    for method in ('begin', 'commit', 'rollback', 'flush'):
        getattr(db, method).assert_not_called()


async def test_application_resolves_permissions_before_capability_page(monkeypatch):
    db = database()
    access = Mock()
    access.permissions_for_scopes = AsyncMock(return_value={'team': {'skill.manage'}, 'other': set()})
    access.accessible_capability_ids = AsyncMock(return_value={'usable'})
    monkeypatch.setattr(repository, 'nonpersonal_scope_ids', AsyncMock(return_value={'team', 'other'}))
    page = AsyncMock(return_value=([Mock(id='usable', scope_id='other'), Mock(id='owned', scope_id='team')], 2))
    monkeypatch.setattr(repository, 'capabilities', page)
    items, total = await resource_lists.capabilities(db, access, 'reader', 'skill', page=2, page_size=1)
    assert total == 2
    assert [(manage, use) for _, manage, use in items] == [(False, True), (True, False)]
    assert page.call_args.kwargs['managed'] == {'team'}
    assert page.call_args.kwargs['usable'] == {'usable'}
    assert page.call_args.kwargs['page'] == 2
    access.permissions_for_scopes.assert_awaited_once_with('reader', {'team', 'other'})


@pytest.mark.parametrize('resource', ['capabilities', 'knowledge'])
async def test_other_catalogue_pages_keep_two_queries_with_visibility_before_limit(resource):
    db = database()
    if resource == 'capabilities':
        await repository.capabilities(db, 'skill', managed={'team'}, usable={'shared'},
            page=3, page_size=4, source='uploaded')
    else:
        await repository.knowledge(db, 'reader', managed={'team'}, usable={'shared'},
            page=3, page_size=4, allowed={'shared'}, view='shared')
    assert db.scalar.await_count == db.scalars.await_count == 1
    count = db.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
    page = db.scalars.call_args.args[0].compile(dialect=postgresql.dialect())
    assert 'LIMIT' not in str(count)
    assert list(page.params.values())[-2:] == [4, 8]
    assert ['shared'] in page.params.values()
    db.commit.assert_not_called()
    db.flush.assert_not_called()


async def test_repository_preserves_caller_autoflush_and_rollback(db_session):
    from sqlalchemy import select
    from workmate_product.persistence.models import Agent, Scope, ScopeKind

    db_session.add(Scope(id='list-scope', name='List scope', kind=ScopeKind.ORGANIZATION))
    await db_session.flush()
    pending = Agent(id='pending-page-agent', slug='pending-page-agent',
        display_name='Pending', scope_id='list-scope')
    db_session.add(pending)
    items, total = await repository.agents(db_session, 'reader', managed={'list-scope'}, shared=set())
    assert total == 1 and items == [pending]
    # Reads still flush the caller's pending ORM changes, without committing them.
    await db_session.rollback()
    assert await db_session.scalar(select(Agent).where(Agent.id == pending.id)) is None


async def test_application_knowledge_preserves_binding_restriction_and_use_grants(monkeypatch):
    db = database()
    access = Mock(permissions_for_scopes=AsyncMock(return_value={'team': {'knowledge.manage'}}))
    monkeypatch.setattr(repository, 'nonpersonal_scope_ids', AsyncMock(return_value={'team'}))
    knowledge_access = Mock(accessible_ids=AsyncMock(return_value={'usable'}))
    monkeypatch.setattr(resource_lists, 'KnowledgeAccess', Mock(return_value=knowledge_access))
    page = AsyncMock(return_value=([], 0))
    monkeypatch.setattr(repository, 'knowledge', page)
    assert await resource_lists.knowledge(db, access, 'reader', allowed=set(), view='shared') == ([], 0)
    assert page.call_args.kwargs['allowed'] == set()
    assert page.call_args.kwargs['managed'] == {'team'}
    assert page.call_args.kwargs['usable'] == {'usable'}
    knowledge_access.accessible_ids.assert_awaited_once_with('reader')
