"""Account disable must stop native execution without an App HTTP client."""
import pytest
from sqlalchemy import select

from workmate_product.persistence.models import User, SecurityAuditEvent, ScopeClosure, Task
from app.services.bootstrap import ensure_bootstrap, ensure_user_scopes
from app.services.product_runtime import ProductRuntime
from app.services.user_management import UserManagementService
from test_durable_projection import projection_database


async def prepare(sessions, admin, *, self_disable=False, status='running'):
    async with sessions() as db:
        db.add(ScopeClosure(ancestor_id='scope', descendant_id='scope', depth=0))
        await db.flush()
        await ensure_bootstrap(db)
        await ensure_user_scopes(db, await db.get(User, 'u'), first_user=True)
        other = User(id='other', email='other@example.invalid', password_hash='x', display_name='Other')
        db.add(other)
        await db.flush()
        # Keep another administrator so self-disable is a valid product action.
        await ensure_user_scopes(db, other, first_user=True)
    target = 'u' if self_disable else 'other'
    await admin.execute("UPDATE conversations SET user_id=$1", target)
    await admin.execute("UPDATE tasks SET status=$1", status)
    if status == 'created':
        await admin.execute("UPDATE runtime_sessions SET active_task_id=NULL,status='accepted',execution_epoch=0,reply_id=NULL")
        await admin.execute("UPDATE runtime_task_executions SET status='accepted',execution_epoch=0,reply_id=NULL")
    return target


@pytest.mark.parametrize('status', ['created', 'running', 'awaiting_permission'])
@pytest.mark.parametrize('self_disable', [False, True])
async def test_disable_persists_stop_with_real_actor_and_retains_gates(
    projection_database, status, self_disable,
):
    sessions, admin = projection_database
    target = await prepare(sessions, admin, status=status, self_disable=self_disable)
    runtime = ProductRuntime(sessions, None)
    async with sessions() as db:
        service = UserManagementService(db, runtime, actor_id='u')
        actor, user = await db.get(User, 'u'), await db.get(User, target)
        await service.disable_user(actor=actor, user=user)
    async with sessions() as db:
        retry_actor = 'other' if self_disable else 'u'
        service = UserManagementService(db, runtime, actor_id=retry_actor)
        actor, user = await db.get(User, retry_actor), await db.get(User, target)
        await service.disable_user(actor=actor, user=user)
    assert await admin.fetchval("SELECT status FROM users WHERE id=$1", target) == 'disabled'
    row = await admin.fetchrow("SELECT actor_user_id,kind FROM runtime_commands WHERE task_id='t'")
    assert row is not None, 'Disabled account still has an active execution but no durable Stop'
    assert dict(row) == {'actor_user_id': 'u', 'kind': 'StopTask'}
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE task_id='t'") == 1
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == 'cancelling'
    assert await admin.fetchval("SELECT active_product_task_id FROM conversations WHERE id='c'") == 't'
    assert await admin.fetchval("SELECT active_task_id FROM runtime_sessions WHERE id='s'") == (None if status == 'created' else 't')
    async with sessions() as db:
        audit = await db.scalar(select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == 'user.disable'))
        assert audit.actor_user_id == 'u' and audit.target_user_id == target


async def test_failed_stop_admission_does_not_undo_disable(projection_database):
    sessions, admin = projection_database
    target = await prepare(sessions, admin)
    class Unavailable:
        async def stop_disabled_user_tasks(self, *args, **kwargs):
            raise RuntimeError('control admission unavailable')
    async with sessions() as db:
        await UserManagementService(db, Unavailable(), actor_id='u').disable_user(
            actor=await db.get(User, 'u'), user=await db.get(User, target))
    assert await admin.fetchval("SELECT status FROM users WHERE id=$1", target) == 'disabled'
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == 'running'


async def test_disable_cancels_only_unadmitted_queue_directly(projection_database):
    sessions, admin = projection_database
    target = await prepare(sessions, admin)
    async with sessions.begin() as db:
        db.add(Task(id='queued', conversation_id='c', title='Next task'))
    async with sessions() as db:
        await UserManagementService(db, ProductRuntime(sessions, None), actor_id='u').disable_user(
            actor=await db.get(User, 'u'), user=await db.get(User, target))
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='queued'") == 'cancelled'
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == 'cancelling'
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE task_id='queued'") == 0


async def test_admin_stop_requires_current_disable_receipt(projection_database):
    from workmate_contracts import Command
    from workmate_product.application.commands import CommandRejected
    from workmate_product.application.control_commands import accept_disabled_user_stop
    sessions, admin = projection_database
    target = await prepare(sessions, admin)
    await admin.execute("UPDATE users SET status='disabled',disabled_by='u',disabled_at=now() WHERE id=$1", target)
    command = Command(command_id='forged', kind='StopTask', conversation_id='c',
        runtime_session_id='s', product_task_id='t', payload={'reason':'account_disabled'})
    async with sessions.begin() as db:
        with pytest.raises(CommandRejected):
            await accept_disabled_user_stop(db, command, actor_user_id='u',
                target_user_id=target, disable_event_id='missing')
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 0


@pytest.mark.parametrize('mutation', ['timestamp', 'actor', 'target', 'event_type', 'reenabled'])
async def test_admin_stop_rejects_mismatched_disable_receipt(projection_database, mutation):
    from workmate_contracts import Command
    from workmate_product.application.commands import CommandRejected
    from workmate_product.application.control_commands import accept_disabled_user_stop
    sessions, admin = projection_database
    target = await prepare(sessions, admin)
    await admin.execute("UPDATE users SET status='disabled',disabled_by='u',disabled_at=now() WHERE id=$1", target)
    timestamp = await admin.fetchval('SELECT disabled_at FROM users WHERE id=$1', target)
    async with sessions.begin() as db:
        db.add(SecurityAuditEvent(id='receipt', event_type='user.enable' if mutation == 'event_type' else 'user.disable',
            actor_user_id=target if mutation == 'actor' else 'u',
            target_user_id='u' if mutation == 'target' else target,
            metadata_={'disabled_at':'2000-01-01T00:00:00' if mutation == 'timestamp' else timestamp.isoformat()}))
    if mutation == 'reenabled':
        await admin.execute("UPDATE users SET status='active' WHERE id=$1", target)
    command = Command(command_id='forged', kind='StopTask', conversation_id='c',
        runtime_session_id='s', product_task_id='t', payload={'reason':'account_disabled'})
    async with sessions.begin() as db:
        with pytest.raises(CommandRejected):
            await accept_disabled_user_stop(db, command, actor_user_id='u',
                target_user_id=target, disable_event_id='receipt')
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 0
