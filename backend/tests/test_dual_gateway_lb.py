"""Two real Gateway processes through real local Nginx, with scripted events.

Requires isolated WORKMATE_TEST_POSTGRES_DSN / WORKMATE_TEST_REDIS_URL plus an
installed WORKMATE_TEST_NGINX executable. Proves HTTP transport/reader/publisher
failover only; does not claim real Worker HITL/Stop or production HA capacity.
"""
import asyncio
from contextlib import AsyncExitStack
import os
import shutil
import time
from urllib.parse import urlsplit

import httpx
import jwt
import pytest

from test_durable_projection import projection_database  # noqa: F401
from test_business_reset_composition import composition  # noqa: F401
from test_dual_gateway_http import Gateway, event, assert_revoked
from gateway_lb import Nginx
from workmate_product.application.message_snapshots import save_message_snapshot
from workmate_product.realtime.message_accumulator import MessageAccumulator
from workmate_product.projections.store import ProjectedEvent, append_business_events


async def test_nginx_gateway_failure_replay_restart_and_revocation(composition, tmp_path):
    if not (os.getenv('WORKMATE_TEST_NGINX') or os.getenv('WORKMATE_TEST_NGINX_IMAGE') or shutil.which('nginx')):
        pytest.skip('requires installed Nginx; acceptance runner must preflight this')
    sessions, admin, log, manager, _ = composition
    schema = await admin.fetchval('SELECT current_schema()')
    config = tmp_path / 'gateway.yaml'
    config.write_text('product:\n  workspace_assistant:\n    enabled: false\n'
        'realtime:\n  authorize_interval_seconds: 0.1\n  reader_block_ms: 50\n'
        '  heartbeat_interval_seconds: 0.1\ncommand_queue:\n  poll_interval_seconds: 0.05\n')
    await admin.execute("UPDATE runtime_sessions SET lease_until=now()+interval '5 minutes'")
    ref = await manager.ensure('c')
    producer = MessageAccumulator('c', 't', 'reply', 1)
    for raw in ({'type':'TEXT_BLOCK_START','block_id':'body'},
                {'type':'TEXT_BLOCK_DELTA','block_id':'body','delta':'initial'}):
        for envelope in producer.apply(raw):
            receipt = await log.append(ref, envelope)
    baseline = producer.capture('partial', receipt.cursor, 0)
    async with sessions.begin() as db:
        await save_message_snapshot(db, baseline, owner_id='worker')
    headers = {'Authorization': 'Bearer ' + jwt.encode({'sub':'u','ver':0,
        'iat':int(time.time()), 'exp':int(time.time())+600},
        'local-dual-gateway-signing-key-0123456789', algorithm='HS256')}
    gateways = [Gateway(tmp_path, schema, log.namespace, config, index) for index in range(2)]
    proxy = None
    route = '/api/tasks/conversations/c/events'
    try:
        for gateway in gateways:
            await gateway.start()
        ports = [urlsplit(gateway.url).port for gateway in gateways]
        proxy = Nginx(tmp_path, ports)
        await proxy.start()
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=None))
            # A single URL reaches both processes; no test-side routing header.
            identities = set()
            for _ in range(4):
                response = await client.get(proxy.url + '/health')
                assert response.status_code == 200
                identities.add(response.headers['x-acceptance-upstream'])
            assert identities == {f'127.0.0.1:{port}' for port in ports}
            streams = []
            for _ in range(2):
                response = await stack.enter_async_context(client.stream('GET', proxy.url + route, headers=headers))
                assert response.status_code == 200
                reader = response.aiter_lines()
                reset, _ = await event(reader, 'REALTIME_RESET')
                assert reset['snapshots'] == [baseline.model_dump(mode='json')]
                await event(reader, 'REPLAY_DONE')
                streams.append((response, reader))
            assert {response.headers['x-acceptance-upstream'] for response, _ in streams} == identities
            live = producer.apply({'type':'TEXT_BLOCK_DELTA','block_id':'body','delta':' live'})[0]
            await log.append(ref, live)
            seen = await asyncio.gather(*(event(reader, 'REALTIME_EVENT') for _, reader in streams))
            assert all(value == live.model_dump(mode='json') for value, _ in seen)
            assert len({cursor for _, cursor in seen}) == 1
            cursor = seen[0][1]
            failed, healthy = sorted(streams, key=lambda pair: pair[0].headers['x-acceptance-upstream'] != f'127.0.0.1:{ports[0]}')
            # Abrupt Gateway loss, not a graceful client disconnect.
            gateways[0].process.kill()
            assert await gateways[0].process.wait() == -9
            async with asyncio.timeout(5):
                try:
                    async for _ in failed[1]:
                        pass
                except httpx.RemoteProtocolError:
                    pass
            later = producer.apply({'type':'TEXT_BLOCK_DELTA','block_id':'body','delta':' after failure'})[0]
            await log.append(ref, later)
            assert (await event(healthy[1], 'REALTIME_EVENT'))[0] == later.model_dump(mode='json')
            async with client.stream('GET', proxy.url + route,
                headers={**headers, 'Last-Event-ID':cursor}) as resumed:
                assert resumed.status_code == 200
                assert resumed.headers['x-acceptance-upstream'].endswith(f'127.0.0.1:{ports[1]}')
                reader = resumed.aiter_lines()
                assert (await event(reader, 'REALTIME_EVENT'))[0] == later.model_dump(mode='json')
                await event(reader, 'REPLAY_DONE')
            await gateways[0].close()
            await gateways[0].start(port=ports[0])
            await asyncio.sleep(1.1)  # Nginx's explicit one-second failed-peer window.
            identities = {(await client.get(proxy.url + '/health')).headers['x-acceptance-upstream'] for _ in range(4)}
            assert identities == {f'127.0.0.1:{port}' for port in ports}
            async with sessions.begin() as db:
                await append_business_events(db, 'c', operation_key='lb-notice', task_id='t', reply_id='reply',
                    items=[ProjectedEvent(key='notice',event_type='CUSTOM',
                        payload={'type':'CUSTOM','name':'NOTICE','value':{'ok':True}})])
            assert (await event(healthy[1], 'REALTIME_EVENT'))[0]['payload']['name'] == 'NOTICE'
            await admin.execute("UPDATE users SET token_version=1 WHERE id='u'")
            await assert_revoked(healthy[1])
            for _ in range(2):
                assert (await client.get(proxy.url + route, headers=headers)).status_code == 401
        assert await admin.fetchval("SELECT count(*) FROM realtime_business_outbox WHERE status='published'") == 1
    finally:
        if proxy:
            await proxy.close()
        await asyncio.gather(*(gateway.close() for gateway in gateways))


async def test_nginx_trimmed_cursor_resets_to_exact_snapshot_on_both_gateways(composition, tmp_path):
    """A real retention trim must never silently resume an incomplete prefix."""
    from workmate_contracts.realtime import encode_cursor, decode_cursor

    sessions, admin, log, manager, _ = composition
    schema = await admin.fetchval('SELECT current_schema()')
    await admin.execute("UPDATE runtime_sessions SET lease_until=now()+interval '5 minutes'")
    ref = await manager.ensure('c')
    producer = MessageAccumulator('c', 't', 'reply', 1)
    for raw in ({'type': 'TEXT_BLOCK_START', 'block_id': 'body'},
                {'type': 'TEXT_BLOCK_DELTA', 'block_id': 'body', 'delta': 'old prefix'}):
        for envelope in producer.apply(raw):
            old = await log.append(ref, envelope)
    for envelope in producer.apply({'type': 'TEXT_BLOCK_DELTA', 'block_id': 'body', 'delta': ' retained suffix'}):
        latest = await log.append(ref, envelope)
    snapshot = producer.capture('partial', latest.cursor, 0)
    async with sessions.begin() as db:
        await save_message_snapshot(db, snapshot, owner_id='worker')
    # Use the production atomic retention implementation, not raw XTRIM which
    # would destroy the log's integrity metadata and test a different failure.
    log.max_bytes = 1
    window = await log.window(ref)
    assert window.floor != old.cursor
    log.max_bytes = 8388608
    config = tmp_path / 'gateway.yaml'
    config.write_text('product:\n  workspace_assistant:\n    enabled: false\n'
        'realtime:\n  reader_block_ms: 50\n  heartbeat_interval_seconds: 0.1\n')
    headers = {'Authorization': 'Bearer ' + jwt.encode({'sub': 'u', 'ver': 0,
        'iat': int(time.time()), 'exp': int(time.time()) + 600},
        'local-dual-gateway-signing-key-0123456789', algorithm='HS256'),
        'Last-Event-ID': encode_cursor(old.cursor)}
    gateways = [Gateway(tmp_path, schema, log.namespace, config, index) for index in range(2)]
    proxy = None
    try:
        for gateway in gateways:
            await gateway.start()
        ports = [urlsplit(gateway.url).port for gateway in gateways]
        proxy = Nginx(tmp_path, ports)
        await proxy.start()
        identities = set()
        async with httpx.AsyncClient(timeout=None) as client:
            for _ in range(2):
                async with client.stream('GET', proxy.url + '/api/tasks/conversations/c/events', headers=headers) as response:
                    assert response.status_code == 200
                    identities.add(response.headers['x-acceptance-upstream'])
                    reader = response.aiter_lines()
                    reset, cursor = await event(reader, 'REALTIME_RESET')
                    assert reset['reason'] == 'trimmed'
                    assert reset['sync_status'] == 'synced'
                    assert reset['snapshots'] == [snapshot.model_dump(mode='json')]
                    assert decode_cursor(cursor) == latest.cursor
                    await event(reader, 'REPLAY_DONE')
            assert identities == {f'127.0.0.1:{port}' for port in ports}
    finally:
        if proxy:
            await proxy.close()
        await asyncio.gather(*(gateway.close() for gateway in gateways))


async def test_nginx_slow_socket_is_bounded_without_blocking_healthy_client(composition, tmp_path):
    """Stop reading a real proxy socket, exceed kernel buffers, then drain it.

    Event producer is scripted; this checks HTTP backpressure and bounded
    subscription eviction, not model throughput or a production memory budget.
    """
    import json
    from pathlib import Path
    from workmate_contracts.realtime import RealtimeEnvelope, encode_cursor

    _, admin, log, manager, _ = composition
    schema = await admin.fetchval('SELECT current_schema()')
    await admin.execute("UPDATE runtime_sessions SET lease_until=now()+interval '5 minutes'")
    log.max_bytes = 33554432  # retain the whole bounded burst for exact reconnect
    ref = await manager.ensure('c')
    origin = (await log.window(ref)).last
    config = tmp_path / 'gateway.yaml'
    config.write_text('product:\n  workspace_assistant:\n    enabled: false\n'
        'realtime:\n  reader_block_ms: 20\n  heartbeat_interval_seconds: 0.1\n'
        '  client_buffer_events: 16\n  client_buffer_bytes: 266240\n'
        '  max_log_bytes: 33554432\n  authorize_interval_seconds: 1.0\n')
    headers = {'Authorization': 'Bearer ' + jwt.encode({'sub': 'u', 'ver': 0,
        'iat': int(time.time()), 'exp': int(time.time()) + 600},
        'local-dual-gateway-signing-key-0123456789', algorithm='HS256'),
        'Last-Event-ID': encode_cursor(origin)}
    gateways = [Gateway(tmp_path, schema, log.namespace, config, index) for index in range(2)]
    proxy = None

    def rss():
        return [int(next(line.split()[1] for line in Path(f'/proc/{g.process.pid}/status').read_text().splitlines()
                         if line.startswith('VmRSS:'))) * 1024 for g in gateways]

    try:
        for gateway in gateways:
            await gateway.start()
        ports = [urlsplit(gateway.url).port for gateway in gateways]
        proxy = Nginx(tmp_path, ports)
        await proxy.start()
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=None))
            route = proxy.url + '/api/tasks/conversations/c/events'
            slow = await stack.enter_async_context(client.stream('GET', route, headers=headers))
            healthy = await stack.enter_async_context(client.stream('GET', route, headers=headers))
            assert slow.status_code == healthy.status_code == 200
            assert slow.headers['x-acceptance-upstream'] != healthy.headers['x-acceptance-upstream']
            slow_lines, healthy_lines = slow.aiter_lines(), healthy.aiter_lines()
            await event(slow_lines, 'REPLAY_DONE')
            await event(healthy_lines, 'REPLAY_DONE')
            same_gateway = await stack.enter_async_context(client.stream('GET', route, headers=headers))
            assert same_gateway.status_code == 200
            assert same_gateway.headers['x-acceptance-upstream'] == slow.headers['x-acceptance-upstream']
            same_lines = same_gateway.aiter_lines()
            await event(same_lines, 'REPLAY_DONE')
            baseline = rss()
            produced = 256
            for index in range(produced):
                envelope = RealtimeEnvelope(event_id=f'pressure-{index}', conversation_id='c',
                    task_id='t', reply_id='reply', source='worker', execution_epoch=1,
                    payload={'type': 'CUSTOM', 'name': 'PRESSURE_FIXTURE',
                             'value': {'index': index, 'padding': 'x' * 65536}})
                await log.append(ref, envelope)
                values = await asyncio.gather(event(healthy_lines, 'REALTIME_EVENT'),
                                              event(same_lines, 'REALTIME_EVENT'))
                assert all(value == envelope.model_dump(mode='json') for value, _ in values)
            after = rss()
            assert all(current - initial < 32 * 1024 * 1024 for current, initial in zip(after, baseline))
            # Draining unblocks the socket send. The evicted subscription must
            # end normally before all produced events, not restart its queue.
            received = []
            last_cursor = headers['Last-Event-ID']
            async with asyncio.timeout(30):
                async for line in slow_lines:
                    if line.startswith('id: '):
                        last_cursor = line[4:]
                    if line.startswith('data: '):
                        value = json.loads(line[6:])
                        if value.get('name') == 'REALTIME_EVENT':
                            received.append(value['value']['payload']['value']['index'])
            assert received == list(range(len(received)))
            assert 0 < len(received) < produced
            # Normal client and both role health endpoints still work.
            for gateway in gateways:
                assert (await client.get(gateway.url + '/health')).status_code == 200
            async with client.stream('GET', route, headers={**headers, 'Last-Event-ID': last_cursor}) as resumed:
                assert resumed.status_code == 200
                reader = resumed.aiter_lines()
                for index in range(len(received), produced):
                    value, _ = await event(reader, 'REALTIME_EVENT')
                    assert value['payload']['value']['index'] == index
                await event(reader, 'REPLAY_DONE')
            (tmp_path / 'slow-client-evidence.json').write_text(json.dumps({
                'produced': produced, 'healthy_received': produced, 'same_gateway_healthy_received': produced, 'slow_received_before_eof': len(received),
                'replayed': produced - len(received), 'gateway_rss_before': baseline,
                'gateway_rss_after': after, 'configured_client_buffer_bytes': 266240,
                'configured_client_buffer_events': 16,
            }, indent=2) + '\n')
    finally:
        if proxy:
            await proxy.close()
        await asyncio.gather(*(gateway.close() for gateway in gateways))
