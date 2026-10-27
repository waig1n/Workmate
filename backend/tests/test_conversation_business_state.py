"""Authorized consistent Product reset views on isolated real PostgreSQL."""
import json

import pytest
import pytest_asyncio

from test_durable_projection import projection_database
from app.api.schemas import TaskOut
from app.services.conversation_business_state import ConversationUnavailable
from app.services.conversation_business_state import (
    ConversationBusinessState, ResetCapacityExceeded, ResetStateConflict,
)
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.persistence.models import TaskMessage, TaskEvent, Artifact, TaskArtifactDelivery
from workmate_product.persistence.repositories.message_snapshots import write_message
from workmate_product.realtime.message_accumulator import MessageAccumulator


@pytest_asyncio.fixture
async def database(projection_database):
    sessions, admin = projection_database
    await admin.execute("UPDATE tasks SET status='running',run_id='reply'; UPDATE runtime_sessions SET owner_id='worker',lease_until=now()+interval '60 seconds'")
    return sessions, admin


def service(sessions, **kwargs):
    return ConversationBusinessState(sessions, 'u', **kwargs)


@pytest.mark.parametrize('mutation', ['nonowner', 'deleted', 'disabled', 'token_version', 'missing'])
async def test_visibility_and_revoked_auth_fail_before_reset_view(database, mutation):
    sessions, admin = database
    actor, cid = 'u', 'c'
    if mutation == 'nonowner':
        actor = 'other'
        await admin.execute("INSERT INTO users(id,email,normalized_email,password_hash,display_name) VALUES ('other','other@example.test','other@example.test','x','Other')")
    elif mutation == 'deleted':
        await admin.execute("UPDATE conversations SET deleted_at=now()")
    elif mutation == 'disabled':
        await admin.execute("UPDATE users SET status='disabled'")
    elif mutation == 'token_version':
        await admin.execute('UPDATE users SET token_version=1')
    else:
        cid = 'missing'
    reader = ConversationBusinessState(sessions, actor, token_version=0)
    with pytest.raises(ConversationUnavailable):
        await reader.authorize(cid)
    with pytest.raises(ConversationUnavailable):
        await reader.read_view(cid)


@pytest.mark.parametrize('deleted_agent', [False, True])
async def test_owned_history_survives_agent_deletion_or_absent_capability_grants(database, deleted_agent):
    sessions, admin = database
    # The organization Agent has no membership or capability grants for u.
    if deleted_agent:
        await admin.execute('UPDATE agents SET deleted_at=now()')
    reader = service(sessions, token_version=0)
    assert await reader.authorize('c') is None
    view = await reader.read_view('c')
    assert view.conversation_id == 'c'
    assert view.active.reply_id == 'reply'


async def test_view_contains_product_task_and_whitelisted_legacy_messages(database):
    sessions, admin = database
    await admin.execute('UPDATE conversations SET business_seq=7')
    async with sessions.begin() as db:
        db.add(TaskMessage(id='legacy', task_id='t', conversation_id='c', role='assistant',
            content={'text':'historic body', 'attachments':[], 'presentation':{'version':1,'process_end':0},
                     'tool_input':'secret', 'core_snapshot':{'secret':'hidden'}}))
    view = await service(sessions).read_view('c')
    assert view.business_seq == 7
    task = TaskOut.model_validate(view.business_state['tasks'][0])
    assert task.id == 't' and task.status == 'running' and task.run_id == 'reply'
    message = next(row for row in view.business_state['messages'] if row['id'] == 'legacy')
    assert message['content'] == {'text':'historic body','attachments':[],
                                  'presentation':{'version':1,'process_end':0}}
    assert view.snapshots == ()


async def test_versioned_display_body_only_appears_in_snapshots(database):
    sessions, _ = database
    accumulator = MessageAccumulator('c', 't', 'reply', 1)
    accumulator.apply({'type':'TEXT_BLOCK_START','reply_id':'reply','block_id':'b'})
    accumulator.apply({'type':'TEXT_BLOCK_DELTA','reply_id':'reply','block_id':'b','delta':'current display'})
    snapshot = accumulator.capture('partial', None, 0, unanchored=True)
    async with sessions.begin() as db:
        await write_message(db, snapshot, unanchored=True)
    view = await service(sessions).read_view('c')
    assert view.snapshots == (snapshot,)
    assert not any(row['id'] == snapshot.message_id for row in view.business_state['messages'])


@pytest.mark.parametrize('mutation', ['reply', 'epoch', 'session_task', 'missing_execution'])
async def test_active_execution_must_agree_with_product_and_session_bindings(database, mutation):
    sessions, admin = database
    if mutation == 'reply':
        await admin.execute("UPDATE runtime_task_executions SET reply_id='different'")
    elif mutation == 'epoch':
        await admin.execute('UPDATE runtime_task_executions SET execution_epoch=2')
    elif mutation == 'session_task':
        await admin.execute('UPDATE runtime_sessions SET active_task_id=NULL')
    else:
        await admin.execute('DELETE FROM runtime_task_executions')
    with pytest.raises(ResetStateConflict):
        await service(sessions).read_view('c')


@pytest.mark.parametrize('status,owner,live,expected', [
    ('running','worker',True,True), ('preparing','worker',True,True),
    ('stopping','worker',True,True), ('awaiting_interaction','worker',True,False),
    ('running',None,True,False), ('running','worker',False,False),
])
async def test_refresh_requires_live_owner_in_refreshable_execution_phase(database, status, owner, live, expected):
    sessions, admin = database
    await admin.execute('UPDATE runtime_sessions SET status=$1,owner_id=$2,lease_until=now()+make_interval(secs=>$3)',
                        status, owner, 60 if live else -60)
    await admin.execute('UPDATE runtime_task_executions SET status=$1', status)
    view = await service(sessions).read_view('c')
    assert view.active.execution_epoch == 1
    assert view.active.refresh_possible is expected
    assert view.active.empty is False


async def empty_boundary(admin):
    await admin.execute("""INSERT INTO runtime_snapshots
        (id,session_id,task_id,state_version,runtime_seq,execution_epoch,status,reply_id,payload)
        VALUES ('cut','s','t',1,1,1,'awaiting_interaction','reply','{}');
        INSERT INTO runtime_events(id,session_id,task_id,runtime_seq,execution_epoch,payload)
        VALUES ('boundary','s','t',1,1,'{"type":"EXECUTION_BOUNDARY","reply_id":"reply","status":"awaiting_interaction","display_empty":true}');
        UPDATE runtime_sessions SET status='awaiting_interaction',state_version=1,snapshot_id='cut',runtime_seq=1;
        UPDATE runtime_task_executions SET status='awaiting_interaction',state_version=1,snapshot_id='cut';
    """)


@pytest.mark.parametrize('resumed', [False, True])
async def test_empty_display_requires_matching_immutable_quiescent_boundary(database, resumed):
    sessions, admin = database
    await empty_boundary(admin)
    if resumed:
        await admin.execute("UPDATE runtime_sessions SET status='running'; UPDATE runtime_task_executions SET status='running'")
    view = await service(sessions).read_view('c')
    assert view.active.empty is (not resumed)


async def test_mutable_event_flag_without_snapshot_cannot_claim_empty(database):
    sessions, admin = database
    await admin.execute("""INSERT INTO runtime_events(id,session_id,task_id,runtime_seq,execution_epoch,payload)
        VALUES ('fake','s','t',1,1,'{"type":"EXECUTION_BOUNDARY","reply_id":"reply","status":"awaiting_interaction","display_empty":true}');
        UPDATE runtime_sessions SET status='awaiting_interaction',runtime_seq=1;
        UPDATE runtime_task_executions SET status='awaiting_interaction';""")
    assert (await service(sessions).read_view('c')).active.empty is False


@pytest.mark.parametrize('kind', ['rows', 'single_large_json'])
async def test_reset_capacity_is_bounded_before_serializing_unlimited_history(database, kind, monkeypatch):
    sessions, _ = database
    policy = RealtimePolicy(reset_max_rows=1) if kind == 'rows' else RealtimePolicy(reset_max_bytes=266240)
    async with sessions.begin() as db:
        if kind == 'rows':
            for index in range(2):
                db.add(TaskMessage(id=f'm{index}',task_id='t',conversation_id='c',role='user',content={'text':'hi'}))
        else:
            db.add(TaskMessage(id='huge',task_id='t',conversation_id='c',role='user',content={'text':'x'*300000}))
    from workmate_product.persistence.repositories import conversation_display as repository
    async def forbidden_messages(*args, **kwargs):
        raise AssertionError('Oversized JSON must be rejected before loading message rows')
    monkeypatch.setattr(repository, 'messages', forbidden_messages)
    with pytest.raises(ResetCapacityExceeded):
        await service(sessions, policy=policy).read_view('c')


async def test_activity_excludes_tool_arguments_results_and_core_state(database):
    sessions, _ = database
    async with sessions.begin() as db:
        db.add(TaskEvent(task_id='t',conversation_id='c',seq=1,event_type='tool_call',payload={
            'toolCallId':'call','tool':'search','user':'u','input':{'secret':'PRIVATE_INPUT'},
            'result':'PRIVATE_RESULT','core_snapshot':{'state':'PRIVATE_CORE'}}))
    view = await service(sessions).read_view('c')
    activity = json.dumps(view.business_state['activity'])
    assert 'search' in activity and 'call' in activity
    assert all(secret not in activity for secret in ('PRIVATE_INPUT','PRIVATE_RESULT','PRIVATE_CORE'))


async def test_artifact_delivery_view_overrides_immutable_revision_metadata(database):
    sessions, _ = database
    async with sessions.begin() as db:
        db.add(Artifact(id='artifact',task_id='t',conversation_id='c',name='report.txt',
            mime_type='text/plain',storage_key='private-key',size=7,version=1,
            change_type='created',metadata_={'title':'revision'}))
        await db.flush()
        db.add(TaskArtifactDelivery(task_id='t',artifact_id='artifact',change_type='presented',
                                   metadata_={'title':'delivery'}))
    artifact = (await service(sessions).read_view('c')).business_state['artifacts'][0]
    assert artifact['change_type'] == 'presented'
    assert artifact['metadata']['title'] == 'delivery'
    assert 'storage_key' not in artifact


@pytest.mark.parametrize('closed', [False, True])
async def test_partial_decisions_and_closed_requests_do_not_reappear_as_pending(database, closed):
    sessions, admin = database
    await admin.execute("UPDATE tasks SET status='awaiting_permission'; UPDATE runtime_sessions SET status='awaiting_interaction'; UPDATE runtime_task_executions SET status='awaiting_interaction'")
    async with sessions.begin() as db:
        db.add(TaskEvent(task_id='t',conversation_id='c',seq=1,event_type='hitl_request',payload={
            'replyId':'reply','decisionVersion':'version-1','toolCalls':[
                {'id':'decided-call','name':'write','input':'{}'},
                {'id':'pending-call','name':'write','input':'{}'}]}))
        db.add(TaskEvent(task_id='t',conversation_id='c',seq=2,event_type='hitl_decision',payload={
            'replyId':'reply','decisionVersion':'version-1',
            'decisions':[{'toolCallId':'decided-call','approved':True}]}))
        if closed:
            db.add(TaskEvent(task_id='t',conversation_id='c',seq=3,event_type='hitl_abandoned',payload={
                'replyId':'reply','decisionVersion':'version-1'}))
    state = (await service(sessions).read_view('c')).business_state
    pending = json.dumps(state['pending_confirm'])
    assert 'decided-call' not in pending
    assert ('pending-call' in pending) is (not closed)
    assert 'decided-call' in json.dumps(state['decided_confirm_tools'])
    if closed:
        assert 'version-1' in json.dumps(state['closed_confirm_versions'])


async def test_view_is_one_repeatable_read_cut_despite_concurrent_product_commit(database, monkeypatch):
    from workmate_product.persistence.repositories import conversation_display as repository
    sessions, admin = database
    await admin.execute('UPDATE conversations SET business_seq=7')
    original_budget = repository.read_budget
    committed = False
    async def concurrent_budget(db, cid):
        nonlocal committed
        if not committed:
            committed = True
            # read_view has already authorized and captured the Conversation.
            # Publish a new message and its business watermark atomically from
            # another physical connection before the remaining state reads.
            async with admin.transaction():
                await admin.execute('UPDATE conversations SET business_seq=8')
                await admin.execute("""INSERT INTO task_messages(id,task_id,conversation_id,role,content)
                    VALUES ('concurrent','t','c','user','{"text":"new state"}')""")
        return await original_budget(db, cid)
    monkeypatch.setattr(repository, 'read_budget', concurrent_budget)
    reader = service(sessions)
    old = await reader.read_view('c')
    assert committed
    assert old.business_seq == 7
    assert all(row['id'] != 'concurrent' for row in old.business_state['messages'])
    new = await reader.read_view('c')
    assert new.business_seq == 8
    assert any(row['id'] == 'concurrent' for row in new.business_state['messages'])


@pytest.mark.parametrize('session_status', ['accepted', 'completed'])
async def test_admitted_unclaimed_task_is_queued_without_previous_reply_identity(database, session_status):
    sessions, admin = database
    await admin.execute("""UPDATE tasks SET status='completed';
        UPDATE runtime_task_executions SET status='completed';
        INSERT INTO tasks(id,conversation_id,title,status) VALUES ('next','c','Next task','created');
        INSERT INTO runtime_task_executions(task_id,session_id,status,execution_epoch)
            VALUES ('next','s','accepted',0);
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
            VALUES ('start-next','s','c','next','u','StartTask','{}',repeat('d',64));
        UPDATE conversations SET active_product_task_id='next';
    """)
    await admin.execute('''UPDATE runtime_sessions SET active_task_id=NULL,owner_id=NULL,lease_until=NULL,
        status=$1,reply_id=$2,execution_epoch=$3''', session_status,
        'previous-reply' if session_status == 'completed' else None,
        1 if session_status == 'completed' else 0)
    view = await service(sessions).read_view('c')
    assert view.active is None
    task = next(row for row in view.business_state['tasks'] if row['id'] == 'next')
    assert task['status'] == 'created' and task['execution_phase'] == 'queued'
    assert task['run_id'] is None
    assert all(snapshot.reply_id != 'previous-reply' for snapshot in view.snapshots)


@pytest.mark.parametrize('mismatch', ['epoch', 'sequence', 'status', 'event_status'])
async def test_empty_boundary_cannot_be_reused_across_changed_execution_cut(database, mismatch):
    sessions, admin = database
    await empty_boundary(admin)
    if mismatch == 'epoch':
        await admin.execute('UPDATE runtime_sessions SET execution_epoch=2; UPDATE runtime_task_executions SET execution_epoch=2')
    elif mismatch == 'sequence':
        await admin.execute('UPDATE runtime_sessions SET runtime_seq=2')
    elif mismatch == 'status':
        await admin.execute("UPDATE runtime_sessions SET status='running'; UPDATE runtime_task_executions SET status='running'")
    else:
        await admin.execute("UPDATE runtime_events SET payload=jsonb_set(payload,'{status}','\"completed\"')")
    assert (await service(sessions).read_view('c')).active.empty is False


@pytest.mark.parametrize('decision_state', ['partial', 'fully_decided', 'closed'])
async def test_rest_pending_confirm_returns_only_unresolved_calls(database, decision_state):
    from fastapi import HTTPException
    from app.api.routers.tasks import get_pending_confirm
    from workmate_product.persistence.models import User
    sessions, admin = database
    await admin.execute("UPDATE tasks SET status='awaiting_permission'")
    decisions = [{'toolCallId':'first','approved':True}]
    if decision_state == 'fully_decided':
        decisions.append({'toolCallId':'second','approved':False})
    async with sessions.begin() as db:
        db.add(TaskEvent(task_id='t',conversation_id='c',seq=1,event_type='hitl_request',payload={
            'replyId':'reply','decisionVersion':'original-version','toolCalls':[
                {'id':'first','name':'write','input':'{}'},
                {'id':'second','name':'write','input':'{}'}]}))
        db.add(TaskEvent(task_id='t',conversation_id='c',seq=2,event_type='hitl_decision',payload={
            'replyId':'reply','decisionVersion':'original-version','decisions':decisions}))
        if decision_state == 'closed':
            db.add(TaskEvent(task_id='t',conversation_id='c',seq=3,event_type='hitl_abandoned',payload={
                'replyId':'reply','decisionVersion':'original-version'}))
    async with sessions() as db:
        user = await db.get(User, 'u')
        if decision_state == 'partial':
            response = await get_pending_confirm('t', user=user, db=db)
            assert [call['id'] for call in response.toolCalls] == ['second']
            assert response.decisionVersion == 'original-version'
            assert response.taskId == 't'
        else:
            with pytest.raises(HTTPException) as error:
                await get_pending_confirm('t', user=user, db=db)
            assert error.value.status_code == 404


@pytest.mark.parametrize('session_status', ['accepted', 'completed'])
async def test_real_stop_admission_of_unclaimed_start_has_no_active_reply(database, session_status):
    from workmate_contracts import Command
    from workmate_product.application.control_commands import accept_stop
    sessions, admin = database
    epoch = 7 if session_status == 'completed' else 0
    await admin.execute("""UPDATE tasks SET status='created',run_id=NULL;
        UPDATE runtime_task_executions SET status='accepted',execution_epoch=0,reply_id=NULL;
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
            VALUES ('unclaimed-start','s','c','t','u','StartTask','{}',repeat('d',64));
    """)
    await admin.execute('''UPDATE runtime_sessions SET active_task_id=NULL,owner_id=NULL,lease_until=NULL,
        status=$1,execution_epoch=$2,reply_id=$3''', session_status, epoch,
        'previous-reply' if session_status == 'completed' else None)
    async with sessions.begin() as db:
        accepted = await accept_stop(db, Command(command_id='stop-unclaimed',kind='StopTask',
            conversation_id='c',runtime_session_id='s',product_task_id='t',payload={}), actor_user_id='u')
    assert accepted.status == 'accepted'
    assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='unclaimed-start'") == 'rejected'
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == 'cancelling'
    stop = await admin.fetchrow("SELECT expected_epoch,reply_id FROM runtime_commands WHERE id='stop-unclaimed'")
    assert tuple(stop) == (epoch, None)
    view = await service(sessions).read_view('c')
    assert view.active is None
    assert view.business_state['tasks'][0]['status'] == 'cancelling'
    assert view.business_state['tasks'][0]['run_id'] is None


@pytest.mark.parametrize('latest', ['fully_decided', 'abandoned'])
async def test_rest_pending_confirm_finds_older_unresolved_request_after_latest_is_closed(database, latest):
    from app.api.routers.tasks import get_pending_confirm
    from workmate_product.persistence.models import User
    sessions, admin = database
    await admin.execute("UPDATE tasks SET status='awaiting_permission'")
    async with sessions.begin() as db:
        for seq, version, call_id in [(1,'v1','a'),(2,'v2','b')]:
            db.add(TaskEvent(task_id='t',conversation_id='c',seq=seq,event_type='hitl_request',payload={
                'replyId':'reply','decisionVersion':version,
                'toolCalls':[{'id':call_id,'name':'write','input':'{}'}]}))
        db.add(TaskEvent(task_id='t',conversation_id='c',seq=3,
            event_type='hitl_decision' if latest == 'fully_decided' else 'hitl_abandoned',
            payload={'replyId':'reply','decisionVersion':'v2',
                     **({'decisions':[{'toolCallId':'b','approved':True}]} if latest == 'fully_decided' else {})}))
    async with sessions() as db:
        response = await get_pending_confirm('t', user=await db.get(User,'u'), db=db)
        assert response.decisionVersion == 'v1'
        assert [call['id'] for call in response.toolCalls] == ['a']


async def store_display(sessions, body='current display'):
    accumulator = MessageAccumulator('c', 't', 'reply', 1)
    accumulator.apply({'type':'TEXT_BLOCK_START','reply_id':'reply','block_id':'b'})
    for offset in range(0, len(body), 16000):
        accumulator.apply({'type':'TEXT_BLOCK_DELTA','reply_id':'reply','block_id':'b',
                           'delta':body[offset:offset + 16000]})
    snapshot = accumulator.capture('partial', None, 0, unanchored=True)
    async with sessions.begin() as db:
        await write_message(db, snapshot, unanchored=True)
    return snapshot


async def test_live_display_reads_do_not_load_the_full_conversation(database, monkeypatch):
    from workmate_contracts.realtime import MessageSnapshotReference
    from workmate_product.persistence.repositories import conversation_display as repository
    sessions, _ = database
    snapshot = await store_display(sessions)
    expected = (await service(sessions).read_view('c')).active
    async def forbidden(*args, **kwargs):
        raise AssertionError('Live display lookup must not materialize full reset state')
    monkeypatch.setattr(repository, 'messages', forbidden)
    monkeypatch.setattr(repository, 'read_budget', forbidden)
    reader = service(sessions)
    assert await reader.active_display('c') == expected
    reference = MessageSnapshotReference.from_snapshot(snapshot)
    assert await reader.display_snapshot('c', reference, task_id='t', reply_id='reply') == snapshot


@pytest.mark.parametrize('mutation', ['nonowner', 'deleted', 'disabled', 'token_version', 'missing'])
async def test_live_display_reads_reauthorize_each_request(database, mutation):
    from workmate_contracts.realtime import MessageSnapshotReference
    sessions, admin = database
    snapshot = await store_display(sessions)
    reference = MessageSnapshotReference.from_snapshot(snapshot)
    reader = service(sessions, token_version=0)
    assert await reader.active_display('c') is not None
    assert await reader.display_snapshot('c', reference, task_id='t', reply_id='reply') == snapshot
    cid = 'c'
    if mutation == 'nonowner':
        await admin.execute("INSERT INTO users(id,email,normalized_email,password_hash,display_name) VALUES ('other','other@example.test','other@example.test','x','Other')")
        reader = ConversationBusinessState(sessions, 'other', token_version=0)
    elif mutation == 'deleted':
        await admin.execute('UPDATE conversations SET deleted_at=now()')
    elif mutation == 'disabled':
        await admin.execute("UPDATE users SET status='disabled'")
    elif mutation == 'token_version':
        await admin.execute('UPDATE users SET token_version=1')
    else:
        cid = 'missing'
    with pytest.raises(ConversationUnavailable):
        await reader.active_display(cid)
    with pytest.raises(ConversationUnavailable):
        await reader.display_snapshot(cid, reference, task_id='t', reply_id='reply')


@pytest.mark.parametrize('mismatch', ['version', 'digest', 'epoch', 'task', 'reply', 'missing', 'legacy'])
async def test_display_snapshot_requires_exact_notification_reference(database, mismatch):
    from workmate_contracts.realtime import MessageSnapshotReference
    from app.services.conversation_business_state import DisplaySnapshotUnavailable
    sessions, admin = database
    snapshot = await store_display(sessions)
    reference = MessageSnapshotReference.from_snapshot(snapshot).model_dump(mode='json')
    task_id, reply_id = 't', 'reply'
    if mismatch == 'version':
        reference['snapshot_version'] += 1
    elif mismatch == 'digest':
        reference['sha256'] = '0' * 64
    elif mismatch == 'epoch':
        reference['execution_epoch'] += 1
    elif mismatch == 'task':
        task_id = 'another-task'
    elif mismatch == 'reply':
        reply_id = 'another-reply'
    elif mismatch == 'missing':
        await admin.execute('DELETE FROM task_messages')
    else:
        await admin.execute('UPDATE task_messages SET snapshot_version=0')
    with pytest.raises(DisplaySnapshotUnavailable):
        await service(sessions).display_snapshot('c', reference, task_id=task_id, reply_id=reply_id)


async def test_display_snapshot_budget_is_checked_before_body_materialization(database, monkeypatch):
    from workmate_contracts.realtime import MessageSnapshotReference
    from workmate_product.persistence.repositories import conversation_display as repository
    sessions, _ = database
    snapshot = await store_display(sessions, '大' * 100000)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Oversize snapshot must fail before loading its JSON body')
    monkeypatch.setattr(repository, 'message', forbidden)
    reader = service(sessions, policy=RealtimePolicy(reset_max_bytes=266240))
    with pytest.raises(ResetCapacityExceeded):
        await reader.display_snapshot('c', MessageSnapshotReference.from_snapshot(snapshot),
                                      task_id='t', reply_id='reply')


@pytest.mark.parametrize('runtime_status', ['preparing', 'running', 'stopping'])
@pytest.mark.parametrize('operation', ['reset', 'snapshot'])
async def test_old_approval_is_not_offered_after_runtime_has_resumed(database, runtime_status, operation):
    from workmate_contracts.realtime import MessageSnapshotReference
    from app.services.conversation_business_state import DisplaySnapshotUnavailable
    sessions, admin = database
    accumulator = MessageAccumulator('c', 't', 'reply', 1)
    accumulator.apply({'type':'TEXT_BLOCK_START','reply_id':'reply','block_id':'b'})
    accumulator.apply({'type':'TEXT_BLOCK_DELTA','reply_id':'reply','block_id':'b','delta':'approve this tool'})
    snapshot = accumulator.capture('approval', None, 0, unanchored=True)
    async with sessions.begin() as db:
        await write_message(db, snapshot, unanchored=True)
        db.add(TaskEvent(task_id='t',conversation_id='c',seq=1,event_type='hitl_request',payload={
            'replyId':'reply','decisionVersion':'version-1',
            'toolCalls':[{'id':'call','name':'write','input':'{}'}]}))
    # Product status can lag the already-resumed Runtime projection; its old
    # approval body is still valid history but no longer an actionable card.
    await admin.execute("UPDATE tasks SET status='awaiting_permission'")
    await admin.execute('UPDATE runtime_sessions SET status=$1', runtime_status)
    await admin.execute('UPDATE runtime_task_executions SET status=$1', runtime_status)
    reader = service(sessions)
    if operation == 'reset':
        view = await reader.read_view('c')
        assert view.snapshots == (snapshot,)
        assert view.business_state['pending_confirm'] is None
    else:
        with pytest.raises(DisplaySnapshotUnavailable):
            await reader.display_snapshot('c', MessageSnapshotReference.from_snapshot(snapshot),
                                          task_id='t', reply_id='reply')


@pytest.mark.parametrize('change', ['unchanged', 'stop', 'partial_decision', 'all_decided'])
async def test_approval_display_revalidates_actionable_request_after_reset_read(database, change):
    from workmate_contracts.realtime import MessageSnapshotReference
    from app.services.conversation_business_state import DisplaySnapshotUnavailable
    sessions, admin = database
    accumulator = MessageAccumulator('c', 't', 'reply', 1)
    accumulator.apply({'type':'TEXT_BLOCK_START','reply_id':'reply','block_id':'b'})
    accumulator.apply({'type':'TEXT_BLOCK_DELTA','reply_id':'reply','block_id':'b','delta':'Approval body'})
    snapshot = accumulator.capture('approval', None, 0, unanchored=True)
    await admin.execute("UPDATE tasks SET status='awaiting_permission'; UPDATE runtime_sessions SET status='awaiting_interaction'; UPDATE runtime_task_executions SET status='awaiting_interaction'")
    async with sessions.begin() as db:
        await write_message(db, snapshot, unanchored=True)
        db.add(TaskEvent(task_id='t',conversation_id='c',seq=1,event_type='hitl_request',payload={
            'replyId':'reply','decisionVersion':'v1','toolCalls':[
                {'id':'a','name':'write','input':'{}'}, {'id':'b','name':'write','input':'{}'}]}))
    reader = service(sessions)
    pending = (await reader.read_view('c')).business_state['pending_confirm']
    assert [call['id'] for call in pending['toolCalls']] == ['a', 'b']
    if change == 'stop':
        await admin.execute("UPDATE tasks SET status='cancelling'")
    elif change in {'partial_decision', 'all_decided'}:
        calls = ['a'] if change == 'partial_decision' else ['a', 'b']
        async with sessions.begin() as db:
            db.add(TaskEvent(task_id='t',conversation_id='c',seq=2,event_type='hitl_decision',payload={
                'replyId':'reply','decisionVersion':'v1',
                'decisions':[{'toolCallId':call,'approved':True} for call in calls]}))
    assert await admin.fetchval("SELECT status FROM runtime_sessions WHERE id='s'") == 'awaiting_interaction'
    async def lookup():
        return await reader.display_snapshot('c', MessageSnapshotReference.from_snapshot(snapshot),
                                            task_id='t', reply_id='reply', expected_approval=pending)
    if change == 'unchanged':
        assert await lookup() == snapshot
    else:
        with pytest.raises(DisplaySnapshotUnavailable):
            await lookup()


async def test_schedule_task_provenance_survives_reset_view(database):
    from datetime import datetime, timezone
    from sqlalchemy import update
    from workmate_product.persistence.models import Schedule, ScheduleOccurrence, Task
    sessions, admin = database
    async with sessions.begin() as db:
        now = datetime.now(timezone.utc)
        db.add(Schedule(id='schedule', owner_user_id='u', agent_id='a', name='Daily',
            prompt='hello', timezone='Asia/Shanghai', rule={'kind':'daily','time':'09:00'}))
        await db.flush()
        db.add(ScheduleOccurrence(id='occ', schedule_id='schedule', schedule_version=1,
            scheduled_for=now, triggered_at=now, outcome='task_created', task_id='t',
            conversation_id='c', snapshot={}))
        await db.flush()
        await db.execute(update(Task).where(Task.id=='t').values(origin='schedule',
            schedule_occurrence_id='occ', approval_profile='unattended'))
    view = await service(sessions).read_view('c')
    task = view.business_state['tasks'][0]
    assert task['origin'] == 'schedule'
    assert task['schedule_occurrence_id'] == 'occ'
    assert task['approval_profile'] == 'unattended'
