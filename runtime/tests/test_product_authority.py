"""Product bridge use cases preserve transaction ownership across refactors."""
import pytest
from test_execution_context import database, claim, record
from workmate_runtime.execution import product_authority
from workmate_runtime.transport.product_pool import ProductWorkerPool


async def test_failure_report_rolls_back_all_facts_if_reporting_does_not_finish(database, monkeypatch):
    sessions, admin = database
    await claim(admin)
    original = product_authority.quarantine_execution
    async def fail_after_projection(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('report failed after staging facts')
    monkeypatch.setattr(product_authority, 'quarantine_execution', fail_after_projection)
    with pytest.raises(RuntimeError, match='report failed'):
        await product_authority.report_worker_failure(sessions, command_id='cmd', session_id='s',
            task_id='t', worker_id='worker', storage_node_id='node', error_type='RuntimeError')
    assert await admin.fetchval('SELECT status FROM tasks') == 'created'
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 0
    assert await admin.fetchval('SELECT count(*) FROM task_events') == 0
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 't'


async def test_resume_reads_uncommitted_authority_without_committing_caller(database):
    sessions, admin = database
    await record(sessions)
    await claim(admin)
    pool = ProductWorkerPool(sessions.kw['bind'])
    with pytest.raises(RuntimeError, match='rollback outer'):
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.fetchrow("SELECT * FROM runtime_sessions WHERE id='s' FOR UPDATE")
                assert await product_authority.authorize_resume(connection, 'u', 'a', 't',
                    storage_node_id='node', knowledge_enabled=False) is True
                await connection.execute("UPDATE users SET status='disabled' WHERE id='u'")
                assert await product_authority.authorize_resume(connection, 'u', 'a', 't',
                    storage_node_id='node', knowledge_enabled=False) is False
                assert await product_authority.authorize_resume(connection, 'u', 'a', 'missing',
                    storage_node_id='node', knowledge_enabled=False) is False
                raise RuntimeError('rollback outer')
    assert await admin.fetchval("SELECT status FROM users WHERE id='u'") == 'active'


async def test_permission_evidence_rejection_does_not_rollback_transport_claim(database, monkeypatch):
    from workmate_product.application.commands import CommandRejected
    sessions, admin = database
    await record(sessions)
    await claim(admin)
    async def reject(*args, **kwargs):
        assert kwargs['allow_pending_recovery'] is True
        raise CommandRejected('historical permission evidence is inconsistent')
    monkeypatch.setattr(product_authority, 'read_execution_context', reject)
    async with ProductWorkerPool(sessions.kw['bind']).acquire() as connection:
        async with connection.transaction():
            await connection.fetchrow("SELECT * FROM runtime_sessions WHERE id='s' FOR UPDATE")
            assert await product_authority.authorize_resume(connection,'u','a','t',
                storage_node_id='node',knowledge_enabled=False) is False
            assert await connection.fetchval("SELECT owner_id FROM runtime_sessions WHERE id='s'") == 'worker'
