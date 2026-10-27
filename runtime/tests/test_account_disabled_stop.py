"""Administrator Stop uses a Product admission receipt across every boundary."""
import json

import pytest

from test_postgres_execution_store import database, claim
from test_preparation_stop import stop as preparation_intent
from test_parked_stop import stop_intent
from test_resume_controller import parked
from test_terminal_stop import terminal, admit
from workmate_runtime.transport.postgres import ExecutionStore
from workmate_runtime.transport.stopping import StopStore
from workmate_runtime.transport.stopping_preparation import PreparationStopStore
from workmate_runtime.transport.running import RunningStopStore
from workmate_runtime.transport.terminal_stop import TerminalStopStore


@pytest.mark.parametrize('boundary', ['preparation', 'running', 'parked', 'terminal'])
@pytest.mark.parametrize('receipt', ['valid', 'missing', 'wrong_actor', 'wrong_target', 'wrong_command', 'wrong_task', 'wrong_conversation', 'wrong_reason', 'wrong_event', 'ordinary', 'self_disabled'])
async def test_admin_stop_requires_exact_admission_receipt(database, boundary, receipt):
    pool, db = database
    if boundary == 'preparation':
        await preparation_intent(db)
    elif boundary == 'running':
        owner = await claim(pool)
        await ExecutionStore(db).begin_reply(owner, 'reply', {'type': 'REPLY_START'})
        await stop_intent(db)
    elif boundary == 'parked':
        await parked(pool, db)
        await stop_intent(db)
    else:
        await terminal(pool)
        await admit(db)
    await db.execute("INSERT INTO users VALUES ('administrator','disabled')")
    actor = 'user' if receipt == 'self_disabled' else 'administrator'
    # Authorization is a durable admission fact: later role/status changes must
    # not revive execution by preventing an already accepted Stop from draining.
    await db.execute("""UPDATE runtime_commands SET actor_user_id=$2,
        payload=$1::jsonb WHERE id='stop'""", json.dumps({
            'reason': 'user_requested' if receipt == 'ordinary' else 'account_disabled',
        }), actor)
    await db.execute('''CREATE TABLE IF NOT EXISTS task_events (
        task_id text, event_type text, payload json)''')
    await db.execute('ALTER TABLE task_events ADD COLUMN conversation_id text')
    evidence = dict(commandId='stop', actorUserId=actor, targetUserId='user', reason='account_disabled')
    task, conversation = 'task', 'conversation'
    if receipt == 'wrong_actor':
        evidence['actorUserId'] = 'another-admin'
    elif receipt == 'wrong_target':
        evidence['targetUserId'] = 'another-user'
    elif receipt == 'wrong_command':
        evidence['commandId'] = 'another-stop'
    elif receipt == 'wrong_task':
        task = 'other-task'
    elif receipt == 'wrong_conversation':
        conversation = 'other-conversation'
    elif receipt == 'wrong_reason':
        evidence['reason'] = 'user_requested'
    elif receipt == 'self_disabled':
        await db.execute("UPDATE users SET status='disabled' WHERE id='user'")
    if receipt != 'missing':
        await db.execute('INSERT INTO task_events(task_id,conversation_id,event_type,payload) VALUES ($1,$2,$3,$4::json)',
            task, conversation, 'other_event' if receipt == 'wrong_event' else 'task_cancel_requested', json.dumps(evidence))
    if boundary == 'preparation':
        result = await PreparationStopStore(db).claim('stop', 'worker', 'node', lease_seconds=30)
    elif boundary == 'running':
        result = await RunningStopStore(db).claim_pending(owner, 'command', 'node')
    elif boundary == 'parked':
        result = await StopStore(db).claim_parked('stop', 'worker', 'node')
    else:
        result = await TerminalStopStore(db).settle('stop', 'settler', 'node')
    assert bool(result) is (receipt in {'valid', 'self_disabled'})
    if receipt not in {'valid', 'self_disabled'}:
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'") == 'accepted'
