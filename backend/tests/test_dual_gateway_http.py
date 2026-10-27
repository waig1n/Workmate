"""Independent Gateway processes and real SSE sockets over isolated PG/Redis.

The event producer is scripted. This proves role transport and restart, not
real model, OS sandbox, or complete multi-Worker business acceptance.
"""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import socket
import sys
import time

import httpx
import jwt
import pytest

from test_durable_projection import projection_database  # noqa: F401
from test_business_reset_composition import composition  # noqa: F401
from workmate_product.application.message_snapshots import save_message_snapshot
from workmate_product.projections.store import ProjectedEvent, append_business_events
from workmate_product.realtime.message_accumulator import MessageAccumulator


class Gateway:
    def __init__(self, tmp_path, schema, namespace, config, index):
        self.path = tmp_path / f'gateway-{index}.log'
        self.schema, self.namespace, self.config = schema, namespace, config
        self.process = self.output = None

    async def start(self, *, port=0):
        endpoint = socket.socket()
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        endpoint.bind(('127.0.0.1', port))
        endpoint.listen()
        endpoint.set_inheritable(True)
        self.url = f'http://127.0.0.1:{endpoint.getsockname()[1]}'
        self.output = self.path.open('ab')
        try:
            self.process = await asyncio.create_subprocess_exec(sys.executable,
                str(Path(__file__).with_name('run_realtime_gateway.py')),
                '--schema', self.schema, '--namespace', self.namespace,
                '--config', str(self.config), '--socket-fd', str(endpoint.fileno()),
                pass_fds=(endpoint.fileno(),), env=os.environ.copy(),
                stdout=self.output, stderr=self.output)
        except BaseException:
            self.output.close()
            raise
        finally:
            endpoint.close()
        async with httpx.AsyncClient(timeout=1) as client:
            async with asyncio.timeout(25):
                while True:
                    assert self.process.returncode is None, self.path.read_text()
                    try:
                        if (await client.get(self.url + '/health')).status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.05)

    async def close(self, *, graceful=False):
        forced = False
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except TimeoutError:
                forced = True
                self.process.kill()
                await self.process.wait()
        if self.output is not None:
            self.output.close()
        if graceful:
            assert not forced, self.path.read_text()
            # Uvicorn re-raises its handled SIGTERM after graceful shutdown.
            assert self.process.returncode in (0, -15), self.path.read_text()


async def event(lines, name):
    token = None
    async with asyncio.timeout(8):
        async for line in lines:
            if line.startswith('id: '):
                token = line[4:]
            if line.startswith('data: '):
                value = json.loads(line[6:])
                if value.get('name') == name:
                    return value['value'], token
            if not line:
                token = None
    raise AssertionError('SSE ended before ' + name)


async def assert_revoked(lines):
    async with asyncio.timeout(5):
        assert [line async for line in lines if line and not line.startswith(':')] == []


@pytest.mark.parametrize('revocation', ['token_version', 'disabled', 'deleted'])
async def test_dual_gateway_http_broadcast_replay_restart_and_revocation(
    composition, tmp_path, revocation,
):
    sessions, admin, log, manager, _ = composition
    schema = await admin.fetchval('SELECT current_schema()')
    config = tmp_path / 'gateway.yaml'
    config.write_text('product:\n  workspace_assistant:\n    enabled: false\n'
        'realtime:\n  authorize_interval_seconds: 0.1\n'
        '  reader_block_ms: 50\n  heartbeat_interval_seconds: 0.1\n'
        'command_queue:\n  poll_interval_seconds: 0.05\n')
    await admin.execute("UPDATE runtime_sessions SET lease_until=now()+interval '5 minutes'")
    ref = await manager.ensure('c')
    producer = MessageAccumulator('c', 't', 'reply', 1)
    receipt = None
    for raw in ({'type':'TEXT_BLOCK_START','block_id':'body'},
                {'type':'TEXT_BLOCK_DELTA','block_id':'body','delta':'initial'}):
        for envelope in producer.apply(raw):
            receipt = await log.append(ref, envelope)
    baseline = producer.capture('partial', receipt.cursor, 0)
    async with sessions.begin() as db:
        await save_message_snapshot(db, baseline, owner_id='worker')
    token = jwt.encode({'sub':'u','ver':0,'iat':int(time.time()),
        'exp':int(time.time())+600}, 'local-dual-gateway-signing-key-0123456789',
        algorithm='HS256')
    headers = {'Authorization':'Bearer ' + token}
    gateways = [Gateway(tmp_path, schema, log.namespace, config, index) for index in range(2)]
    route = '/api/tasks/conversations/c/events'
    try:
        starts = await asyncio.gather(*(gateway.start() for gateway in gateways),
            return_exceptions=True)
        for result in starts:
            if isinstance(result, BaseException):
                raise result
        assert gateways[0].process.pid != gateways[1].process.pid
        async with AsyncExitStack() as connections:
            clients = [await connections.enter_async_context(httpx.AsyncClient(timeout=None)) for _ in range(4)]
            responses = []
            for index, client in enumerate(clients):
                response = await connections.enter_async_context(client.stream('GET',
                    gateways[index // 2].url + route, headers=headers))
                assert response.status_code == 200, await response.aread()
                responses.append(response)
            readers = [response.aiter_lines() for response in responses]
            for reader in readers:
                reset, _ = await event(reader, 'REALTIME_RESET')
                assert reset['snapshots'] == [baseline.model_dump(mode='json')]
                await event(reader, 'REPLAY_DONE')
            # Readers and publishers briefly own transactions between queries.
            # SSE must not keep an idle transaction for the connection lifetime.
            await asyncio.sleep(1.2)
            stale_transactions = await admin.fetch("SELECT pid,query,xact_start FROM pg_stat_activity "
                "WHERE application_name=ANY($1) AND state='idle in transaction' "
                "AND xact_start < clock_timestamp()-interval '1 second'",
                [f'q4_gateway_{gateway.process.pid}' for gateway in gateways])
            assert not stale_transactions, stale_transactions
            live = producer.apply({'type':'TEXT_BLOCK_DELTA','block_id':'body','delta':' live'})[0]
            await log.append(ref, live)
            seen = await asyncio.gather(*(event(reader, 'REALTIME_EVENT') for reader in readers))
            assert all(value == live.model_dump(mode='json') for value, _ in seen)
            assert len({cursor for _, cursor in seen}) == 1
            cursor = seen[0][1]
            async with sessions.begin() as db:
                await append_business_events(db, 'c', operation_key='http-notice',
                    task_id='t', reply_id='reply', items=[ProjectedEvent(key='notice',
                        event_type='CUSTOM', payload={'type':'CUSTOM','name':'NOTICE','value':{'ok':True}})])
            notices = await asyncio.gather(*(event(reader, 'REALTIME_EVENT') for reader in readers))
            assert all(value['payload']['name'] == 'NOTICE' for value, _ in notices)
            assert len({value['event_id'] for value, _ in notices}) == 1
            # Closing one Gateway cannot cancel another process's reader.
            await responses[0].aclose()
            await responses[1].aclose()
            retired_pid = gateways[0].process.pid
            await gateways[0].close(graceful=True)
            assert await admin.fetchval('SELECT count(*) FROM pg_stat_activity WHERE application_name=$1',
                f'q4_gateway_{retired_pid}') == 0
            later = producer.apply({'type':'TEXT_BLOCK_DELTA','block_id':'body','delta':' after restart'})[0]
            await log.append(ref, later)
            peer = await asyncio.gather(*(event(reader, 'REALTIME_EVENT') for reader in readers[2:]))
            assert all(value == later.model_dump(mode='json') for value, _ in peer)
            await gateways[0].start()
            async with clients[0].stream('GET', gateways[0].url + route,
                headers={**headers, 'Last-Event-ID':cursor}) as resumed:
                assert resumed.status_code == 200, await resumed.aread()
                replay = resumed.aiter_lines()
                notice, _ = await event(replay, 'REALTIME_EVENT')
                tail, _ = await event(replay, 'REALTIME_EVENT')
                assert notice['payload']['name'] == 'NOTICE'
                assert tail == later.model_dump(mode='json')
                await event(replay, 'REPLAY_DONE')
                if revocation == 'token_version':
                    await admin.execute("UPDATE users SET token_version=1 WHERE id='u'")
                elif revocation == 'disabled':
                    await admin.execute("UPDATE users SET status='disabled' WHERE id='u'")
                else:
                    await admin.execute("UPDATE conversations SET deleted_at=now() WHERE id='c'")
                results = await asyncio.gather(*(assert_revoked(reader)
                    for reader in (replay, *readers[2:])), return_exceptions=True)
                assert not [result for result in results if isinstance(result, BaseException)], results
            async with httpx.AsyncClient() as client:
                expected = 404 if revocation == 'deleted' else 401
                assert (await client.get(gateways[1].url + route, headers=headers)).status_code == expected
        assert await admin.fetchval("SELECT count(*) FROM realtime_business_outbox WHERE status='published'") == 1
        assert await admin.fetchval('SELECT count(*) FROM runtime_events') == 0
        await asyncio.gather(*(gateway.close(graceful=True) for gateway in gateways))
    finally:
        await asyncio.gather(*(gateway.close() for gateway in gateways))
    assert await admin.fetchval('SELECT count(*) FROM pg_stat_activity WHERE application_name=ANY($1)',
        [f'q4_gateway_{gateway.process.pid}' for gateway in gateways]) == 0
