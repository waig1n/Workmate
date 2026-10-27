"""Command queue transport contracts, independently of Product execution authority."""
import os
import uuid

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from workmate_product.coordination.command_queue import CommandEnvelope, QueueRoute, RedisCommandQueue


@pytest_asyncio.fixture
async def queue():
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires explicitly configured local Redis')
    client=Redis.from_url(url,decode_responses=True)
    namespace='q2-test-'+uuid.uuid4().hex
    value=RedisCommandQueue(client,namespace=namespace)
    try:
        yield value
    finally:
        keys=[key async for key in client.scan_iter(match=f'workmate:{namespace}:*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def envelope(command='command',generation=1,worker=None):
    return CommandEnvelope(command_id=command,delivery_generation=generation,
        storage_node_id='node',target_incarnation=worker)


async def test_competing_consumers_and_ack_are_explicit(queue):
    route=QueueRoute('node')
    await queue.ensure_group(route)
    mid=await queue.publish(route,envelope())
    first=await queue.read(route,'worker-a',count=1)
    second=await queue.read(route,'worker-b',count=1)
    assert len(first)==1 and second==[]
    assert first[0].message_id==mid and first[0].envelope.command_id=='command'
    assert first[0].protocol_error is None
    pending=await queue.pending(route,min_idle_ms=0,count=10)
    assert [p.message_id for p in pending]==[mid]
    assert await queue.ack(route,[mid])==1
    assert await queue.ack(route,[mid])==0
    assert await queue.pending(route,min_idle_ms=0,count=10)==[]


async def test_pending_requires_explicit_claim_and_survives_consumer_exit(queue):
    route=QueueRoute('node')
    await queue.ensure_group(route)
    mid=await queue.publish(route,envelope())
    await queue.read(route,'old',count=1)
    candidates=await queue.pending(route,min_idle_ms=0,count=1)
    assert candidates[0].consumer=='old'
    assert await queue.read(route,'new',count=1)==[]
    claimed=await queue.claim_pending(route,'new',[mid],min_idle_ms=0)
    assert claimed[0].message_id==mid
    assert (await queue.pending(route,min_idle_ms=0,count=1))[0].consumer=='new'


async def test_namespace_and_directed_control_routes_do_not_cross(queue):
    other=RedisCommandQueue(queue.client,namespace=queue.namespace+'-other')
    node=QueueRoute('node')
    directed=QueueRoute('node',worker_id='target',control=True)
    for route in (node,directed):
        await queue.ensure_group(route)
    await queue.publish(directed,envelope(worker='target'))
    assert await queue.read(node,'target',count=1)==[]
    with pytest.raises(ValueError):
        await queue.read(directed,'wrong-worker',count=1)
    assert len(await queue.read(directed,'target',count=1))==1
    assert other.stream_key(node)!=queue.stream_key(node)


async def test_bad_protocol_is_returned_without_ack_for_durable_isolation(queue):
    route=QueueRoute('node')
    await queue.ensure_group(route)
    mid=await queue.client.xadd(queue.stream_key(route),{'payload':'{"protocol_version":99}'})
    messages=await queue.read(route,'worker',count=1)
    assert messages[0].message_id==mid and messages[0].envelope is None
    assert messages[0].protocol_error=='invalid_envelope'
    assert len(await queue.pending(route,min_idle_ms=0,count=1))==1


async def test_trim_preserves_pending_and_unread_messages(queue):
    route=QueueRoute('node')
    await queue.ensure_group(route)
    ids=[await queue.publish(route,envelope(str(n))) for n in range(5)]
    messages=await queue.read(route,'worker',count=3)
    await queue.ack(route,[messages[0].message_id,messages[2].message_id])
    await queue.trim_acknowledged(route,retain_ms=0)
    stored=[key for key,_ in await queue.client.xrange(queue.stream_key(route))]
    assert ids[1:] == stored
    assert [m.message_id for m in await queue.read(route,'worker',count=10)]==ids[3:]


async def test_slowest_group_blocks_trim(queue):
    route=QueueRoute('node')
    await queue.ensure_group(route)
    ids=[await queue.publish(route,envelope(str(n))) for n in range(3)]
    await queue.client.xgroup_create(queue.stream_key(route),'other-group',id='0-0')
    messages=await queue.read(route,'worker',count=10)
    await queue.ack(route,[m.message_id for m in messages])
    assert await queue.trim_acknowledged(route,retain_ms=0)==0
    assert await queue.client.xlen(queue.stream_key(route))==len(ids)


@pytest.mark.parametrize('namespace',['','bad:key','white space','{cluster}'])
def test_namespace_cannot_escape_key_boundaries(namespace):
    with pytest.raises(ValueError):
        RedisCommandQueue(None,namespace=namespace)


@pytest.mark.parametrize('payload',[{'command_id':'','delivery_generation':1,'storage_node_id':'node'},
    {'command_id':'c','delivery_generation':True,'storage_node_id':'node'},
    {'command_id':'c','delivery_generation':0,'storage_node_id':'node'},
    {'command_id':'c','delivery_generation':1,'storage_node_id':'node','actor':'forged'}])
def test_command_envelope_is_strict_and_contains_no_business_authority(payload):
    with pytest.raises(ValueError):
        CommandEnvelope.model_validate(payload)


async def test_group_creation_is_idempotent_and_reads_preexisting_message(queue):
    route = QueueRoute('node')
    mid = await queue.publish(route, envelope())
    await queue.ensure_group(route)
    await queue.ensure_group(route)
    assert (await queue.read(route, 'worker'))[0].message_id == mid
    assert await queue.stream_has(route, mid)
    assert not await queue.stream_has(route, '0-0')


async def test_foreign_route_rejected_at_publish_and_quarantinable_on_read(queue):
    route = QueueRoute('node')
    await queue.ensure_group(route)
    wrong = CommandEnvelope(command_id='c', delivery_generation=1, storage_node_id='other')
    with pytest.raises(ValueError, match='route'):
        await queue.publish(route, wrong)
    mid = await queue.client.xadd(queue.stream_key(route), {'payload': wrong.model_dump_json()})
    message = (await queue.read(route, 'worker'))[0]
    assert message.message_id == mid and message.protocol_error == 'route_mismatch'
    assert len(await queue.pending(route)) == 1


async def test_binary_client_decodes_messages_and_pending(queue):
    client = Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'], decode_responses=False)
    try:
        binary = RedisCommandQueue(client, namespace=queue.namespace)
        route = QueueRoute('node')
        await binary.ensure_group(route)
        mid = await binary.publish(route, envelope())
        assert isinstance(mid, str)
        assert (await binary.read(route, 'worker'))[0].envelope == envelope()
        pending = (await binary.pending(route))[0]
        assert pending.message_id == mid and pending.consumer == 'worker'
        assert pending.delivery_count == 1
        assert (await binary.claim_pending(route, 'new', [mid], min_idle_ms=0))[0].message_id == mid
    finally:
        await client.aclose()


async def test_trim_no_group_has_no_proof_and_retention_window_preserved(queue):
    route = QueueRoute('node')
    assert await queue.trim_acknowledged(route, retain_ms=0) == 0
    ids = [await queue.publish(route, envelope(str(n))) for n in range(3)]
    assert await queue.trim_acknowledged(route, retain_ms=0) == 0
    await queue.ensure_group(route)
    await queue.read(route, 'worker', count=10)
    await queue.ack(route, ids)
    assert await queue.trim_acknowledged(route, retain_ms=60000) == 0
    assert await queue.client.xlen(queue.stream_key(route)) == 3


async def test_other_group_pending_and_large_sequence_ids_preserved(queue):
    route = QueueRoute('node')
    await queue.ensure_group(route)
    # IDs above 2**53 expose precision loss if Lua compares tonumber(id sequence).
    ids = ['1-9007199254740992', '1-9007199254740993', '1-9007199254740994']
    for mid in ids:
        await queue.client.xadd(queue.stream_key(route), {'payload': envelope().model_dump_json()}, id=mid)
    await queue.read(route, 'worker', count=10)
    await queue.ack(route, ids)
    await queue.client.xgroup_create(queue.stream_key(route), 'other-group', id='0-0')
    await queue.client.xreadgroup('other-group', 'other', {queue.stream_key(route): '>'}, count=3)
    await queue.client.xack(queue.stream_key(route), 'other-group', ids[0], ids[2])
    assert await queue.trim_acknowledged(route, retain_ms=0) == 1
    assert [mid for mid, _ in await queue.client.xrange(queue.stream_key(route))] == ids[1:]


async def test_invalid_format_extra_fields_remains_pending(queue):
    route = QueueRoute('node')
    await queue.ensure_group(route)
    await queue.client.xadd(queue.stream_key(route), {'payload': envelope().model_dump_json(), 'extra': 'x'})
    assert (await queue.read(route, 'worker'))[0].protocol_error == 'invalid_envelope'
    assert len(await queue.pending(route)) == 1


async def test_bounds_and_directed_claim_are_checked_before_io(queue):
    route = QueueRoute('node')
    with pytest.raises(ValueError):
        await queue.read(route, 'worker', count=0)
    with pytest.raises(ValueError):
        await queue.read(route, 'worker', block_ms=0)
    with pytest.raises(ValueError):
        await queue.pending(route, min_idle_ms=-1)
    with pytest.raises(ValueError):
        await queue.trim_acknowledged(route, retain_ms=-1)
    with pytest.raises(ValueError):
        await queue.claim_pending(QueueRoute('node', 'owner'), 'stranger', ['1-0'], min_idle_ms=0)
    assert await queue.claim_pending(route, 'worker', [], min_idle_ms=0) == []
    assert await queue.ack(route, []) == 0


@pytest.mark.parametrize('kwargs', [{'node_id': ''}, {'node_id': 'node', 'worker_id': 'x:y'},
                                   {'node_id': 'node', 'control': 1}])
def test_routes_cannot_escape_namespace(kwargs):
    with pytest.raises(ValueError):
        QueueRoute(**kwargs)


async def test_redis_failure_is_propagated_without_ack(queue):
    from redis.exceptions import ResponseError
    route = QueueRoute('node')
    await queue.client.set(queue.stream_key(route), 'not a stream')
    with pytest.raises(ResponseError):
        await queue.ensure_group(route)


@pytest.mark.parametrize('version', [True, 0, 2, '1'])
def test_protocol_version_is_strict(version):
    with pytest.raises(ValueError):
        CommandEnvelope(command_id='command', delivery_generation=1, storage_node_id='node',
                        protocol_version=version)


async def test_peek_does_not_reassign_live_owner_or_update_delivery_count(queue):
    route = QueueRoute('node')
    await queue.ensure_group(route)
    mid = await queue.publish(route, envelope())
    await queue.read(route, 'owner')
    assert (await queue.peek(route, mid)).envelope == envelope()
    assert await queue.peek(route, '0-0') is None
    pending = (await queue.pending(route))[0]
    assert pending.consumer == 'owner' and pending.delivery_count == 1


async def test_pending_cursor_reaches_orphans_behind_live_owners(queue):
    route = QueueRoute('node')
    await queue.ensure_group(route)
    ids = [await queue.publish(route, envelope(str(n))) for n in range(3)]
    await queue.read(route, 'live-owner', count=2)
    await queue.read(route, 'orphan-owner', count=1)
    first = await queue.pending(route, count=2)
    assert [item.message_id for item in first] == ids[:2]
    following = await queue.pending(route, count=2, start='(' + first[-1].message_id)
    assert [item.message_id for item in following] == ids[2:]
    assert following[0].consumer == 'orphan-owner'
    assert await queue.pending(route, start='(' + following[-1].message_id) == []


async def test_maintenance_pages_only_command_streams_with_safe_group_proof(queue):
    routes = [QueueRoute('node-' + str(n), control=bool(n % 2)) for n in range(10)]
    routes += [QueueRoute('node', 'worker-' + str(n), control=bool(n % 2)) for n in range(10)]
    for route in routes:
        await queue.ensure_group(route)
        # Old timestamps make retention tests independent of wall-clock scheduling.
        for mid in ('1-0', '2-0', '3-0'):
            await queue.client.xadd(queue.stream_key(route), {'payload': 'opaque'}, id=mid)
        await queue.client.xreadgroup(queue.group, route.worker_id or 'owner',
                                     {queue.stream_key(route): '>'}, count=3)
        await queue.client.xack(queue.stream_key(route), queue.group, '1-0', '3-0')
    unrelated = f'workmate:{queue.namespace}:node:node:events'
    invalid_suffix = f'workmate:{queue.namespace}:worker:worker:control:extra'
    invalid_kind = f'workmate:{queue.namespace}:node:node:execute'
    for key in (unrelated, invalid_suffix, invalid_kind):
        await queue.client.xadd(key, {'payload': 'opaque'}, id='1-0')
    non_stream = f'workmate:{queue.namespace}:node:ordinary:ready'
    await queue.client.set(non_stream, 'ordinary')
    cursor = 0
    seen_pages = 0
    while True:
        cursor = await queue.trim_page(cursor=cursor, count=2, retain_ms=0)
        seen_pages += 1
        if cursor == 0:
            break
        assert seen_pages < 10000
    assert seen_pages > 1
    for route in routes:
        assert [mid for mid, _ in await queue.client.xrange(queue.stream_key(route))] == ['2-0', '3-0']
        assert len(await queue.pending(route)) == 1
    for key in (unrelated, invalid_suffix, invalid_kind):
        assert await queue.client.xlen(key) == 1
    assert await queue.client.get(non_stream) == 'ordinary'


async def test_maintenance_rejects_invalid_bounds(queue):
    for kwargs in ({'cursor': -1}, {'count': 0}, {'retain_ms': -1}):
        with pytest.raises(ValueError):
            await queue.trim_page(**({'retain_ms': 0} | kwargs))


async def test_command_identity_matches_business_contract_not_key_segment(queue):
    route = QueueRoute('node')
    await queue.ensure_group(route)
    value = envelope('command: legacy / 审批')
    mid = await queue.publish(route, value)
    message = (await queue.read(route, 'worker'))[0]
    assert message.message_id == mid and message.envelope == value
