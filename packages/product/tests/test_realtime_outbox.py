"""Business notification intents commit with facts and use fenced publication leases."""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from workmate_contracts import ProductEvent
from workmate_product.persistence.repositories import realtime_outbox as repository
from workmate_product.realtime.classification import is_business_event
from test_execution_context import database

pytestmark=pytest.mark.asyncio


def event(*, seq=1, payload=None):
    payload=payload or {'type':'CUSTOM','name':'TASK_COMPLETED','value':{'taskStatus':'completed'}}
    return ProductEvent(product_event_id=str(uuid4()),conversation_id='c',product_seq=seq,
        product_task_id='t',reply_id='reply',event_type=payload['type'],
        occurred_at=datetime.now(timezone.utc),payload=payload)


async def insert(db,value):
    import json
    await db.execute(text('''INSERT INTO product_events(id,conversation_id,product_seq,business_seq,task_id,reply_id,
        projection_item_key,event_type,occurred_at,payload)
        VALUES (:id,:cid,:seq,:seq,:tid,:reply,:item,:type,:at,CAST(:payload AS jsonb))'''),
        dict(id=value.product_event_id,cid=value.conversation_id,seq=value.product_seq,
            tid=value.product_task_id,reply=value.reply_id,item=str(value.product_seq),type=value.event_type,
            at=value.occurred_at,payload=json.dumps(value.payload)))


async def stage(sessions,*,seq=1,payload=None):
    value=event(seq=seq,payload=payload)
    async with sessions.begin() as db:
        await insert(db,value)
        assert await repository.stage(db,value,business_seq=seq)
    return value


async def lease(sessions,**kwargs):
    async with sessions.begin() as db:
        return await repository.lease_due(db,**kwargs)


async def test_intent_rollback_and_repeated_stage_bind_exact_fact(database):
    sessions,admin=database
    value=event()
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            await insert(db,value)
            await repository.stage(db,value,business_seq=1)
            assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox')==0
            raise RuntimeError('rollback')
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox')==0
    async with sessions.begin() as db:
        await insert(db,value)
        assert await repository.stage(db,value,business_seq=1)
        assert not await repository.stage(db,value,business_seq=1)
        with pytest.raises(ValueError): await repository.stage(db,value,business_seq=2)
        with pytest.raises(ValueError):
            await repository.stage(db,value.model_copy(update={'payload':{'type':'RUN_ERROR'}}),business_seq=1)
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox')==1


@pytest.mark.parametrize('payload',[
    {'type':kind} for kind in ['TEXT_MESSAGE_START','TEXT_MESSAGE_CONTENT','TEXT_MESSAGE_END',
        'REASONING_MESSAGE_START','REASONING_MESSAGE_CONTENT','REASONING_MESSAGE_END']
]+[{'type':'CUSTOM','name':name} for name in ['REASONING_DELTA']])
async def test_ephemeral_content_never_creates_business_outbox(database,payload):
    sessions,admin=database
    value=event(payload=payload)
    assert not is_business_event(value.event_type,value.payload)
    async with sessions.begin() as db:
        await insert(db,value)
        with pytest.raises(ValueError,match='ephemeral'):
            await repository.stage(db,value,business_seq=1)
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox')==0


@pytest.mark.parametrize('payload',[
    {'type':kind} for kind in ['RUN_STARTED','RUN_FINISHED','RUN_ERROR','RUN_CANCELLED','TOOL_CALL_START','TOOL_CALL_RESULT',
        'TOOL_CALL_ARGS','STEP_STARTED','STEP_FINISHED']
]+[{'type':'CUSTOM','name':name} for name in ['HITL_APPROVAL_REQUEST','ARTIFACT_CREATED','new_extension','TOOL_RESULT_DELTA']])
async def test_reliable_public_facts_include_unknown_extensions(payload):
    assert is_business_event(payload['type'],payload)


async def test_lease_skips_locked_and_fences_expired_or_superseded_owner(database):
    sessions,admin=database
    await stage(sessions)
    async with sessions.begin() as db:
        claim=(await repository.lease_due(db))[0]
        assert await lease(sessions)==[]
    assert claim.event.payload['name']=='TASK_COMPLETED'
    assert claim.event.product_seq==1 and claim.business_seq==1 and claim.attempt_count==1
    await admin.execute("UPDATE realtime_business_outbox SET lease_until=clock_timestamp()-interval '1 second'")
    async with sessions.begin() as db:
        assert not await repository.failed(db,claim,error_code='network',retry_seconds=0)
    fresh=(await lease(sessions))[0]
    assert fresh.claim_token!=claim.claim_token and fresh.attempt_count==2
    async with sessions.begin() as db:
        assert not await repository.failed(db,claim,error_code='late',retry_seconds=0)
        assert await repository.failed(db,fresh,error_code='protocol_invalid',blocked=True,retry_seconds=0)
    assert await lease(sessions)==[]
    assert await admin.fetchval('SELECT status FROM realtime_business_outbox')=='blocked'


async def test_publication_claim_bounds_require_real_caller_transaction(database):
    sessions,_=database
    async with sessions() as db:
        with pytest.raises(ValueError): await repository.lease_due(db)
    async with sessions.begin() as db:
        for args in ({'limit':True},{'limit':0},{'limit':1001},{'lease_seconds':0},{'lease_seconds':float('inf')},{'lease_seconds':10**500}):
            with pytest.raises(ValueError): await repository.lease_due(db,**args)


async def activate(admin):
    generation,incarnation=str(uuid4()),str(uuid4())
    await admin.execute('''INSERT INTO conversation_event_logs
        (conversation_id,generation,incarnation,version,status,initialization_token)
        VALUES ('c',$1,$2,1,'active',$3)''',generation,incarnation,str(uuid4()))
    return generation,incarnation


async def test_ordered_conversation_claims_and_published_receipt_fences(database):
    sessions,admin=database
    first=await stage(sessions,seq=1);second=await stage(sessions,seq=2)
    generation,incarnation=await activate(admin)
    claims=await lease(sessions)
    assert len(claims)==1 and claims[0].event.product_event_id==first.product_event_id
    old=claims[0]
    assert await lease(sessions)==[]
    await admin.execute("UPDATE realtime_business_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE status='publishing'")
    fresh=(await lease(sessions))[0]
    async with sessions.begin() as db:
        assert not await repository.published(db,old,generation=generation,incarnation=incarnation,stream_id='1-0')
        assert not await repository.published(db,fresh,generation=str(uuid4()),incarnation=incarnation,stream_id='1-0')
        assert not await repository.published(db,fresh,generation=generation,incarnation=str(uuid4()),stream_id='1-0')
        assert await repository.published(db,fresh,generation=generation,incarnation=incarnation,stream_id='1-0')
        assert await admin.fetchval('SELECT status FROM realtime_business_outbox WHERE event_id=$1',first.product_event_id)=='publishing'
    following=await lease(sessions)
    assert len(following)==1 and following[0].event.product_event_id==second.product_event_id


async def test_receipt_generation_lock_prevents_rotation_before_commit(database):
    import asyncpg
    sessions,admin=database
    await stage(sessions);generation,incarnation=await activate(admin)
    claim=(await lease(sessions))[0]
    async with sessions.begin() as db:
        assert await repository.published(db,claim,generation=generation,incarnation=incarnation,stream_id='1-0')
        with pytest.raises(asyncpg.LockNotAvailableError):
            await admin.fetchrow("SELECT * FROM conversation_event_logs WHERE conversation_id='c' FOR UPDATE NOWAIT")
    assert await admin.fetchrow("SELECT * FROM conversation_event_logs WHERE conversation_id='c' FOR UPDATE NOWAIT")


async def test_blocked_head_retains_order_but_other_conversation_can_publish(database):
    from workmate_product.persistence.models import Conversation
    sessions,admin=database
    await stage(sessions,seq=1);await stage(sessions,seq=2)
    first=(await lease(sessions))[0]
    async with sessions.begin() as db:
        assert await repository.failed(db,first,error_code='protocol_invalid',blocked=True,retry_seconds=0)
        db.add(Conversation(id='c2',user_id='u',agent_id='a',model_profile='chat'))
        await db.flush()
        other=event().model_copy(update={'conversation_id':'c2','product_task_id':None})
        await insert(db,other)
        await repository.stage(db,other,business_seq=1)
    claims=await lease(sessions)
    assert len(claims)==1 and claims[0].event.conversation_id=='c2'
    assert await admin.fetchval("SELECT status FROM realtime_business_outbox WHERE conversation_id='c' AND business_seq=2")=='pending'


async def test_claim_rechecks_sanitized_fact_semantics_and_retains_bad_row(database):
    sessions,admin=database
    await stage(sessions)
    await admin.execute('''UPDATE product_events SET payload='{"type":"CUSTOM","name":"REASONING_DELTA"}'::jsonb''')
    assert await lease(sessions)==[]
    row=await admin.fetchrow('SELECT * FROM realtime_business_outbox')
    assert row['status']=='blocked' and row['error_code']=='protocol_invalid'
    assert row['claim_token'] is None and row['lease_until'] is None


async def test_stage_cannot_assign_new_sequence_to_unrelated_source(database):
    sessions,admin=database
    value=event()
    async with sessions.begin() as db:
        await insert(db,value)
        with pytest.raises(ValueError):await repository.stage(db,value,business_seq=2)
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox')==0


@pytest.mark.parametrize('payload',[{'type':'CUSTOM'},{'type':'CUSTOM','name':''},{'type':'CUSTOM','name':None}])
async def test_unnamed_legacy_custom_remains_durable(payload):
    assert is_business_event('CUSTOM',payload)


async def test_malformed_event_names_and_type_disagreement_rejected():
    for event_type,payload in [('CUSTOM',{'type':'CUSTOM','name':['name']}),
                               ('CUSTOM',{'type':'RUN_ERROR'}),('',{}),('CUSTOM',[])]:
        with pytest.raises(ValueError): is_business_event(event_type,payload)


async def test_business_event_identity_preserves_existing_128_character_contract(database):
    sessions,admin=database
    value=event().model_copy(update={'product_event_id':'e'*128})
    async with sessions.begin() as db:
        await insert(db,value)
        assert await repository.stage(db,value,business_seq=1)
        with pytest.raises(ValueError):
            await repository.stage(db,value.model_copy(update={'product_event_id':'e'*129}),business_seq=2)
    assert await admin.fetchval('SELECT event_id FROM realtime_business_outbox')=='e'*128


async def test_business_sequence_is_independent_of_legacy_product_replay_sequence(database):
    sessions,admin=database
    value=event(seq=500)
    async with sessions.begin() as db:
        await insert(db,value)
        await db.execute(text('UPDATE product_events SET business_seq=7 WHERE id=:id'),{'id':value.product_event_id})
        assert await repository.stage(db,value,business_seq=7)
    claim=(await lease(sessions))[0]
    assert claim.business_seq==7 and claim.event.product_seq==500
