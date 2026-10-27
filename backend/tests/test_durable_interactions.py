"""Durable permission cards are published only from verified parked boundaries."""
import asyncio
import json

import pytest
from sqlalchemy.exc import IntegrityError

from app.adapter.durable_interactions import interaction_projector
from workmate_product.approval import approval_request_version
from workmate_contracts import snapshot_digest
from workmate_product.projections.store import consume_next, ProjectionConflict
from test_durable_projection import projection_database, emit  # noqa: F401


CALL={'id':'a','name':'write','input':'{"path":"a"}','state':'asking','suggested_rules':[]}
SECOND={**CALL,'id':'b','input':'{"path":"b"}'}


async def consume(sessions):
    async with sessions.begin() as db:
        return await consume_next(db,'c',project=interaction_projector())


async def source(sessions,admin,raw):
    seq=(await admin.fetchval('SELECT runtime_seq FROM runtime_sessions'))+1
    await emit(admin,seq,raw)
    await consume(sessions)
    return seq


async def request(sessions,admin,calls=None,kind='REQUIRE_USER_CONFIRM'):
    if not await admin.fetchval('SELECT count(*) FROM runtime_events'):
        await source(sessions,admin,{'type':'REPLY_START','session_id':'s'})
    return await source(sessions,admin,{'type':kind,'tool_calls':calls or [CALL]})


async def park(admin,calls=None,status='awaiting_interaction',digest=None,display=None,missing_display=False):
    calls=[CALL] if calls is None else calls
    seq=(await admin.fetchval('SELECT runtime_seq FROM runtime_sessions'))+1
    revision=(await admin.fetchval('SELECT count(*) FROM runtime_snapshots'))+1
    snapshot={'state':{'fixture':'opaque-engine-data'},'format_version':1,'engine_version':'2.0.7'}
    pending=[{key:call[key] for key in ('id','name','input')} | {
        'kind':'external_execution' if call.get('state')=='submitted' else 'permission'} for call in calls]
    await emit(admin,seq,{'type':'EXECUTION_BOUNDARY','status':status,
        'pending_tool_call_ids':[c['id'] for c in calls],
        'boundary_evidence':{'protocol_version':1,'snapshot_sha256':digest or snapshot_digest(snapshot),'pending_tools':pending},
        **({'display_snapshot':display} if display is not None else {} if missing_display else {'display_empty':True})})
    await admin.execute('''INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,
        runtime_seq,execution_epoch,status,reply_id,payload)
        VALUES ($1,'s','t',$2,$3,1,$4,'reply',$5::jsonb)''',str(seq),revision,seq,status,json.dumps(snapshot))
    return seq


async def cards(admin):
    return [json.loads(row['payload']) for row in await admin.fetch(
        "SELECT payload FROM product_events WHERE payload->>'name'='HITL_APPROVAL_REQUEST' ORDER BY product_seq")]


async def decide(admin,calls,version='custom-client-key'):
    await admin.execute('''INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
        SELECT 't','c',coalesce(max(seq),0)+1,'hitl_decision',$1::json FROM task_events WHERE task_id='t' ''',
        json.dumps({'decisionVersion':version,'decisions':[{'toolCallId':c['id'],'approved':True} for c in calls]}))


async def test_request_staging_then_atomic_park_and_duplicate_consumers(projection_database):
    sessions,admin=projection_database
    seq=await request(sessions,admin)
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await cards(admin)==[]
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_request'")==0
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq
    await park(admin)
    await asyncio.gather(consume(sessions),consume(sessions))
    assert await admin.fetchval('SELECT status FROM tasks')=='awaiting_permission'
    card=(await cards(admin))[0]['value']
    assert card['toolCalls']==[CALL]
    assert card['decisionVersion']==approval_request_version('reply',[CALL])
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_request'")==1
    await request(sessions,admin)
    await park(admin)
    await consume(sessions)
    assert len(await cards(admin))==1
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'


async def test_partial_confirmation_keeps_original_version_without_new_request(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin,[CALL,SECOND])
    await park(admin,[CALL,SECOND])
    await consume(sessions)
    await decide(admin,[CALL])
    await source(sessions,admin,{'type':'REPLY_START','session_id':'s'})
    await park(admin,[SECOND])
    await consume(sessions)
    values=await cards(admin)
    assert len(values)==2
    assert values[-1]['value']['toolCalls']==[SECOND]
    assert values[-1]['value']['decisionVersion']==approval_request_version('reply',[CALL,SECOND])
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_request'")==1
    await decide(admin,[SECOND],version='another-client-key')
    await park(admin,[SECOND])
    await consume(sessions)
    assert len(await cards(admin))==2
    state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    assert state['pending_interactions'][0]['awaiting_resume'] is True


async def test_same_reply_new_round_uses_new_request_and_version(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin)
    await park(admin)
    await consume(sessions)
    await decide(admin,[CALL])
    await request(sessions,admin,[SECOND])
    await park(admin,[SECOND])
    await consume(sessions)
    values=await cards(admin)
    assert len(values)==2
    assert values[0]['value']['decisionVersion']!=values[1]['value']['decisionVersion']


@pytest.mark.parametrize('bad',['unknown','changed_input','missing_original','bad_digest','duplicate'])
async def test_unsafe_boundary_does_not_publish_or_advance(projection_database,bad):
    sessions,admin=projection_database
    await request(sessions,admin,[CALL,SECOND])
    calls={'unknown':[{**CALL,'id':'unknown'}],'changed_input':[{**CALL,'input':'{}'}],
           'missing_original':[CALL],'duplicate':[CALL,CALL]}.get(bad,[CALL,SECOND])
    seq=await park(admin,calls,digest='0'*64 if bad=='bad_digest' else None)
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq-1
    assert await cards(admin)==[]


@pytest.mark.parametrize('state',['cancelling','cancelled'])
async def test_cancellation_never_reopens_approval(projection_database,state):
    sessions,admin=projection_database
    await request(sessions,admin)
    await admin.execute('UPDATE tasks SET status=$1',state)
    await park(admin)
    await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')==state
    assert await cards(admin)==[]


@pytest.mark.parametrize('tombstone',['hitl_abandoned','hitl_resume_exhausted'])
async def test_closed_request_is_not_republished(projection_database,tombstone):
    sessions,admin=projection_database
    await request(sessions,admin)
    await admin.execute("INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload) VALUES ('t','c',2,$1,$2::json)",
        tombstone,json.dumps({'decisionVersion':approval_request_version('reply',[CALL])}))
    await park(admin)
    await consume(sessions)
    assert await cards(admin)==[]
    assert await admin.fetchval('SELECT status FROM tasks')=='running'


async def test_external_execution_remains_internal_and_never_permission_card(projection_database):
    sessions,admin=projection_database
    external={**CALL,'state':'submitted'}
    await request(sessions,admin,[external],kind='REQUIRE_EXTERNAL_EXECUTION')
    await park(admin,[external])
    await consume(sessions)
    assert await cards(admin)==[]
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    assert state['pending_interactions'][0]['kind']=='external_execution'


async def test_card_failure_rolls_back_park_audit_and_cursor(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin)
    seq=await park(admin)
    await admin.execute("ALTER TABLE product_events ADD CONSTRAINT reject_card CHECK (payload->>'name' IS DISTINCT FROM 'HITL_APPROVAL_REQUEST')")
    async with sessions.begin() as db:
        with pytest.raises(IntegrityError):
            await consume_next(db,'c',project=interaction_projector())
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_request'")==0
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq-1


@pytest.mark.parametrize('answered',[False,True])
async def test_safe_terminal_closes_interaction_and_settles_without_gate_release(projection_database,answered):
    sessions,admin=projection_database
    await request(sessions,admin)
    await park(admin)
    await consume(sessions)
    if answered:
        await decide(admin,[CALL])
    await source(sessions,admin,{'type':'REPLY_END','finished_reason':'completed'})
    await park(admin,[],status='completed')
    await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='completed'
    expected='hitl_resume_exhausted' if answered else 'hitl_abandoned'
    assert await admin.fetchval('SELECT count(*) FROM task_events WHERE event_type=$1',expected)==1
    assert json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))['pending_interactions']==[]
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'


async def test_per_tool_core_requests_publish_one_aggregated_product_card(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin,[CALL])
    await request(sessions,admin,[SECOND])
    await park(admin,[CALL,SECOND])
    await consume(sessions)
    values=await cards(admin)
    assert len(values)==1
    assert values[0]['value']=={'toolCalls':[CALL,SECOND],
        'taskId':'t','approvalProfile':'standard',
        'decisionVersion':approval_request_version('reply',[CALL,SECOND])}
    await decide(admin,[CALL])
    await park(admin,[SECOND])
    await consume(sessions)
    values=await cards(admin)
    assert values[-1]['value']['toolCalls']==[SECOND]
    assert values[-1]['value']['decisionVersion']==values[0]['value']['decisionVersion']
    decision=json.loads(await admin.fetchval("SELECT payload FROM product_events WHERE payload->>'name'='HITL_DECISION_RECORDED'"))
    assert decision['value']['decidedToolCallIds']==['a']


async def test_jsonb_snapshot_numeric_roundtrip_keeps_evidence_valid(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin)
    snapshot={'numeric':[1e20,1.2345678901234567e50,1e-20,-0.0,123456789012345678901234567890,True]}
    seq=3
    await emit(admin,seq,{'type':'EXECUTION_BOUNDARY','status':'awaiting_interaction',
        'pending_tool_call_ids':['a'],'display_empty':True,'boundary_evidence':{'snapshot_sha256':snapshot_digest(snapshot),
        'pending_tools':[{'id':'a','name':CALL['name'],'input':CALL['input'],'kind':'permission'}]}})
    await admin.execute('''INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,
        runtime_seq,execution_epoch,status,reply_id,payload)
        VALUES ('numeric','s','t',1,$1,1,'awaiting_interaction','reply',$2::jsonb)''',seq,json.dumps(snapshot))
    await consume(sessions)
    assert len(await cards(admin))==1


@pytest.mark.parametrize('change',[{'input':'{}'},{'suggested_rules':[{'tool_name':'write','rule_content':'*'}]}])
async def test_repeated_request_cannot_replace_original_input_or_rules(projection_database,change):
    sessions,admin=projection_database
    await request(sessions,admin)
    with pytest.raises(ProjectionConflict):
        await request(sessions,admin,[{**CALL,**change}])
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==2


async def test_decided_and_executed_request_is_not_marked_exhausted(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin)
    await park(admin)
    await consume(sessions)
    await decide(admin,[CALL])
    await source(sessions,admin,{'type':'TOOL_RESULT_END','tool_call_id':'a','state':'success'})
    await source(sessions,admin,{'type':'REPLY_END','finished_reason':'completed'})
    await park(admin,[],status='completed')
    await consume(sessions)
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type IN ('hitl_abandoned','hitl_resume_exhausted')")==0


async def test_terminal_replay_failure_also_rolls_back_interaction_closure(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin)
    await park(admin)
    await consume(sessions)
    await source(sessions,admin,{'type':'REPLY_END','finished_reason':'completed'})
    seq=await park(admin,[],status='completed')
    await admin.execute("ALTER TABLE product_events ADD CHECK (event_type <> 'RUN_FINISHED')")
    async with sessions.begin() as db:
        with pytest.raises(IntegrityError):
            await consume_next(db,'c',project=interaction_projector())
    assert await admin.fetchval('SELECT status FROM tasks')=='awaiting_permission'
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_abandoned'")==0
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq-1


async def test_all_decisions_before_projection_do_not_repark_a_running_task(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin)
    await decide(admin,[CALL])
    await park(admin)
    await consume(sessions)
    assert await cards(admin)==[]
    assert await admin.fetchval('SELECT status FROM tasks')=='running'


@pytest.mark.parametrize('external',[False,True])
async def test_real_core_requests_and_resume_project_the_remaining_interaction(projection_database,tmp_path,external):
    import os
    from pathlib import Path
    import subprocess
    core_python=os.getenv('WORKMATE_TEST_CORE_PYTHON')
    if not core_python:
        pytest.skip('requires the isolated Core test interpreter')
    root=Path(__file__).resolve().parents[2]
    target=tmp_path/'trace.json'
    paths=[str(root/p) for p in ('runtime','runtime/tests','packages/contracts/src')]
    # The Core fixture imports pytest; preserve the selected interpreter's
    # explicitly supplied test dependencies (e.g. read-only container mounts).
    inherited=os.environ.get('PYTHONPATH')
    if inherited:
        paths.append(inherited)
    env={**os.environ,'PYTHONPATH':os.pathsep.join(paths)}
    result=subprocess.run([core_python,str(root/'runtime/tests/export_permission_trace.py'),str(target),*(['external'] if external else [])],
                          env=env,check=False,timeout=30,capture_output=True,text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    initial,partial=json.loads(target.read_text())
    sessions,admin=projection_database
    reply=initial['events'][0]['reply_id']
    await admin.execute('UPDATE runtime_sessions SET reply_id=$1',reply)
    await admin.execute('UPDATE runtime_task_executions SET reply_id=$1',reply)
    async def consume_trace(trace,revision):
        for raw in trace['events']:
            await source(sessions,admin,raw)
        seq=(await admin.fetchval('SELECT runtime_seq FROM runtime_sessions'))+1
        pending=[call['id'] for call in trace['evidence']['pending_tools']]
        await emit(admin,seq,{'type':'EXECUTION_BOUNDARY','reply_id':reply,'status':'awaiting_interaction',
            'pending_tool_call_ids':pending,'display_empty':True,'boundary_evidence':trace['evidence']})
        await admin.execute('''INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,
            runtime_seq,execution_epoch,status,reply_id,payload)
            VALUES ($1,'s','t',$2,$3,1,'awaiting_interaction',$4,$5::jsonb)''',
            str(seq),revision,seq,reply,json.dumps(trace['snapshot']))
        await consume(sessions)
    assert sum(e['type']=='REQUIRE_USER_CONFIRM' for e in initial['events'])==(1 if external else 2)
    await consume_trace(initial,1)
    first=(await cards(admin))[0]['value']
    assert {call['id'] for call in first['toolCalls']}==({'a'} if external else {'a','b'})
    await decide(admin,[CALL])
    await consume_trace(partial,2)
    if external:
        assert len(await cards(admin))==1
        state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
        assert state['pending_interactions'][0]['kind']=='external_execution'
        assert await admin.fetchval('SELECT status FROM tasks')=='running'
    else:
        remaining=(await cards(admin))[-1]['value']
        assert remaining['decisionVersion']==first['decisionVersion']
        assert [call['id'] for call in remaining['toolCalls']]==['b']


def test_product_interaction_projection_imports_without_agentscope():
    import os
    import subprocess
    import sys
    script='''
import sys
class DenyEngine:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'agentscope' or fullname.startswith('agentscope.'):
            raise AssertionError('Product API imported AgentScope')
sys.meta_path.insert(0,DenyEngine())
from app.adapter.durable_interactions import interaction_projector
assert callable(interaction_projector())
'''
    subprocess.run([sys.executable,'-c',script],env=os.environ.copy(),check=True,capture_output=True,timeout=20)


async def test_approved_call_can_transition_to_external_execution_without_new_permission(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin)
    await park(admin)
    await consume(sessions)
    await decide(admin,[CALL])
    external={**CALL,'state':'submitted'}
    await request(sessions,admin,[external],kind='REQUIRE_EXTERNAL_EXECUTION')
    await park(admin,[external])
    await consume(sessions)
    assert len(await cards(admin))==1
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    assert state['pending_interactions'][0]['kind']=='external_execution'
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE payload->>'name'='HITL_DECISION_RECORDED'")==1


@pytest.mark.parametrize('denied',[False,True])
async def test_unapproved_permission_cannot_be_relabelled_as_external(projection_database,denied):
    sessions,admin=projection_database
    await request(sessions,admin)
    if denied:
        await admin.execute("INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload) VALUES ('t','c',2,'hitl_decision',$1::json)",
            json.dumps({'decisions':[{'toolCallId':'a','approved':False}]}))
    with pytest.raises(ProjectionConflict):
        await request(sessions,admin,[{**CALL,'state':'submitted'}],kind='REQUIRE_EXTERNAL_EXECUTION')
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==2


@pytest.mark.parametrize('when',['before_stage','before_boundary','denied_before_boundary'])
async def test_closed_or_revoked_permission_cannot_revive_as_external(projection_database,when):
    sessions,admin=projection_database
    await request(sessions,admin)
    await park(admin)
    await consume(sessions)
    await decide(admin,[CALL])
    external={**CALL,'state':'submitted'}
    if when!='before_stage':
        await request(sessions,admin,[external],kind='REQUIRE_EXTERNAL_EXECUTION')
    payload=({'decisions':[{'toolCallId':'a','approved':False}]} if when=='denied_before_boundary'
             else {'decisionVersion':approval_request_version('reply',[CALL])})
    kind='hitl_decision' if when=='denied_before_boundary' else 'hitl_resume_exhausted'
    await admin.execute('''INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
        SELECT 't','c',coalesce(max(seq),0)+1,$1,$2::json FROM task_events WHERE task_id='t' ''',kind,json.dumps(payload))
    with pytest.raises(ProjectionConflict):
        if when=='before_stage':
            await request(sessions,admin,[external],kind='REQUIRE_EXTERNAL_EXECUTION')
        else:
            await park(admin,[external])
            await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='awaiting_permission'
    state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    assert all(p['kind']=='permission' for p in state['pending_interactions'])


@pytest.mark.parametrize("profile", ["strict", "full_access"])
async def test_live_card_carries_task_and_effective_profile(projection_database, profile):
    sessions, admin = projection_database
    await request(sessions, admin)
    # Exercise current profile at the parked boundary, after initial activation.
    await admin.execute("UPDATE tasks SET approval_profile=$1", profile)
    await park(admin)
    await consume(sessions)
    card = (await cards(admin))[0]["value"]
    assert card["taskId"] == "t"
    assert card["approvalProfile"] == profile


async def save_approval_display(sessions, admin, *, version=1, body='Plan', phase='approval'):
    from workmate_contracts.realtime import MessageSnapshot, MessageSnapshotReference, ContentBlock, message_id
    from workmate_product.application.message_snapshots import save_message_snapshot
    await admin.execute("UPDATE runtime_sessions SET owner_id='owner',lease_until=clock_timestamp()+interval '60 seconds'")
    snapshot = MessageSnapshot(message_id=message_id('t','reply'), conversation_id='c', task_id='t',
        reply_id='reply', execution_epoch=1, snapshot_version=version,
        blocks=[ContentBlock(block_id='b',kind='text',text=body,content_version=version,closed=phase!='approval')],
        business_seq=0, phase=phase, incomplete=False)
    async with sessions.begin() as db:
        await save_message_snapshot(db, snapshot, owner_id='owner')
    return MessageSnapshotReference.from_snapshot(snapshot).model_dump(mode='json')


async def test_approval_card_uses_safe_boundary_cut_not_earlier_request_reference(projection_database):
    sessions, admin = projection_database
    await source(sessions, admin, {'type':'REPLY_START','session_id':'s'})
    earlier = await save_approval_display(sessions, admin)
    latest = await save_approval_display(sessions, admin, version=2, body='Plan revised')
    # Delivery/projection may lag behind the Worker. Staging this old reference
    # must not require the mutable message row to still be version 1.
    seq = await source(sessions, admin, {'type':'REQUIRE_USER_CONFIRM',
        'tool_calls':[CALL], 'display_snapshot':earlier})
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == seq
    assert await cards(admin) == []
    await park(admin, display=latest)
    await consume(sessions)
    card = (await cards(admin))[0]
    assert card['display_snapshot'] == latest
    assert card['display_snapshot'] != earlier
    assert 'display_snapshot' not in card['value']
    assert card['value']['toolCalls'] == [CALL]


async def test_cancelling_task_skips_obsolete_approval_cut_when_no_card_is_published(projection_database):
    sessions, admin = projection_database
    await source(sessions, admin, {'type':'REPLY_START','session_id':'s'})
    earlier = await save_approval_display(sessions, admin)
    await source(sessions, admin, {'type':'REQUIRE_USER_CONFIRM',
        'tool_calls':[CALL], 'display_snapshot':earlier})
    seq = await park(admin, display=earlier)
    await save_approval_display(sessions, admin, version=2, phase='interrupted')
    await admin.execute("UPDATE tasks SET status='cancelling'")
    await consume(sessions)
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == seq
    assert await admin.fetchval('SELECT status FROM tasks') == 'cancelling'
    assert await cards(admin) == []
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_request'") == 0


@pytest.mark.parametrize('corruption', ['version', 'digest', 'identity', 'epoch'])
async def test_wrong_final_approval_reference_rolls_back_card_and_audit(projection_database, corruption):
    from workmate_contracts.realtime import message_id
    sessions, admin = projection_database
    await request(sessions, admin)
    reference = await save_approval_display(sessions, admin)
    if corruption == 'version':
        reference['snapshot_version'] += 1
    elif corruption == 'digest':
        reference['sha256'] = 'f' * 64
    elif corruption == 'identity':
        reference['message_id'] = message_id('other', 'reply')
    else:
        reference['execution_epoch'] = 2
    seq = await park(admin, display=reference)
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await cards(admin) == []
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == seq - 1
    assert await admin.fetchval('SELECT status FROM tasks') == 'running'
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_request'") == 0


async def test_approval_without_display_proof_keeps_cursor_before_boundary(projection_database):
    sessions,admin=projection_database
    await request(sessions,admin)
    seq=await park(admin,missing_display=True)
    with pytest.raises(ProjectionConflict,match='display'):
        await consume(sessions)
    assert await cards(admin)==[]
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq-1
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
