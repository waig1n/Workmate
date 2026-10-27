"""Real Redis broadcast/reconnect tests for the bounded shared conversation reader."""
import asyncio
from uuid import uuid4

import pytest
from test_realtime_redis_log import log, event, origin  # noqa: F401
from workmate_contracts.realtime import RealtimeCursor
from workmate_product.realtime.redis_log import LogLost, ReplayGap, FutureCursor
from workmate_product.realtime.reader import (
    ConversationReaderHub, SlowSubscriber, ReaderClosed, GenerationMismatch,
)


async def next_entry(subscription):
    return await asyncio.wait_for(anext(subscription), 3)


async def subscribe_error(hub, ref, cursor, error):
    with pytest.raises(error):
        sub=await hub.subscribe(ref,cursor)
        try:
            await next_entry(sub)
        finally:
            await sub.aclose()


async def test_two_tabs_receive_all_events_and_reconnect_from_own_cursor(log):
    ref=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,block_ms=20)
    try:
        left=await hub.subscribe(ref,origin(ref))
        right=await hub.subscribe(ref,origin(ref))
        first=await log.append(ref,event(1))
        assert (await next_entry(left)).cursor==first.cursor
        assert (await next_entry(right)).cursor==first.cursor
        await left.aclose()
        second=await log.append(ref,event(2))
        assert (await next_entry(right)).cursor==second.cursor
        left=await hub.subscribe(ref,first.cursor)
        assert (await next_entry(left)).cursor==second.cursor
        third=await log.append(ref,event(3))
        assert (await next_entry(left)).cursor==third.cursor
        assert (await next_entry(right)).cursor==third.cursor
    finally:
        await hub.close()


async def test_each_tab_replays_its_own_cursor_with_multiple_pages(log):
    ref=await log.initialize('conversation',str(uuid4()))
    entries=[await log.append(ref,event(i)) for i in range(1,7)]
    hub=ConversationReaderHub(log,page_events=2,block_ms=20)
    try:
        left=await hub.subscribe(ref,entries[0].cursor)
        right=await hub.subscribe(ref,entries[3].cursor)
        assert [(await next_entry(left)).envelope.event_id for _ in range(5)]==[f'event-{i}' for i in range(2,7)]
        assert [(await next_entry(right)).envelope.event_id for _ in range(2)]==['event-5','event-6']
    finally:
        await hub.close()


async def test_replay_tail_race_neither_drops_nor_duplicates_cursor(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    await log.append(ref,event(1))
    original=log.read
    injected=False
    async def racing_read(*args,**kwargs):
        nonlocal injected
        page=await original(*args,**kwargs)
        if page.entries and not injected:
            injected=True
            await log.append(ref,event(2))
        return page
    monkeypatch.setattr(log,'read',racing_read)
    hub=ConversationReaderHub(log,page_events=1,block_ms=20)
    try:
        sub=await hub.subscribe(ref,origin(ref))
        assert (await next_entry(sub)).envelope.event_id=='event-1'
        assert (await next_entry(sub)).envelope.event_id=='event-2'
        await log.append(ref,event(3))
        assert (await next_entry(sub)).envelope.event_id=='event-3'
    finally:
        await hub.close()


async def test_only_slow_subscriber_is_evicted_by_event_budget(log):
    ref=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,page_events=1,buffer_events=2,block_ms=20)
    try:
        slow=await hub.subscribe(ref,origin(ref))
        fast=await hub.subscribe(ref,origin(ref))
        for i in range(1,5):
            await log.append(ref,event(i))
            assert (await next_entry(fast)).envelope.event_id==f'event-{i}'
        with pytest.raises(SlowSubscriber): await next_entry(slow)
        await log.append(ref,event(5))
        assert (await next_entry(fast)).envelope.event_id=='event-5'
    finally:
        await hub.close()


async def test_byte_budget_evicts_without_delivering_unbounded_payload(log):
    ref=await log.initialize('conversation',str(uuid4()))
    size=len(event().model_dump_json().encode())
    hub=ConversationReaderHub(log,buffer_bytes=size-1,block_ms=20)
    try:
        sub=await hub.subscribe(ref,origin(ref))
        await log.append(ref,event())
        with pytest.raises(SlowSubscriber): await next_entry(sub)
    finally:
        await hub.close()


async def test_shared_tail_stops_after_last_unsubscribe(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    entered=asyncio.Event()
    active=0
    peak=0
    original=log.tail
    async def observed(*args,**kwargs):
        nonlocal active,peak
        active+=1;peak=max(peak,active);entered.set()
        try: return await original(*args,**kwargs)
        finally: active-=1
    monkeypatch.setattr(log,'tail',observed)
    hub=ConversationReaderHub(log,block_ms=1000)
    try:
        one=await hub.subscribe(ref,origin(ref))
        two=await hub.subscribe(ref,origin(ref))
        await asyncio.wait_for(entered.wait(),3)
        await one.aclose()
        await two.aclose()
        assert active==0 and peak==1
        await log.append(ref,event())
        three=await hub.subscribe(ref,origin(ref))
        assert (await next_entry(three)).envelope.event_id=='event-1'
        await three.aclose()
    finally:
        await hub.close()


async def test_log_loss_reaches_all_subscribers_and_new_reference_can_restart(log):
    ref=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,block_ms=20)
    try:
        subs=[await hub.subscribe(ref,origin(ref)) for _ in range(2)]
        await log.client.delete(*log.keys(ref.conversation_id,ref.generation))
        for sub in subs:
            with pytest.raises(LogLost): await next_entry(sub)
        fresh=await log.initialize('conversation',str(uuid4()))
        sub=await hub.subscribe(fresh,origin(fresh))
        await log.append(fresh,event())
        assert (await next_entry(sub)).envelope.event_id=='event-1'
    finally:
        await hub.close()


async def test_trimmed_and_future_cursors_fail_explicitly(log):
    ref=await log.initialize('conversation',str(uuid4()))
    log.max_bytes=len(event().model_dump_json().encode())
    await log.append(ref,event(1));await log.append(ref,event(2))
    hub=ConversationReaderHub(log,block_ms=20)
    try:
        await subscribe_error(hub,ref,origin(ref),ReplayGap)
        future=RealtimeCursor(conversation_id='conversation',generation=ref.generation,stream_id='18446744073709551615-0')
        await subscribe_error(hub,ref,future,FutureCursor)
        foreign=RealtimeCursor(conversation_id='other',generation=ref.generation,stream_id='0-0')
        await subscribe_error(hub,ref,foreign,ValueError)
    finally:
        await hub.close()


async def test_hub_close_wakes_pending_consumers_and_rejects_new_subscriptions(log):
    ref=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,block_ms=1000)
    sub=await hub.subscribe(ref,origin(ref))
    pending=asyncio.create_task(next_entry(sub))
    await hub.close()
    with pytest.raises(ReaderClosed): await pending
    with pytest.raises(ReaderClosed): await hub.subscribe(ref,origin(ref))
    await sub.aclose()
    await hub.close()


async def test_last_overflowed_client_does_not_leave_a_background_tail(log):
    ref=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,buffer_events=1,block_ms=20)
    try:
        sub=await hub.subscribe(ref,origin(ref))
        task=hub._hubs['conversation'].task
        await log.append(ref,event(1))
        await log.append(ref,event(2))
        await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),1)
        with pytest.raises(SlowSubscriber): await next_entry(sub)
    finally:
        await hub.close()


async def test_generation_change_requires_explicit_exact_reference_invalidation(log):
    ref=await log.initialize('conversation',str(uuid4()))
    fresh=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,block_ms=20)
    try:
        old=await hub.subscribe(ref,origin(ref))
        with pytest.raises(GenerationMismatch):
            await hub.subscribe(fresh,origin(fresh))
        assert await hub.invalidate(ref)
        with pytest.raises(GenerationMismatch): await next_entry(old)
        current=await hub.subscribe(fresh,origin(fresh))
        assert not await hub.invalidate(ref)
        await log.append(fresh,event())
        assert (await next_entry(current)).envelope.event_id=='event-1'
    finally:
        await hub.close()


async def test_client_mutation_cannot_change_another_subscribers_payload(log):
    ref=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,block_ms=20)
    try:
        one=await hub.subscribe(ref,origin(ref))
        two=await hub.subscribe(ref,origin(ref))
        await log.append(ref,event())
        first=await next_entry(one)
        original=first.envelope.payload['type']
        first.envelope.payload['type']='client mutation'
        assert (await next_entry(two)).envelope.payload['type']==original
    finally:
        await hub.close()


async def test_trim_during_paginated_replay_is_not_hidden_by_tail_buffer(log):
    ref=await log.initialize('conversation',str(uuid4()))
    log.max_bytes=3*len(event().model_dump_json().encode())
    for i in range(1,4): await log.append(ref,event(i))
    hub=ConversationReaderHub(log,page_events=1,block_ms=20)
    try:
        sub=await hub.subscribe(ref,origin(ref))
        assert (await next_entry(sub)).envelope.event_id=='event-1'
        for i in range(4,6): await log.append(ref,event(i))
        with pytest.raises(ReplayGap): await next_entry(sub)
    finally:
        await hub.close()


async def test_cancelled_explicit_close_still_removes_subscriber_and_joins_tail(log):
    ref=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,block_ms=1000)
    sub=await hub.subscribe(ref,origin(ref))
    task=hub._hubs['conversation'].task
    try:
        async with hub._lock:
            closing=asyncio.create_task(sub.aclose())
            await asyncio.sleep(0)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError): await closing
        await sub.aclose()
        await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),1)
        assert not hub._hubs
    finally:
        await hub.close()


async def test_cancelled_handshake_releases_its_registered_buffer(log,monkeypatch):
    ref=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,block_ms=1000)
    entered=asyncio.Event()
    original=log.read
    async def blocked(ref,after,**kwargs):
        if kwargs['count']==hub.replay_events:
            entered.set()
            await asyncio.Event().wait()
        return await original(ref,after,**kwargs)
    monkeypatch.setattr(log,'read',blocked)
    opening=asyncio.create_task(hub.subscribe(ref,origin(ref)))
    try:
        await asyncio.wait_for(entered.wait(),1)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError): await opening
        assert not hub._hubs
    finally:
        await hub.close()


async def test_subscriber_requires_one_consumer_but_does_not_close_the_first(log):
    ref=await log.initialize('conversation',str(uuid4()))
    hub=ConversationReaderHub(log,block_ms=20)
    try:
        sub=await hub.subscribe(ref,origin(ref))
        first=asyncio.create_task(anext(sub))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError,match='single consumer'): await anext(sub)
        await log.append(ref,event())
        assert (await asyncio.wait_for(first,1)).envelope.event_id=='event-1'
    finally:
        await hub.close()


async def test_two_independent_gateway_hubs_broadcast_the_same_log(log):
    ref=await log.initialize('conversation',str(uuid4()))
    hubs=[ConversationReaderHub(log,block_ms=20) for _ in range(2)]
    try:
        subs=[await hub.subscribe(ref,origin(ref)) for hub in hubs]
        for i in range(1,4):
            await log.append(ref,event(i))
            assert [entry.envelope.event_id for entry in
                await asyncio.gather(*(next_entry(sub) for sub in subs))]==[f'event-{i}']*2
    finally:
        await asyncio.gather(*(hub.close() for hub in hubs))


async def test_reader_capacity_counts_conversations_and_releases_after_last_tab(log):
    from workmate_product.realtime.reader import ReaderCapacityExceeded
    ref = await log.initialize('conversation', str(uuid4()))
    other = await log.initialize('other', str(uuid4()))
    hub = ConversationReaderHub(log, block_ms=20, max_readers=1)
    try:
        first = await hub.subscribe(ref, origin(ref))
        second = await hub.subscribe(ref, origin(ref))
        with pytest.raises(ReaderCapacityExceeded):
            await hub.subscribe(other, origin(other))
        await first.aclose()
        with pytest.raises(ReaderCapacityExceeded):
            await hub.subscribe(other, origin(other))
        await log.append(ref, event())
        assert (await next_entry(second)).envelope.event_id == 'event-1'
        await second.aclose()
        replacement = await hub.subscribe(other, origin(other))
        assert not replacement.closed
    finally:
        await hub.close()


async def test_reader_invalidation_releases_capacity_with_old_tabs_open(log):
    ref = await log.initialize('conversation', str(uuid4()))
    other = await log.initialize('other', str(uuid4()))
    hub = ConversationReaderHub(log, block_ms=20, max_readers=1)
    try:
        old = await hub.subscribe(ref, origin(ref))
        assert await hub.invalidate(ref)
        current = await hub.subscribe(other, origin(other))
        with pytest.raises(GenerationMismatch):
            await next_entry(old)
        # A late close from the retired reader must not remove the new slot.
        await old.aclose()
        assert not current.closed
        assert set(hub._hubs) == {'other'}
    finally:
        await hub.close()
