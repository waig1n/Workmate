"""Catalog projections share the caller transaction and do not issue per-card reads."""
import pytest
from sqlalchemy import event, select

from workmate_product.persistence.models import CapabilityInstallation, Scope


@pytest.mark.asyncio
async def test_catalog_projection_flushes_current_changes_but_never_commits(db_session):
    from workmate_product.persistence.repositories.catalog_status import installed_flags
    db = db_session
    db.add(Scope(id='catalog-org', kind='organization', name='Catalog'))
    await db.flush()
    db.add(CapabilityInstallation(id='catalog-installation', scope_id='catalog-org',
        capability_type='skill', name='renamed', metadata_={'hub_id': 'hub', 'card_id': 'one'}))
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = db.bind.sync_engine
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        flags = await installed_flags(db, scope_id='catalog-org', kind='skill', user_id='actor',
            can_manage=True, hub_id='hub', cards=[{'id':'one','name':'original'}, {'id':'two','name':'new'}])
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert flags == [True, False]
    assert sum(sql.startswith('SELECT EXISTS') for sql in statements) == 1
    assert sum(sql.startswith('INSERT INTO capability_installations') for sql in statements) == 1
    assert db.in_transaction()
    await db.rollback()
    assert await db.scalar(select(CapabilityInstallation.id).where(CapabilityInstallation.id=='catalog-installation')) is None


@pytest.mark.asyncio
async def test_catalog_application_uses_domain_error_for_invalid_scope(db_session):
    from workmate_product.application.catalog_status import annotate, CatalogAccessError
    class NoAuthorization:
        async def has_permission(self, *args):
            raise AssertionError('Missing scope must fail before permission lookup')
    with pytest.raises(CatalogAccessError) as error:
        await annotate(db_session, NoAuthorization(), 'actor', {'cards': []}, 'hub', 'mcp', scope_id='missing')
    assert error.value.status_code == 404
