"""Independent conversation readers and explicit log-loss handling on local Redis."""
import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import NoScriptError, TimeoutError as RedisTimeoutError, ConnectionError as RedisConnectionError

from workmate_contracts.realtime import RealtimeCursor, RealtimeEnvelope
from workmate_product.realtime.redis_log import RedisConversationLog, LogLost, ReplayGap, FutureCursor


@pytest_asyncio.fixture
async def log():
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url: pytest.skip('requires explicitly configured local Redis')
    client=Redis.from_url(url,decode_responses=True,retry=Retry(NoBackoff(),0),retry_on_timeout=False)
    value=RedisConversationLog(client,namespace='realtime-test-'+uuid4().hex,
                              retention_ms=60000,max_bytes=4096,ttl_seconds=120)
    try:
        yield value
    finally:
        keys=[key async for key in client.scan_iter(match=f'workmate:{value.namespace}:*')]
        if keys: await client.delete(*keys)
        await client.aclose()


def event(index=1,body='你好🙂'):
    return RealtimeEnvelope(event_id=f'event-{index}',conversation_id='conversation',source='business',
                            business_seq=index,payload={'type':'CUSTOM','text':body})


def origin(ref):
    return RealtimeCursor(conversation_id=ref.conversation_id,generation=ref.generation,stream_id='0-0')


async def test_initialization_and_two_independent_readers(log):
    generation=str(uuid4())
    ref=await log.initialize('conversation',generation)
    assert await log.initialize('conversation',generation)==ref
    assert (await log.read(ref,origin(ref))).entries==()
    appended=await log.append(ref,event())
    a,b=await asyncio.gather(log.read(ref,origin(ref)),log.read(ref,origin(ref)))
    assert a==b and len(a.entries)==1
    assert a.entries[0].envelope==event() and a.entries[0].cursor==appended.cursor
    assert a.window.payload_bytes==len(event().model_dump_json().encode())
    assert await log.client.xinfo_groups(log.keys(ref.conversation_id,ref.generation)[1])==[]


async def test_tail_wakes_both_readers_and_revalidates(log):
    ref=await log.initialize('conversation',str(uuid4()))
    readers=[asyncio.create_task(log.tail(ref,origin(ref),block_ms=1000)) for _ in range(2)]
    await asyncio.sleep(.02)
    await log.append(ref,event())
    pages=await asyncio.gather(*readers)
    assert all(page.entries[0].envelope.event_id=='event-1' for page in pages)


@pytest.mark.parametrize('which',[0,1])
async def test_partial_key_loss_never_reinitializes_or_appends(log,which):
    ref=await log.initialize('conversation',str(uuid4()))
    await log.client.delete(log.keys(ref.conversation_id,ref.generation)[which])
    with pytest.raises(LogLost): await log.initialize(ref.conversation_id,ref.generation)
    with pytest.raises(LogLost): await log.append(ref,event())
    with pytest.raises(LogLost): await log.read(ref,origin(ref))


async def test_complete_loss_new_nonce_does_not_authorize_old_reference(log):
    ref=await log.initialize('conversation',str(uuid4()))
    await log.client.delete(*log.keys(ref.conversation_id,ref.generation))
    fresh=await log.initialize(ref.conversation_id,ref.generation)
    assert fresh.incarnation!=ref.incarnation
    with pytest.raises(LogLost): await log.append(ref,event())
    with pytest.raises(LogLost): await log.read(ref,origin(ref))
    assert (await log.append(fresh,event())).cursor.generation==fresh.generation


async def test_exact_byte_retention_floor_and_future_cursor(log):
    one=len(event().model_dump_json().encode())
    log.max_bytes=one*2
    ref=await log.initialize('conversation',str(uuid4()))
    first=await log.append(ref,event(1))
    second=await log.append(ref,event(2))
    third=await log.append(ref,event(3))
    assert third.window.floor==first.cursor
    assert third.window.payload_bytes==one*2
    with pytest.raises(ReplayGap): await log.read(ref,origin(ref))
    assert [entry.envelope.event_id for entry in (await log.read(ref,first.cursor)).entries]==['event-2','event-3']
    future=RealtimeCursor(**{**third.cursor.model_dump(),'stream_id':'18446744073709551615-0'})
    with pytest.raises(FutureCursor): await log.read(ref,future)
    assert (await log.read(ref,second.cursor)).entries[0].cursor==third.cursor


async def test_age_retention_empty_stream_preserves_loss_detection(log):
    log.retention_ms=1
    ref=await log.initialize('conversation',str(uuid4()))
    first=await log.append(ref,event())
    await asyncio.sleep(.01)
    window=await log.window(ref)
    assert window.payload_bytes==0 and window.floor==first.cursor
    assert (await log.read(ref,first.cursor)).entries==()
    with pytest.raises(ReplayGap): await log.read(ref,origin(ref))
    assert await log.client.exists(*log.keys(ref.conversation_id,ref.generation))==2


async def test_response_loss_duplicate_preserves_stable_identity(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    original=log.client.evalsha
    failed=False
    calls=0
    async def lose_response(*args,**kwargs):
        nonlocal failed,calls
        calls+=1
        result=await original(*args,**kwargs)
        if not failed:
            failed=True
            raise ConnectionError('response lost')
        return result
    monkeypatch.setattr(log.client,'evalsha',lose_response)
    with pytest.raises(ConnectionError): await log.append(ref,event())
    assert calls==1
    assert (await log.window(ref)).last_ordinal==1
    await log.append(ref,event())
    page=await log.read(ref,origin(ref))
    assert [entry.envelope.event_id for entry in page.entries]==['event-1','event-1']


async def test_warm_script_cache_does_not_reload(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    async def unexpected_load(*args,**kwargs):
        pytest.fail('A warm script must only send its SHA')
    async def unexpected_eval(*args,**kwargs):
        pytest.fail('Conversation operations must not send the complete Lua script')
    monkeypatch.setattr(log.client,'script_load',unexpected_load)
    monkeypatch.setattr(log.client,'eval',unexpected_eval)
    await log.append(ref,event())
    assert (await log.read(ref,origin(ref))).entries[0].envelope==event()
    assert (await log.window(ref)).last_ordinal==1


async def test_client_replacement_uses_current_transport(log):
    ref=await log.initialize('conversation',str(uuid4()))
    original=log.client
    unavailable=Redis(unix_socket_path='/tmp/workmate-no-redis-'+uuid4().hex,
                      decode_responses=True,retry=Retry(NoBackoff(),0))
    log.client=unavailable
    try:
        with pytest.raises(RedisConnectionError): await log.append(ref,event())
    finally:
        log.client=original
        await unavailable.aclose()
    assert (await log.window(ref)).last_ordinal==0


@pytest.mark.parametrize('persistent',[False,True])
async def test_noscript_loads_once_and_reexecutes_only_once(log,monkeypatch,persistent):
    ref=await log.initialize('conversation',str(uuid4()))
    original_evalsha,original_load=log.client.evalsha,log.client.script_load
    executions,loads=0,0
    async def missing(*args,**kwargs):
        nonlocal executions
        executions+=1
        if executions==1 or persistent: raise NoScriptError('Script is not cached')
        return await original_evalsha(*args,**kwargs)
    async def measured_load(*args,**kwargs):
        nonlocal loads
        loads+=1
        return await original_load(*args,**kwargs)
    monkeypatch.setattr(log.client,'evalsha',missing)
    monkeypatch.setattr(log.client,'script_load',measured_load)
    if persistent:
        with pytest.raises(NoScriptError): await log.append(ref,event())
    else:
        result=await log.append(ref,event())
        assert result.window.last_ordinal==1
    assert executions==2 and loads==1
    monkeypatch.setattr(log.client,'evalsha',original_evalsha)
    assert (await log.window(ref)).last_ordinal==(0 if persistent else 1)


async def test_script_load_timeout_does_not_retry_or_execute_append(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    original=log.client.evalsha
    executions,loads=0,0
    async def missing(*args,**kwargs):
        nonlocal executions
        executions+=1
        raise NoScriptError('Script is not cached')
    async def timed_out(*args,**kwargs):
        nonlocal loads
        loads+=1
        raise RedisTimeoutError('Script load response lost')
    monkeypatch.setattr(log.client,'evalsha',missing)
    monkeypatch.setattr(log.client,'script_load',timed_out)
    with pytest.raises(RedisTimeoutError): await log.append(ref,event())
    assert executions==1 and loads==1
    monkeypatch.setattr(log.client,'evalsha',original)
    assert (await log.window(ref)).last_ordinal==0


@pytest.mark.parametrize('failure',[RedisTimeoutError,RedisConnectionError,asyncio.CancelledError])
@pytest.mark.parametrize('cold',[False,True])
async def test_script_transport_failure_or_cancellation_is_not_retried(log,monkeypatch,failure,cold):
    ref=await log.initialize('conversation',str(uuid4()))
    original_load=log.client.script_load
    executions,loads=0,0
    async def fail(*args,**kwargs):
        nonlocal executions
        executions+=1
        if cold and executions==1: raise NoScriptError('Script is not cached')
        raise failure('Uncertain execution')
    async def measured_load(*args,**kwargs):
        nonlocal loads
        loads+=1
        if not cold: pytest.fail('Transport failure cannot prove script absence')
        return await original_load(*args,**kwargs)
    monkeypatch.setattr(log.client,'evalsha',fail)
    monkeypatch.setattr(log.client,'script_load',measured_load)
    with pytest.raises(failure): await log.append(ref,event())
    assert executions==(2 if cold else 1) and loads==(1 if cold else 0)


async def test_corrupted_metadata_or_external_stream_trim_is_log_lost(log):
    ref=await log.initialize('conversation',str(uuid4()))
    await log.append(ref,event())
    meta,stream=log.keys(ref.conversation_id,ref.generation)
    await log.client.xtrim(stream,maxlen=0)
    with pytest.raises(LogLost): await log.window(ref)


async def test_oversized_or_foreign_event_rejected_before_append(log):
    ref=await log.initialize('conversation',str(uuid4()))
    with pytest.raises(ValueError): await log.append(ref,event(body='x'*5000))
    with pytest.raises(ValueError): await log.append(ref,event().model_copy(update={'conversation_id':'other'}))
    assert (await log.window(ref)).payload_bytes==0


def test_nonzero_automatic_retry_and_unsafe_bounds_are_rejected():
    client=Redis(retry=Retry(NoBackoff(),1))
    with pytest.raises(ValueError,match='retry'): RedisConversationLog(client,namespace='test')
    client=Redis(retry=Retry(NoBackoff(),0))
    # Redis8 ignores the deprecated constructor flag; emulate effective older-client setting.
    client.connection_pool.connection_kwargs['retry_on_timeout']=True
    with pytest.raises(ValueError,match='retry'): RedisConversationLog(client,namespace='test')


async def test_older_delayed_initializer_cannot_recreate_confirmed_incarnation(log,monkeypatch):
    original=log.client.evalsha
    entered,release=asyncio.Event(),asyncio.Event()
    delayed=True
    async def delay_first(*args,**kwargs):
        nonlocal delayed
        if delayed:
            delayed=False
            entered.set()
            await release.wait()
        return await original(*args,**kwargs)
    monkeypatch.setattr(log.client,'evalsha',delay_first)
    generation=str(uuid4())
    older=asyncio.create_task(log.initialize('conversation',generation))
    await entered.wait()
    confirmed=await log.initialize('conversation',generation)
    await log.client.delete(*log.keys('conversation',generation))
    release.set()
    orphan=await older
    assert orphan.incarnation!=confirmed.incarnation
    with pytest.raises(LogLost): await log.append(confirmed,event())


async def test_tail_never_exposes_raw_page_after_incarnation_loss(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    original=log.client.xread
    async def lose_after_wakeup(*args,**kwargs):
        await log.append(ref,event())
        result=await original(*args,**kwargs)
        await log.client.delete(*log.keys(ref.conversation_id,ref.generation))
        await log.initialize(ref.conversation_id,ref.generation)
        return result
    monkeypatch.setattr(log.client,'xread',lose_after_wakeup)
    with pytest.raises(LogLost): await log.tail(ref,origin(ref),block_ms=10)


async def test_tail_detects_concurrent_trim_after_wakeup(log,monkeypatch):
    log.max_bytes=len(event().model_dump_json().encode())
    ref=await log.initialize('conversation',str(uuid4()))
    original=log.client.xread
    async def trim_after_wakeup(*args,**kwargs):
        await log.append(ref,event())
        result=await original(*args,**kwargs)
        await log.append(ref,event(2))
        return result
    monkeypatch.setattr(log.client,'xread',trim_after_wakeup)
    with pytest.raises(ReplayGap): await log.tail(ref,origin(ref),block_ms=10)


async def test_unretained_in_between_position_is_gap_not_silent_resume(log):
    ref=await log.initialize('conversation',str(uuid4()))
    await log.append(ref,event())
    fabricated=RealtimeCursor(**{**origin(ref).model_dump(),'stream_id':'1-999'})
    with pytest.raises(ReplayGap): await log.read(ref,fabricated)


async def test_internal_ordinal_hole_cannot_hide_behind_matching_count(log):
    ref=await log.initialize('conversation',str(uuid4()))
    first=await log.append(ref,event())
    second=await log.append(ref,event(2))
    third=await log.append(ref,event(3))
    meta,stream=log.keys(ref.conversation_id,ref.generation)
    await log.client.xdel(stream,second.cursor.stream_id)
    # Even matching count/tail is insufficient proof: ordinal continuity rejects the hole.
    await log.client.hset(meta,'count',2)
    with pytest.raises(LogLost): await log.read(ref,first.cursor)


async def test_large_numeric_stream_id_and_exact_floor_boundary(log):
    ref=await log.initialize('conversation',str(uuid4()))
    first=await log.append(ref,event())
    meta,stream=log.keys(ref.conversation_id,ref.generation)
    high='18446744073709551615-18446744073709551614'
    body=event(2).model_dump_json()
    await log.client.xadd(stream,{'payload':body,'ordinal':'2','bytes':str(len(body.encode()))},id=high)
    await log.client.hset(meta,mapping={'tail':high,'ordinal':2,'count':2,'bytes':len(body.encode())*2})
    result=await log.read(ref,first.cursor)
    assert result.entries[0].cursor.stream_id==high
    assert result.window.last.stream_id==high


@pytest.mark.parametrize('mutation',[{'bytes':'bad'}, {'incarnation':'bad'}, {'count':'9'}, {'conversation':'other'}])
async def test_corrupt_metadata_is_log_lost(log,mutation):
    ref=await log.initialize('conversation',str(uuid4()))
    await log.client.hset(log.keys(ref.conversation_id,ref.generation)[0],mapping=mutation)
    with pytest.raises(LogLost): await log.window(ref)


@pytest.mark.parametrize('kwargs',[{'retention_ms':0},{'retention_ms':True},{'max_bytes':0},
    {'ttl_seconds':0},{'ttl_seconds':1,'retention_ms':2000},{'namespace':'bad:key'}])
def test_policy_bounds(kwargs):
    client=Redis(retry=Retry(NoBackoff(),0))
    with pytest.raises(ValueError): RedisConversationLog(client,**({'namespace':'test'}|kwargs))


async def test_empty_window_cursor_can_be_used_for_tail(log):
    ref=await log.initialize('conversation',str(uuid4()))
    window=await log.window(ref)
    assert window.last.stream_id=='0-0'
    assert (await log.read(ref,window.last)).entries==()


async def test_invalid_floor_metadata_reports_log_lost(log):
    ref=await log.initialize('conversation',str(uuid4()))
    await log.client.hset(log.keys(ref.conversation_id,ref.generation)[0],'floor','invalid')
    with pytest.raises(LogLost): await log.window(ref)


async def test_binary_client_namespace_and_ttl_refresh(log):
    async with Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'],decode_responses=False,
                              retry=Retry(NoBackoff(),0)) as client:
        binary=RedisConversationLog(client,namespace=log.namespace,retention_ms=60000,max_bytes=4096,ttl_seconds=120)
        ref=await binary.initialize('conversation',str(uuid4()))
        appended=await binary.append(ref,event())
        assert (await binary.read(ref,origin(ref))).entries[0].cursor==appended.cursor
        other=RedisConversationLog(client,namespace=log.namespace+'-other')
        with pytest.raises(LogLost): await other.window(ref)
        meta,stream=binary.keys(ref.conversation_id,ref.generation)
        await client.pexpire(meta,500)
        await client.pexpire(stream,500)
        await binary.window(ref)
        assert 119000<=await client.pttl(meta)<=120000
        assert 119000<=await client.pttl(stream)<=120000


async def seed_expired(log, ref, count):
    """Build a coherent old log to exercise retention without sleeping per event."""
    meta,stream=log.keys(ref.conversation_id,ref.generation)
    body=event().model_dump_json()
    size=len(body.encode())
    async with log.client.pipeline(transaction=True) as pipe:
        for ordinal in range(1,count+1):
            pipe.xadd(stream,{'payload':body,'ordinal':str(ordinal),'bytes':str(size)},id=f'1-{ordinal}')
        pipe.hset(meta,mapping={'tail':f'1-{count}','count':count+1,'ordinal':count,'bytes':count*size})
        await pipe.execute()
    return size


async def test_retention_is_chunked_with_committed_floor_before_each_yield(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    await seed_expired(log,ref,150)
    original=log.client.evalsha
    chunks=[]
    meta,stream=log.keys(ref.conversation_id,ref.generation)
    async def measured(*args,**kwargs):
        before=await log.client.xlen(stream)
        result=await original(*args,**kwargs)
        after=await log.client.xlen(stream)
        chunks.append(before-after)
        assert before-after<=64
        assert int(await log.client.hget(meta,'count'))==after
        return result
    monkeypatch.setattr(log.client,'evalsha',measured)
    window=await log.window(ref)
    assert len(chunks)>=3 and window.floor_ordinal==150 and window.payload_bytes==0
    assert (await log.read(ref,window.floor)).entries==()
    with pytest.raises(ReplayGap): await log.read(ref,origin(ref))


async def test_append_cleanup_continuations_do_not_duplicate_or_overfill(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    size=await seed_expired(log,ref,150)
    log.max_bytes=size*2
    original=log.client.evalsha
    calls=[]
    async def measured(*args,**kwargs):
        result=await original(*args,**kwargs)
        calls.append(result[0])
        return result
    monkeypatch.setattr(log.client,'evalsha',measured)
    appended=await log.append(ref,event(2))
    assert len(calls)>=3
    assert appended.window.last_ordinal==151 and appended.window.floor_ordinal==150
    assert appended.window.payload_bytes==size
    assert [item.envelope.event_id for item in (await log.read(ref,appended.window.floor)).entries]==['event-2']


async def test_cancelled_retention_slice_is_resumable_not_complete(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    await seed_expired(log,ref,150)
    original=log.client.evalsha
    stop=asyncio.Event()
    async def cancel_after_first_slice(*args,**kwargs):
        result=await original(*args,**kwargs)
        stop.set()
        await asyncio.Future()
        return result
    monkeypatch.setattr(log.client,'evalsha',cancel_after_first_slice)
    cleaning=asyncio.create_task(log.window(ref))
    await stop.wait()
    cleaning.cancel()
    with pytest.raises(asyncio.CancelledError): await cleaning
    meta,stream=log.keys(ref.conversation_id,ref.generation)
    assert 0<await log.client.xlen(stream)<151
    monkeypatch.setattr(log.client,'evalsha',original)
    with pytest.raises(ReplayGap): await log.read(ref,origin(ref))
    final=await log.window(ref)
    assert final.floor_ordinal==150 and final.payload_bytes==0


async def test_maintenance_continuation_rechecks_automatic_retry_policy(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    await seed_expired(log,ref,150)
    original=log.client.evalsha
    calls=0
    async def mutate_after_slice(*args,**kwargs):
        nonlocal calls
        calls+=1
        result=await original(*args,**kwargs)
        assert result[0]=='maintenance_pending'
        log.client.connection_pool.connection_kwargs['retry_on_timeout']=True
        return result
    monkeypatch.setattr(log.client,'evalsha',mutate_after_slice)
    try:
        with pytest.raises(ValueError,match='retry'): await log.append(ref,event(2))
        assert calls==1
    finally:
        log.client.connection_pool.connection_kwargs['retry_on_timeout']=False
    monkeypatch.setattr(log.client,'evalsha',original)
    result=await log.window(ref)
    assert result.last_ordinal==150 and result.payload_bytes==0
