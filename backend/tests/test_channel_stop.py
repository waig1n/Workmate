"""Channel /stop joins inbox/outbox caller transaction, including cancellation facts."""
import pytest

from workmate_product.application.task_controls import TaskControls
from workmate_product.persistence.models import Conversation, Task, User
from test_durable_projection import projection_database  # noqa: F401
from test_control_commands import parked


@pytest.mark.parametrize('admitted', [False, True])
async def test_stage_stop_rollback_then_commit_preserves_atomicity(projection_database, admitted):
    sessions, admin = projection_database
    task_id = 't' if admitted else 'queued'
    if admitted:
        await parked(sessions, admin)
    else:
        async with sessions.begin() as db:
            db.add(Task(id=task_id, conversation_id='c', title='Queued'))
    original = await admin.fetchval('SELECT status FROM tasks WHERE id=$1', task_id)
    original_audit = await admin.fetchval('SELECT count(*) FROM task_events')
    original_events = await admin.fetchval('SELECT count(*) FROM product_events')
    controls = TaskControls()
    async with sessions() as db:
        user = await db.get(User, 'u')
        conversation = await db.get(Conversation, 'c')
        task = await db.get(Task, task_id)
        await controls.stage_stop(db, user, conversation, task)
        assert db.in_transaction()
        # Separate DB connection cannot observe any uncommitted stop fact.
        assert await admin.fetchval('SELECT status FROM tasks WHERE id=$1', task_id) == original
        await db.rollback()
    assert await admin.fetchval('SELECT status FROM tasks WHERE id=$1', task_id) == original
    assert await admin.fetchval('SELECT count(*) FROM task_events') == original_audit
    assert await admin.fetchval('SELECT count(*) FROM product_events') == original_events
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 0
    async with sessions.begin() as db:
        await controls.stage_stop(db, await db.get(User, 'u'), await db.get(Conversation, 'c'),
                                  await db.get(Task, task_id))
    assert await admin.fetchval('SELECT status FROM tasks WHERE id=$1', task_id) == ('cancelling' if admitted else 'cancelled')
    assert await admin.fetchval('SELECT count(*) FROM task_events') > original_audit
    assert await admin.fetchval('SELECT count(*) FROM product_events') > original_events
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == int(admitted)
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 't'
