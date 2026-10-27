"""Durable dispatch protocol on isolated PostgreSQL; no Redis or tool execution."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from test_command_admission import database, command, allow_agent, create_task
from workmate_product.application.commands import accept_start
from workmate_product.persistence.repositories import command_deliveries as outbox

pytestmark = pytest.mark.asyncio


async def admit(sessions):
    async with sessions.begin() as db:
        await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                           authorize_agent=allow_agent, prepare_task=create_task)


async def test_admission_stages_outbox_atomically(database):
    sessions, admin = database
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                               authorize_agent=allow_agent, prepare_task=create_task)
            assert await db.scalar(text('SELECT count(*) FROM command_deliveries')) == 1
            assert await admin.fetchval('SELECT count(*) FROM command_deliveries') == 0
            raise RuntimeError('rollback')
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries') == 0
    await admit(sessions)
    async with sessions.begin() as db:
        await accept_start(db, command(), actor_user_id='u', storage_node_id='node',authorize_agent=allow_agent)
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries') == 1


async def test_publish_lease_competition_and_token_fence(database):
    sessions, admin = database
    await admit(sessions)
    async def lease():
        async with sessions.begin() as db:
            return await outbox.lease_due(db, limit=2, lease_seconds=30)
    batches = await asyncio.gather(lease(), lease())
    claims = [claim for batch in batches for claim in batch]
    assert len(claims) == 1
    old = claims[0]
    await admin.execute("UPDATE command_deliveries SET lease_until=now()-interval '1 second'")
    new = (await lease())[0]
    async with sessions.begin() as db:
        assert await outbox.published(db, old, '1-0') is False
        assert await outbox.published(db, new, '2-0') is True
    assert await admin.fetchval('SELECT stream_message_id FROM command_deliveries') == '2-0'


async def test_failed_publish_backoff_then_quarantine(database):
    sessions, admin = database
    await admit(sessions)
    async with sessions.begin() as db:
        claim = (await outbox.lease_due(db, limit=1, lease_seconds=30))[0]
        assert await outbox.failed(db, claim, 'redis_unavailable',
            next_attempt_at=datetime.now(timezone.utc)+timedelta(hours=1), max_attempts=2)
        assert await outbox.lease_due(db, limit=1, lease_seconds=30) == []
    await admin.execute('UPDATE command_deliveries SET next_attempt_at=now()')
    async with sessions.begin() as db:
        claim = (await outbox.lease_due(db, limit=1, lease_seconds=30))[0]
        assert await outbox.failed(db, claim, 'redis_unavailable',next_attempt_at=datetime.now(timezone.utc), max_attempts=2)
    assert await admin.fetchval('SELECT status FROM command_deliveries') == 'quarantined'
    assert await admin.fetchval('SELECT status FROM runtime_commands') == 'accepted'


async def test_owner_route_and_safe_reroute_preserve_command(database):
    sessions, admin = database
    await admit(sessions)
    await admin.execute("UPDATE runtime_sessions SET owner_id='old',lease_until=now()+interval '1 minute'")
    async with sessions.begin() as db:
        assert await outbox.reroute(db, 'cmd', expected_generation=1) == 2
    rows = await admin.fetch('SELECT * FROM command_deliveries ORDER BY delivery_generation')
    assert rows[0]['status'] == 'superseded'
    assert rows[1]['target_incarnation'] == 'old'
    await admin.execute("UPDATE runtime_commands SET status='claimed',claimed_by='old',claimed_epoch=1")
    async with sessions.begin() as db:
        assert await outbox.reroute(db, 'cmd', expected_generation=2) is None
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries') == 2


async def test_malformed_quarantine_is_durable_and_idempotent(database):
    sessions, admin = database
    async with sessions.begin() as db:
        await outbox.quarantine_message(db, 'stream', '1-0', 'invalid_envelope')
        await outbox.quarantine_message(db, 'stream', '1-0', 'invalid_envelope')
    assert await admin.fetchval('SELECT count(*) FROM command_delivery_quarantines') == 1


def envelope(generation=1, node='node', worker=None):
    from types import SimpleNamespace
    return SimpleNamespace(command_id='cmd',delivery_generation=generation,
                           storage_node_id=node,target_incarnation=worker)


async def test_classify_accepted_claimed_terminal_and_unknown(database):
    sessions, admin = database
    await admit(sessions)
    async with sessions.begin() as db:
        result = await outbox.classify(db,envelope(),worker_id='w',node_id='node')
        assert (result.action,result.command['task_id'],result.command['delivery_channel']) == ('execute','t','execute')
    await admin.execute("UPDATE runtime_commands SET status='claimed'")
    async with sessions.begin() as db:
        assert (await outbox.classify(db,envelope(),worker_id='w',node_id='node')).action == 'wait'
    await admin.execute("UPDATE runtime_commands SET status='completed'")
    async with sessions.begin() as db:
        assert (await outbox.classify(db,envelope(),worker_id='w',node_id='node')).action == 'ack'
        assert (await outbox.classify(db,envelope(),worker_id='w',node_id='node')).reason == 'settled'
    assert await admin.fetchval('SELECT status FROM command_deliveries') == 'settled'
    async with sessions.begin() as db:
        with pytest.raises(ValueError,match='unknown_delivery_generation'):
            await outbox.classify(db,envelope(generation=99),worker_id='w',node_id='node')


@pytest.mark.parametrize('command_status', ['rejected','indeterminate'])
async def test_terminal_or_indeterminate_uses_durable_transport_disposition(database, command_status):
    sessions, admin = database
    await admit(sessions)
    await admin.execute('UPDATE runtime_commands SET status=$1',command_status)
    async with sessions.begin() as db:
        assert (await outbox.classify(db,envelope(),worker_id='w',node_id='node')).action == 'ack'
    expected = 'quarantined' if command_status=='indeterminate' else 'settled'
    assert await admin.fetchval('SELECT status FROM command_deliveries') == expected


async def test_route_change_ack_requires_committed_replacement(database):
    sessions, admin = database
    await admit(sessions)
    await admin.execute("UPDATE runtime_sessions SET owner_id='old',lease_until=now()-interval '1 hour'")
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            result=await outbox.classify(db,envelope(),worker_id='w',node_id='node')
            assert result.action == 'ack'
            raise RuntimeError('rollback means caller must not ACK')
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries') == 1
    async with sessions.begin() as db:
        result=await outbox.classify(db,envelope(),worker_id='w',node_id='node')
        assert result.action == 'ack'
    assert await admin.fetchval('SELECT target_incarnation FROM command_deliveries WHERE delivery_generation=2') == 'old'
    async with sessions.begin() as db:
        assert (await outbox.classify(db,envelope(),worker_id='w',node_id='node')).reason == 'superseded'
        with pytest.raises(ValueError,match='consumer_route_mismatch'):
            await outbox.classify(db,envelope(2,worker='old'),worker_id='new',node_id='node')
        assert (await outbox.classify(db,envelope(2,worker='old'),worker_id='old',node_id='node')).action == 'execute'


async def test_released_owner_reroutes_to_node(database):
    sessions, admin = database
    await admit(sessions)
    await admin.execute("UPDATE runtime_sessions SET owner_id='old'")
    async with sessions.begin() as db:
        await outbox.reroute(db,'cmd',expected_generation=1)
    await admin.execute('UPDATE runtime_sessions SET owner_id=NULL')
    async with sessions.begin() as db:
        assert (await outbox.classify(db,envelope(2,worker='old'),worker_id='old',node_id='node')).action == 'ack'
    assert await admin.fetchval('SELECT target_incarnation FROM command_deliveries WHERE delivery_generation=3') is None


async def test_mutated_envelope_cannot_settle_legitimate_delivery(database):
    sessions, admin = database
    await admit(sessions)
    async with sessions.begin() as db:
        for value in (envelope(node='another'),envelope(worker='forged')):
            with pytest.raises(ValueError,match='envelope_route_mismatch'):
                await outbox.classify(db,value,worker_id='w',node_id='node')
        with pytest.raises(ValueError,match='consumer_route_mismatch'):
            await outbox.classify(db,envelope(),worker_id='w',node_id='another')
    assert await admin.fetchval('SELECT status FROM command_deliveries') == 'pending'


async def test_stop_uses_control_channel_and_classification(database):
    sessions, admin = database
    await admit(sessions)
    await admin.execute("UPDATE runtime_commands SET kind='StopTask'")
    async with sessions.begin() as db:
        await outbox.reroute(db,'cmd',expected_generation=1)
        result=await outbox.classify(db,envelope(2),worker_id='w',node_id='node')
        assert result.action == 'control'
        assert result.command['delivery_channel'] == 'control'
    assert await admin.fetchval('SELECT channel FROM command_deliveries WHERE delivery_generation=2') == 'control'


async def test_repair_only_latest_stale_published_and_missing_intents(database):
    sessions, admin = database
    await admit(sessions)
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=5)
    async with sessions.begin() as db:
        assert await outbox.repair_candidates(db,older_than=cutoff) == []
        claim=(await outbox.lease_due(db,limit=1))[0]
        await outbox.published(db,claim,'1-0')
        assert await outbox.repair_candidates(db,older_than=cutoff) == []
    await admin.execute("UPDATE command_deliveries SET published_at=now()-interval '10 minutes'")
    async with sessions.begin() as db:
        candidates=await outbox.repair_candidates(db,older_than=cutoff)
        assert len(candidates)==1 and candidates[0]['command_status']=='accepted'
        await outbox.reroute(db,'cmd',expected_generation=1)
        assert await outbox.repair_candidates(db,older_than=cutoff) == []
    await admin.execute('DELETE FROM command_deliveries')
    async with sessions.begin() as db:
        assert (await outbox.repair_candidates(db,older_than=cutoff))[0]['delivery_generation'] is None
        assert await outbox.stage(db,'cmd')


async def test_failed_publication_with_expired_token_cannot_override(database):
    sessions, admin = database
    await admit(sessions)
    async with sessions.begin() as db:
        claim=(await outbox.lease_due(db,limit=1))[0]
    await admin.execute("UPDATE command_deliveries SET lease_until=now()-interval '1 second'")
    async with sessions.begin() as db:
        assert not await outbox.failed(db,claim,'timeout',next_attempt_at=datetime.now(timezone.utc))
        assert not await outbox.published(db,claim,'1-0')


async def test_stage_and_settle_reject_unsafe_command_states(database):
    sessions, admin = database
    async with sessions.begin() as db:
        with pytest.raises(ValueError,match='Unsupported'):
            await outbox.stage(db,'missing')
        assert not await outbox.settle(db,'missing',1)
        with pytest.raises(ValueError,match='unknown_command'):
            await outbox.classify(db,envelope(),worker_id='w',node_id='node')
    await admit(sessions)
    async with sessions.begin() as db:
        assert not await outbox.settle(db,'cmd',1)
        assert await outbox.reroute(db,'cmd',expected_generation=99) is None
    await admin.execute("UPDATE runtime_commands SET status='claimed'")
    async with sessions.begin() as db:
        assert not await outbox.stage(db,'cmd')
        assert not await outbox.settle(db,'cmd',1)


async def test_claimed_live_owner_is_not_quarantined_but_lost_authority_is(database):
    sessions, admin = database
    await admit(sessions)
    await admin.execute("""UPDATE runtime_sessions SET owner_id='worker',execution_epoch=1,lease_until=now()+interval '1 hour';
        UPDATE runtime_commands SET status='claimed',claimed_by='worker',claimed_epoch=1;""")
    async with sessions.begin() as db:
        assert not await outbox.quarantine_abandoned(db,'cmd',1)
    await admin.execute("UPDATE runtime_sessions SET lease_until=now()-interval '1 second'")
    async with sessions.begin() as db:
        assert await outbox.quarantine_abandoned(db,'cmd',1)
        assert (await outbox.classify(db,envelope(),worker_id='worker',node_id='node')).action=='ack'
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='claimed'
    assert await admin.fetchval('SELECT owner_id FROM runtime_sessions')=='worker'
    assert await admin.fetchval('SELECT error_code FROM command_deliveries')=='execution_owner_lost'


async def test_repair_defer_keeps_old_live_delivery_out_of_next_batch(database):
    sessions, admin = database
    await admit(sessions)
    async with sessions.begin() as db:
        claim=(await outbox.lease_due(db,limit=1))[0]
        await outbox.published(db,claim,'1-0')
    await admin.execute("UPDATE command_deliveries SET published_at=now()-interval '1 hour'")
    async with sessions.begin() as db:
        cutoff=datetime.now(timezone.utc)-timedelta(minutes=5)
        assert len(await outbox.repair_candidates(db,older_than=cutoff))==1
        assert await outbox.touch_repair(db,'cmd',1,next_attempt_at=datetime.now(timezone.utc)+timedelta(hours=1))
        assert await outbox.repair_candidates(db,older_than=cutoff)==[]


async def test_owner_resume_control_becomes_execute_only_after_release(database):
    sessions, admin = database
    await admit(sessions)
    await admin.execute("UPDATE runtime_commands SET kind='ResumeInteraction'; UPDATE runtime_sessions SET owner_id='worker'")
    async with sessions.begin() as db:
        assert await outbox.reroute(db,'cmd',expected_generation=1)==2
        assert (await outbox.classify(db,envelope(2,worker='worker'),worker_id='worker',node_id='node')).action=='control'
    assert await admin.fetchval('SELECT channel FROM command_deliveries WHERE delivery_generation=2')=='control'
    await admin.execute('UPDATE runtime_sessions SET owner_id=NULL')
    async with sessions.begin() as db:
        assert (await outbox.classify(db,envelope(2,worker='worker'),worker_id='worker',node_id='node')).action=='ack'
        assert (await outbox.classify(db,envelope(3),worker_id='new',node_id='node')).action=='execute'


async def test_route_retry_exhaustion_quarantines_before_ack_without_replay(database):
    sessions, admin = database
    await admit(sessions)
    await admin.execute("UPDATE runtime_sessions SET owner_id='owner'")
    async with sessions.begin() as db:
        result=await outbox.classify(db,envelope(),worker_id='w',node_id='node',max_generations=1)
        assert (result.action,result.reason)==('ack','route_retry_exhausted')
    assert await admin.fetchval('SELECT status FROM command_deliveries')=='quarantined'
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'
    async with sessions.begin() as db:
        assert await outbox.reroute(db,'cmd',expected_generation=1) is None
        assert await outbox.lease_due(db,limit=1)==[]


async def test_repair_covers_claimed_before_publication_receipt_crash(database):
    sessions, admin = database
    await admit(sessions)
    async with sessions.begin() as db:
        await outbox.lease_due(db,limit=1)
    await admin.execute("""UPDATE runtime_commands SET status='claimed',claimed_by='worker',claimed_epoch=1;
        UPDATE runtime_sessions SET owner_id='worker',execution_epoch=1,lease_until=now()+interval '1 hour';
        UPDATE command_deliveries SET created_at=now()-interval '1 hour';""")
    async with sessions.begin() as db:
        cutoff=datetime.now(timezone.utc)-timedelta(minutes=5)
        candidates=await outbox.repair_candidates(db,older_than=cutoff)
        assert len(candidates)==1 and candidates[0]['delivery_status']=='publishing'
        assert not await outbox.quarantine_abandoned(db,'cmd',1)
        assert await outbox.touch_repair(db,'cmd',1,next_attempt_at=datetime.now(timezone.utc)+timedelta(hours=1))
        assert await outbox.repair_candidates(db,older_than=cutoff)==[]
    await admin.execute("UPDATE runtime_sessions SET lease_until=now()-interval '1 second'")
    async with sessions.begin() as db:
        assert await outbox.quarantine_abandoned(db,'cmd',1)
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='claimed'


async def test_control_admission_stages_same_transaction_and_owner_route(database):
    from workmate_contracts import Command
    from workmate_product.persistence.repositories.control_admission import insert_command
    sessions, admin = database
    await admit(sessions)
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker'")
    control=Command(command_id='stop',kind='StopTask',conversation_id='c',
                    runtime_session_id='s',product_task_id='t',payload={'reason':'user_requested'})
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            await insert_command(db,control,'u',control.content_digest())
            assert await db.scalar(text("SELECT count(*) FROM command_deliveries WHERE command_id='stop'"))==1
            assert await admin.fetchval("SELECT count(*) FROM command_deliveries WHERE command_id='stop'")==0
            raise RuntimeError('rollback')
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE id='stop'")==0
    async with sessions.begin() as db:
        await insert_command(db,control,'u',control.content_digest())
    row=await admin.fetchrow("SELECT channel,target_incarnation FROM command_deliveries WHERE command_id='stop'")
    assert tuple(row)==('control','worker')


async def test_failed_claim_quarantines_only_current_accepted_delivery(database):
    sessions,admin=database
    await admit(sessions)
    async with sessions.begin() as db:
        await outbox.reroute(db,'cmd',expected_generation=1)
        assert not await outbox.quarantine_unclaimed(db,'cmd',1)
        assert await outbox.quarantine_unclaimed(db,'cmd',2)
        assert not await outbox.quarantine_unclaimed(db,'cmd',2)
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'
    await admin.execute("UPDATE command_deliveries SET status='pending' WHERE delivery_generation=2; UPDATE runtime_commands SET status='claimed'")
    async with sessions.begin() as db:
        assert not await outbox.quarantine_unclaimed(db,'cmd',2)
    assert await admin.fetchval('SELECT status FROM command_deliveries WHERE delivery_generation=2')=='pending'


async def seed_residency(admin,status='held'):
    await admin.execute("""INSERT INTO workspace_residencies(session_id,storage_node_id,workspace_generation,
        holder_worker_id,hold_version,status,release_request_id,last_task_id,last_execution_epoch)
        SELECT session_id,'node','directory','warm',1,$1::varchar,CASE WHEN $1::varchar='held' THEN NULL ELSE 'release' END,task_id,1
        FROM runtime_commands WHERE id='cmd'""",status)


async def test_public_delivery_reroutes_to_persistent_warm_holder_before_ack(database):
    from workmate_product.coordination.command_queue import CommandEnvelope
    sessions,admin=database
    await admit(sessions)
    await seed_residency(admin)
    async with sessions.begin() as db:
        result=await outbox.classify(db,CommandEnvelope(command_id='cmd',delivery_generation=1,storage_node_id='node'),worker_id='peer',node_id='node')
        assert result.action=='ack' and result.reason=='route_changed'
        assert await db.scalar(text("SELECT target_incarnation FROM command_deliveries WHERE delivery_generation=2"))=='warm'
    async with sessions.begin() as db:
        result=await outbox.classify(db,CommandEnvelope(command_id='cmd',delivery_generation=2,storage_node_id='node',target_incarnation='warm'),worker_id='warm',node_id='node')
        assert result.action=='execute'


@pytest.mark.parametrize('status',['releasing','blocked'])
async def test_unresolved_release_waits_without_quarantining_start(database,status):
    from workmate_product.coordination.command_queue import CommandEnvelope
    sessions,admin=database
    await admit(sessions)
    await seed_residency(admin,status)
    async with sessions.begin() as db:
        result=await outbox.classify(db,CommandEnvelope(command_id='cmd',delivery_generation=1,storage_node_id='node'),worker_id='peer',node_id='node')
        assert result.action=='wait'
        assert not await outbox.quarantine_unclaimed(db,'cmd',1)
        assert await outbox.reroute(db,'cmd',expected_generation=1) is None
    assert await admin.fetchval('SELECT status FROM command_deliveries')=='pending'
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'


async def test_execution_owner_overrides_warm_holder_and_released_ignores_old_hint(database):
    sessions,admin=database
    await admit(sessions)
    await seed_residency(admin)
    await admin.execute("UPDATE runtime_sessions SET owner_id='actual'")
    async with sessions.begin() as db:
        assert await outbox.reroute(db,'cmd',expected_generation=1,target_worker='hint')==2
    assert await admin.fetchval('SELECT target_incarnation FROM command_deliveries WHERE delivery_generation=2')=='actual'
    await admin.execute("UPDATE runtime_sessions SET owner_id=NULL; UPDATE workspace_residencies SET status='released',release_request_id='release'")
    async with sessions.begin() as db:
        assert await outbox.reroute(db,'cmd',expected_generation=2,target_worker='stale-hint')==3
    assert await admin.fetchval('SELECT target_incarnation FROM command_deliveries WHERE delivery_generation=3') is None


async def test_claim_rejection_after_residency_change_reroutes_instead_of_quarantining(database):
    from workmate_product.coordination.command_queue import CommandEnvelope
    sessions,admin=database
    await admit(sessions)
    envelope=CommandEnvelope(command_id='cmd',delivery_generation=1,storage_node_id='node')
    async with sessions.begin() as db:
        assert (await outbox.classify(db,envelope,worker_id='peer',node_id='node')).action=='execute'
    await seed_residency(admin)
    async with sessions.begin() as db:
        assert not await outbox.quarantine_unclaimed(db,'cmd',1)
        result=await outbox.classify(db,envelope,worker_id='peer',node_id='node')
        assert result.action=='ack' and result.reason=='superseded'
    assert await admin.fetchval('SELECT target_incarnation FROM command_deliveries WHERE delivery_generation=2')=='warm'
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'


@pytest.mark.parametrize('command_status', ['accepted', 'claimed'])
async def test_superseded_session_quarantines_transport_without_mutating_execution(database, command_status):
    sessions, admin = database
    await admit(sessions)
    await admin.execute("UPDATE runtime_commands SET status=$1", command_status)
    await admin.execute("UPDATE runtime_sessions SET superseded_at=clock_timestamp(),owner_id='old'")
    async with sessions.begin() as db:
        assert await outbox.lease_due(db) == []
        candidates = await outbox.repair_candidates(db, older_than=datetime.now(timezone.utc))
        assert len(candidates) == 1
        assert candidates[0]['session_superseded_at'] is not None
        result = await outbox.classify(db, envelope(), worker_id='w', node_id='node')
        assert (result.action, result.reason) == ('ack', 'session_superseded')
        assert await outbox.reroute(db, envelope().command_id, expected_generation=1) is None
    assert await admin.fetchval('SELECT status FROM runtime_commands') == command_status
    assert await admin.fetchval('SELECT owner_id FROM runtime_sessions') == 'old'
    assert await admin.fetchval('SELECT status FROM command_deliveries') == 'quarantined'
    async with sessions.begin() as db:
        assert await outbox.repair_candidates(db, older_than=datetime.now(timezone.utc)) == []
