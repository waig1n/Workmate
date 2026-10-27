"""Read-only preparation display follows current execution without changing Task status."""
import pytest
from workmate_product.persistence.models import Agent, Conversation, RuntimeSession, RuntimeTaskExecution, Scope, Task, User
from app.services.task_presentation import present_task, present_tasks
from app.api.routers.tasks import get_task, list_tasks


@pytest.mark.parametrize('execution_status,epoch,task_status,expected', [
    ('accepted', 1, 'created', 'queued'),
    ('preparing', 1, 'created', 'preparing'),
    ('running', 1, 'created', 'running'),
    ('preparing', 2, 'created', 'queued'),
    ('preparing', 1, 'cancelling', None),
    ('preparing', 1, 'completed', None),
])
async def test_phase_uses_current_execution_and_terminal_status_wins(db_session, execution_status, epoch, task_status, expected):
    db = db_session
    db.add(User(id='u', email='phase@example.invalid', password_hash='x', display_name='Phase'))
    db.add(Scope(id='scope', kind='organization', name='Phase', normalized_name='phase'))
    await db.flush()
    db.add(Agent(id='a', slug='phase', display_name='Phase', scope_id='scope'))
    await db.flush()
    db.add(Conversation(id='c', user_id='u', agent_id='a'))
    await db.flush()
    task = Task(id='t', conversation_id='c', title='Phase', status=task_status)
    queued = Task(id='q', conversation_id='c', title='Next', status='created')
    db.add_all([task, queued])
    await db.flush()
    db.add(RuntimeSession(id='s', conversation_id='c', storage_node_id='node', active_task_id='t', owner_id='worker', execution_epoch=1, status=execution_status))
    await db.flush()
    db.add(RuntimeTaskExecution(task_id='t', session_id='s', execution_epoch=epoch, status=execution_status))
    await db.commit()
    result = await present_tasks(db, [task, queued])
    assert result[0].execution_phase == expected
    assert result[0].status == task_status
    assert result[1].execution_phase == 'queued'
    assert await present_task(db, task) == result[0]
    user = await db.get(User, 'u')
    assert await get_task('t', user=user, db=db) == result[0]
    listed = await list_tasks(conversation_id='c', user=user, db=db, limit=200, offset=0)
    assert {item.id: item.execution_phase for item in listed} == {'t': expected, 'q': 'queued'}
