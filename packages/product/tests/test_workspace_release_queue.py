"""Internal compute-release notices remain distinct from user Stop commands."""
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from workmate_product.coordination.command_queue import CommandEnvelope, QueueRoute, RedisCommandQueue
from workmate_product.coordination.workspace_release_queue import RedisWorkspaceReleaseQueue, WorkspaceReleaseEnvelope


@pytest_asyncio.fixture
async def queue():
    url = os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires explicitly configured local Redis')
    client = Redis.from_url(url, decode_responses=True)
    value = RedisWorkspaceReleaseQueue(client, namespace='release-test-' + uuid4().hex)
    try:
        yield value
    finally:
        keys = [key async for key in client.scan_iter(match=f'workmate:{value.namespace}:*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def notice(**kwargs):
    return WorkspaceReleaseEnvelope(**({'request_id':'release:request', 'session_id':'session: identity',
        'storage_node_id':'node', 'target_incarnation':'worker',
        'workspace_generation':'generation', 'expected_hold_version':1} | kwargs))


def route():
    return QueueRoute('node', 'worker', control=True)


async def test_typed_release_does_not_mix_with_user_control(queue):
    commands = RedisCommandQueue(queue.client, namespace=queue.namespace)
    await queue.ensure_group(route())
    await commands.ensure_group(route())
    mid = await queue.publish(route(), notice())
    assert await commands.read(route(), 'worker') == []
    message = (await queue.read(route(), 'worker'))[0]
    assert message.message_id == mid and message.envelope == notice()
    assert message.protocol_error is None
    assert len(await queue.pending(route())) == 1
    assert await queue.ack(route(), [mid]) == 1
    assert await queue.ack(route(), [mid]) == 0


async def test_recovery_is_explicit_and_same_target_only(queue):
    await queue.ensure_group(route())
    mid = await queue.publish(route(), notice())
    await queue.read(route(), 'worker')
    assert (await queue.peek(route(), mid)).envelope == notice()
    assert await queue.read(route(), 'worker') == []
    with pytest.raises(ValueError):
        await queue.claim_pending(route(), 'other', [mid], min_idle_ms=0)
    assert (await queue.claim_pending(route(), 'worker', [mid], min_idle_ms=0))[0].message_id == mid
    with pytest.raises(ValueError):
        await queue.read(route(), 'other')
    with pytest.raises(ValueError):
        await queue.publish(route(), notice(target_incarnation='other'))


async def test_command_or_bad_protocol_requires_durable_isolation_before_ack(queue):
    await queue.ensure_group(route())
    command = CommandEnvelope(command_id='stop', delivery_generation=1,
                              storage_node_id='node', target_incarnation='worker')
    for payload in (command.model_dump_json(), notice().model_dump_json().replace('workspace_release', 'StopTask'), '{}'):
        await queue.client.xadd(queue.stream_key(route()), {'payload':payload})
    messages = await queue.read(route(), 'worker', count=10)
    assert len(messages) == 3
    assert all(message.envelope is None and message.protocol_error == 'invalid_envelope' for message in messages)
    assert len(await queue.pending(route())) == 3


async def test_foreign_release_route_is_quarantinable_not_executable(queue):
    await queue.ensure_group(route())
    await queue.client.xadd(queue.stream_key(route()), {'payload':notice(storage_node_id='other').model_dump_json()})
    message = (await queue.read(route(), 'worker'))[0]
    assert message.envelope is None and message.protocol_error == 'route_mismatch'
    assert len(await queue.pending(route())) == 1


async def test_release_safe_retention_does_not_touch_command_stream(queue):
    await queue.ensure_group(route())
    for mid in ('1-0','2-0','3-0'):
        await queue.client.xadd(queue.stream_key(route()), {'payload':notice().model_dump_json()}, id=mid)
    await queue.read(route(), 'worker', count=2)
    await queue.ack(route(), ['1-0'])
    commands = RedisCommandQueue(queue.client, namespace=queue.namespace)
    await commands.ensure_group(route())
    command_mid = await commands.publish(route(), CommandEnvelope(command_id='stop', delivery_generation=1,
        storage_node_id='node', target_incarnation='worker'))
    cursor = 0
    while True:
        cursor = await queue.trim_page(cursor=cursor, count=2, retain_ms=0)
        if cursor == 0:
            break
    assert [mid for mid,_ in await queue.client.xrange(queue.stream_key(route()))] == ['2-0','3-0']
    assert await commands.stream_has(route(), command_mid)
    assert (await queue.pending(route()))[0].message_id == '2-0'


@pytest.mark.parametrize('value', [QueueRoute('node'), QueueRoute('node', 'worker'), QueueRoute('node', control=True)])
def test_release_queue_requires_directed_internal_route(value):
    with pytest.raises(ValueError):
        RedisWorkspaceReleaseQueue(None, namespace='test').stream_key(value)


@pytest.mark.parametrize('kwargs', [{'protocol_version':True}, {'kind':'StopTask'},
    {'expected_hold_version':0}, {'expected_hold_version':True}, {'request_id':''},
    {'workspace_generation':''}, {'actor_user_id':'forged'}])
def test_release_envelope_is_strict_and_does_not_carry_business_authority(kwargs):
    with pytest.raises(ValueError):
        notice(**kwargs)
