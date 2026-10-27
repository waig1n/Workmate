"""Opt-in local Redis AOF acceptance; never restarts an existing service.

WORKMATE_TEST_REDIS_DOCKER=1 creates a unique container and volume using an already
local redis:7-alpine image. Both are removed in finally, including on failure.
"""
import asyncio
from contextlib import suppress
import os
import subprocess
import time
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import RedisError

from workmate_product.coordination.command_queue import CommandEnvelope, QueueRoute, RedisCommandQueue

pytestmark = pytest.mark.asyncio


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.STDOUT).strip()


async def ready(client):
    deadline = time.monotonic() + 15
    while True:
        try:
            if await client.ping():
                return
        except (RedisError, OSError):
            if time.monotonic() >= deadline:
                raise
        await asyncio.sleep(.05)


@pytest_asyncio.fixture
async def persistent_redis():
    if os.environ.get('WORKMATE_TEST_REDIS_DOCKER') != '1':
        pytest.skip('requires explicitly enabled isolated local Docker Redis test')
    name = 'workmate-q2-aof-test-' + uuid4().hex
    volume = name + '-data'
    client = None
    docker('volume', 'create', volume)
    try:
        docker('run', '--detach', '--pull=never', '--name', name,
               '--memory=128m', '--cpus=.5', '--read-only',
               '--publish', '127.0.0.1::6379', '--volume', volume + ':/data',
               'redis:7-alpine', 'redis-server', '--appendonly', 'yes',
               '--appendfsync', 'always', '--save', '', '--maxmemory-policy', 'noeviction')
        port = docker('port', name, '6379/tcp').rsplit(':', 1)[1]
        client = Redis(host='127.0.0.1', port=int(port), decode_responses=True,
                       socket_connect_timeout=1, socket_timeout=1)
        await ready(client)
        yield client, name
    finally:
        if client is not None:
            await client.aclose()
        with suppress(subprocess.CalledProcessError):
            docker('rm', '--force', name)
        docker('volume', 'rm', volume)


async def test_aof_crash_restart_retains_unread_pending_and_acknowledgements(persistent_redis):
    client, container = persistent_redis
    queue = RedisCommandQueue(client, namespace='acceptance-' + uuid4().hex)
    route = QueueRoute('node')
    control = QueueRoute('node', control=True)
    for target in (route, control):
        await queue.ensure_group(target)
    ids = []
    for n in range(4):
        ids.append(await queue.publish(route, CommandEnvelope(
            command_id=f'command-{n}', delivery_generation=1, storage_node_id='node')))
    first = await queue.read(route, 'old-worker', count=2)
    assert [message.message_id for message in first] == ids[:2]
    await queue.ack(route, [ids[0]])
    stop_id = await queue.publish(control, CommandEnvelope(
        command_id='stop-command', delivery_generation=1, storage_node_id='node'))
    # SIGKILL exercises AOF recovery, not graceful snapshot/shutdown behavior.
    docker('kill', '--signal=KILL', container)
    docker('start', container)
    # Docker may allocate a new ephemeral host port on container restart.
    port = docker('port', container, '6379/tcp').rsplit(':', 1)[1]
    async with Redis(host='127.0.0.1', port=int(port), decode_responses=True,
                     socket_connect_timeout=1, socket_timeout=1) as client:
        queue = RedisCommandQueue(client, namespace=queue.namespace)
        await ready(client)
        assert (await client.info('persistence'))['aof_enabled'] == 1
        await queue.ensure_group(route)  # Existing group's position is not reset.
        pending = await queue.pending(route)
        assert len(pending) == 1 and pending[0].message_id == ids[1]
        assert pending[0].consumer == 'old-worker'
        assert [message.message_id for message in await queue.read(route, 'new-worker', count=10)] == ids[2:]
        # Independent control traffic remains consumable even with all execution
        # messages held pending. The production Worker has a separate control budget.
        assert (await queue.read(control, 'new-worker'))[0].message_id == stop_id
        reclaimed = await queue.claim_pending(route, 'new-worker', [ids[1]], min_idle_ms=0)
        assert reclaimed[0].envelope.command_id == 'command-1'
        assert await queue.ack(route, ids) == 3
        assert await queue.pending(route) == []
        assert await queue.read(route, 'new-worker') == []
        assert await queue.trim_acknowledged(route, retain_ms=0) == 3
        assert await queue.stream_has(route, ids[-1])
