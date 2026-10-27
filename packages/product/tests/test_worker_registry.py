"""Real Redis worker/affinity hints, never execution authority."""
import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from workmate_product.coordination.worker_registry import AffinityRecord, RedisWorkerRegistry, WorkerRecord


@pytest_asyncio.fixture
async def registry():
    url = os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires explicitly configured local Redis')
    client = Redis.from_url(url, decode_responses=True)
    value = RedisWorkerRegistry(client, namespace='registry-test-' + uuid4().hex)
    try:
        yield value
    finally:
        keys = [key async for key in client.scan_iter(match=f'workmate:{value.namespace}:*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def worker(**kwargs):
    return WorkerRecord(**({'incarnation':'worker-a', 'node_id':'node', 'total_capacity':2,
                           'available_capacity':1} | kwargs))


def affinity(**kwargs):
    return AffinityRecord(**({'session_id':'session', 'node_id':'node', 'incarnation':'worker-a',
                             'workspace_generation':'generation-a', 'hold_version':1} | kwargs))


async def test_health_hint_capacity_defaults_and_drain(registry):
    record = worker()
    assert not record.draining
    assert await registry.worker(record.incarnation) is None
    await registry.heartbeat(record, ttl_seconds=30)
    assert await registry.worker(record.incarnation, node_id='node') == record
    assert await registry.worker(record.incarnation, node_id='other') is None
    draining = worker(draining=True)
    await registry.heartbeat(draining, ttl_seconds=30)
    assert await registry.worker(record.incarnation) is None
    assert await registry.worker(record.incarnation, allow_draining=True) == draining
    assert await registry.withdraw_worker(record)
    assert await registry.worker(record.incarnation, allow_draining=True) is None


async def test_affinity_requires_live_same_node_nondraining_worker(registry):
    hint = affinity()
    assert await registry.publish_affinity(hint, ttl_seconds=30)
    assert await registry.affinity('session', 'node') is None
    await registry.heartbeat(worker(node_id='other'), ttl_seconds=30)
    assert await registry.affinity('session', 'node') is None
    await registry.heartbeat(worker(), ttl_seconds=30)
    assert await registry.affinity('session', 'node') == hint
    assert await registry.affinity('session', 'other') is None
    await registry.heartbeat(worker(draining=True), ttl_seconds=30)
    assert await registry.affinity('session', 'node') is None
    await registry.heartbeat(worker(available_capacity=0), ttl_seconds=30)
    assert await registry.affinity('session', 'node') == hint  # Sticky queue while busy is allowed.


async def test_stale_refresh_or_exit_cannot_replace_new_holder(registry):
    old = affinity()
    new = affinity(incarnation='worker-b', workspace_generation='generation-b', hold_version=2)
    await registry.heartbeat(worker(), ttl_seconds=30)
    await registry.heartbeat(worker(incarnation='worker-b'), ttl_seconds=30)
    assert await registry.publish_affinity(old, ttl_seconds=30)
    assert await registry.publish_affinity(old, ttl_seconds=30)
    assert not await registry.publish_affinity(affinity(incarnation='worker-b'), ttl_seconds=30)
    assert await registry.publish_affinity(new, ttl_seconds=30)
    assert not await registry.publish_affinity(old, ttl_seconds=30)
    assert not await registry.withdraw_affinity(old)
    assert await registry.withdraw_worker(worker())
    assert await registry.worker('worker-b') is not None
    assert await registry.affinity('session', 'node') == new
    assert await registry.withdraw_affinity(new)
    assert not await registry.withdraw_affinity(new)
    assert await registry.affinity('session', 'node') is None


async def test_health_and_affinity_ttl_expire_independently(registry):
    await registry.heartbeat(worker(), ttl_seconds=30)
    await registry.publish_affinity(affinity(), ttl_seconds=30)
    await registry.client.pexpire(registry.worker_key('worker-a'), 1)
    await asyncio.sleep(.01)
    assert await registry.affinity('session', 'node') is None
    await registry.heartbeat(worker(), ttl_seconds=30)
    assert await registry.affinity('session', 'node') == affinity()
    await registry.client.pexpire(registry.affinity_key('session'), 1)
    await asyncio.sleep(.01)
    assert await registry.affinity('session', 'node') is None
    assert await registry.worker('worker-a') is not None


async def test_namespaces_and_unrestricted_session_id_do_not_collide(registry):
    other = RedisWorkerRegistry(registry.client, namespace=registry.namespace + '-other')
    assert other.worker_key('worker-a') != registry.worker_key('worker-a')
    value = affinity(session_id='session: / 审批')
    await registry.heartbeat(worker(), ttl_seconds=30)
    await registry.publish_affinity(value, ttl_seconds=30)
    assert await registry.affinity(value.session_id, 'node') == value
    assert await other.affinity(value.session_id, 'node') is None
    assert await other.worker('worker-a') is None
    assert registry.affinity_key('a:b') != registry.affinity_key('a/b')


@pytest.mark.parametrize('kwargs', [{'total_capacity':0}, {'available_capacity':-1},
    {'available_capacity':3}, {'total_capacity':1025}, {'total_capacity':True}, {'draining':1}, {'incarnation':'escape:key'}])
def test_worker_bounds_are_strict(kwargs):
    with pytest.raises(ValueError):
        worker(**kwargs)


@pytest.mark.parametrize('kwargs', [{'hold_version':0}, {'hold_version':True},
    {'workspace_generation':''}, {'workspace_generation':'bad:key'}, {'session_id':''}])
def test_affinity_generation_and_identity_bounds(kwargs):
    with pytest.raises(ValueError):
        affinity(**kwargs)


@pytest.mark.parametrize('ttl', [0, -1, True, 3601, '30'])
async def test_ttl_bounds_reject_invalid_input(registry, ttl):
    with pytest.raises(ValueError):
        await registry.heartbeat(worker(), ttl_seconds=ttl)
    with pytest.raises(ValueError):
        await registry.publish_affinity(affinity(), ttl_seconds=ttl)


async def test_corrupted_or_foreign_key_records_are_ignored(registry):
    key = registry.worker_key('worker-a')
    await registry.client.set(key, worker(incarnation='foreign').model_dump_json())
    assert await registry.worker('worker-a') is None
    assert not await registry.withdraw_worker(worker())
    await registry.client.set(key, 'invalid json')
    assert await registry.worker('worker-a') is None
    await registry.heartbeat(worker(), ttl_seconds=30)
    await registry.client.set(registry.affinity_key('session'), affinity(session_id='other').model_dump_json())
    assert await registry.affinity('session', 'node') is None
    await registry.client.set(registry.affinity_key('session'), 'invalid json')
    assert await registry.affinity('session', 'node') is None


async def test_bigint_versions_compare_without_lua_precision_loss(registry):
    await registry.heartbeat(worker(), ttl_seconds=30)
    first = affinity(hold_version=9007199254740992)
    second = affinity(hold_version=9007199254740993, workspace_generation='generation-b')
    assert await registry.publish_affinity(first)
    assert await registry.publish_affinity(second)
    assert not await registry.publish_affinity(first)
    assert not await registry.withdraw_affinity(first)
    assert await registry.affinity('session', 'node') == second


async def test_ttl_expiry_does_not_fence_old_hint_requires_pg_residence_check(registry):
    old = affinity()
    newer = affinity(hold_version=2, workspace_generation='generation-b')
    await registry.heartbeat(worker())
    assert await registry.publish_affinity(newer)
    await registry.client.pexpire(registry.affinity_key('session'), 1)
    await asyncio.sleep(.01)
    # This intentionally succeeds: Redis is reconstructible, not a lock ledger.
    # A router must reject this if PG residence already has version 2.
    assert await registry.publish_affinity(old)
    assert await registry.affinity('session', 'node') == old


async def test_default_ttl_and_namespace_validation(registry):
    await registry.heartbeat(worker())
    await registry.publish_affinity(affinity())
    assert 0 < await registry.client.ttl(registry.worker_key('worker-a')) <= 30
    assert 0 < await registry.client.ttl(registry.affinity_key('session')) <= 30
    for namespace in ('', 'bad:key', 'space here', '{cluster}'):
        with pytest.raises(ValueError):
            RedisWorkerRegistry(registry.client, namespace=namespace)


async def test_binary_client_and_malformed_wrapper(registry):
    async with Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'], decode_responses=False) as client:
        binary = RedisWorkerRegistry(client, namespace=registry.namespace)
        await binary.heartbeat(worker())
        await binary.publish_affinity(affinity())
        assert await binary.affinity('session', 'node') == affinity()
        key = binary.affinity_key('session')
        await client.set(key, '{"version":"2","holder":"wrong","record":' + affinity().model_dump_json() + '}')
        assert await binary.affinity('session', 'node') is None
        await client.set(key, '{"version":"1","holder":"wrong","record":' + affinity().model_dump_json() + '}')
        assert await binary.affinity('session', 'node') is None
        await client.set(key, '[]')
        assert await binary.affinity('session', 'node') is None
        await client.set(key, '\xff'.encode('latin-1'))
        assert await binary.affinity('session', 'node') is None


async def test_foreign_node_affinity_and_withdraw_identity(registry):
    await registry.heartbeat(worker())
    assert not await registry.withdraw_worker(worker(node_id='other'))
    assert await registry.publish_affinity(affinity(node_id='other'))
    assert await registry.affinity('session', 'node') is None
    await registry.client.set(registry.worker_key('worker-a'), 'null')
    assert not await registry.withdraw_worker(worker())
    await registry.client.set(registry.affinity_key('session'), 'null')
    assert not await registry.withdraw_affinity(affinity())
    assert await registry.publish_affinity(affinity())
    for value in ('', 'a'*129, 1):
        with pytest.raises(ValueError):
            registry.affinity_key(value)


def test_capacity_matches_existing_worker_configuration_limit():
    assert worker(total_capacity=1024, available_capacity=1024).available_capacity == 1024
