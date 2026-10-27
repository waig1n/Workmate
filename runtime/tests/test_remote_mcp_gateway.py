import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.asyncio
async def test_remote_envelope_keeps_secrets_out_of_exec_and_rejects_replay():
    from workmate_runtime.mcp.remote import RemoteGatewayClient
    from workmate_runtime.mcp.remote_server import build_remote_app
    from workmate_runtime.mcp.server import GatewayState

    key = b'k' * 32
    state = GatewayState()
    app = build_remote_app(state, key=key, instance_nonce='instance')
    observed = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as http:
        class Backend:
            async def exec_shell(self, command, **kwargs):
                observed.append(command)
                response = await http.post('/envelope', content=command[-1])
                return SimpleNamespace(exit_code=0, stdout=response.content, stderr=b'')
            async def write_file(self, *args):
                raise AssertionError('No credential request files')
        client = RemoteGatewayClient(Backend(), 12345, key, 'instance')
        assert await client.health()
        status, raw = await client.exec_request('POST', '/mcps', body={'secret': 'private-value'})
        assert status == 400
        serialized = json.dumps(observed)
        assert 'private-value' not in serialized and key.hex() not in serialized
        replay = await http.post('/envelope', content=observed[-1][-1])
        assert replay.status_code == 409
        corrupt = await http.post('/envelope', content='invalid')
        assert corrupt.status_code == 400
        assert (await http.get('/health')).status_code == 404
        await client.reset()
        await client.aclose()
        with pytest.raises(RuntimeError):
            await client.exec_request('GET', '/health')


@pytest.mark.asyncio
async def test_remote_reset_closes_actors_and_retains_unresolved_actor():
    from workmate_runtime.mcp.remote_server import build_remote_app
    from workmate_runtime.mcp.remote_wire import encrypt_message, decrypt_message
    from workmate_runtime.mcp.server import GatewayState

    class Actor:
        def __init__(self): self.fail = True
        async def submit(self, operation):
            assert operation == 'close'
            if self.fail: raise RuntimeError('secret')
    actor = Actor()
    state = GatewayState()
    state.actors['identity'] = actor
    key = b'k' * 32
    app = build_remote_app(state, key=key, instance_nonce='instance')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as http:
        async def reset(seq):
            body = encrypt_message(key, 'instance', 'request', {'seq': seq, 'method': 'POST', 'path': '/reset'})
            response = await http.post('/envelope', content=body)
            return decrypt_message(key, 'instance', f'response:{seq}', response.content)
        assert (await reset(1))['status'] == 409
        assert state.actors['identity'] is actor
        actor.fail = False
        assert (await reset(2))['status'] == 200
        assert not state.actors


def test_stdio_factory_wraps_command_without_changing_http_or_input():
    from workmate_runtime.mcp.remote_server import remote_client_factory, request_epoch
    spec = {'name': 'test', 'mcp_config': {'type': 'stdio_mcp', 'command': 'python', 'args': ['worker.py'], 'env': {'TOKEN': 'secret'}}}
    factory = remote_client_factory('/root/control/launch.py', 62000, '/usr/bin/python3', validate=lambda item: item)
    with pytest.raises(ValueError, match='epoch'):
        factory(spec)
    token = request_epoch.set('a' * 64)
    try:
        wrapped = factory(spec)['mcp_config']
    finally:
        request_epoch.reset(token)
    assert wrapped['command'] == '/usr/bin/python3'
    assert wrapped['args'][-2:] == ['python', 'worker.py']
    assert '/root/control/launch.py' in wrapped['args']
    assert wrapped['args'][3:5] == ['--epoch', 'a' * 64]
    assert spec['mcp_config']['command'] == 'python' and spec['mcp_config']['args'] == ['worker.py']
    assert set(wrapped['env']) == {'WORKMATE_MCP_CHILD_ENV'}
    assert json.loads(wrapped['env']['WORKMATE_MCP_CHILD_ENV']) == {'TOKEN': 'secret'}
    http = {'name': 'test', 'mcp_config': {'type': 'http_mcp', 'url': 'https://example.com'}}
    assert factory(http) == http


@pytest.mark.asyncio
async def test_wrong_instance_and_oversized_payload_are_rejected_before_execution():
    from workmate_runtime.mcp.remote import RemoteGatewayClient
    from workmate_runtime.mcp.remote_wire import encrypt_message, decrypt_message
    from workmate_runtime.mcp.gateway import MCPGatewayError
    class Backend:
        async def exec_shell(self, *args, **kwargs):
            raise AssertionError('Invalid request must not launch')
    client = RemoteGatewayClient(Backend(), 12345, b'k' * 32, 'instance')
    with pytest.raises(MCPGatewayError):
        await client.exec_request('POST', '/mcps', body='x' * (20 * 1024 * 1024))
    envelope = encrypt_message(b'k' * 32, 'instance', 'request', {'seq': 1})
    with pytest.raises(Exception):
        decrypt_message(b'k' * 32, 'other', 'request', envelope)


@pytest.mark.asyncio
async def test_real_backend_relay_transfers_large_encrypted_payload_and_removes_spill(tmp_path):
    import socket
    import sys
    import uvicorn
    from workmate_runtime.mcp.remote import RemoteGatewayClient
    from workmate_runtime.mcp.remote_server import build_remote_app
    from workmate_runtime.mcp.server import GatewayState

    key = b'k' * 32
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(
        build_remote_app(GatewayState(), key=key, instance_nonce='instance'),
        access_log=False, log_level='critical'))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    class Backend:
        python = sys.executable
        async def exec_shell(self, command, **kwargs):
            process = await asyncio.create_subprocess_exec(*command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            return SimpleNamespace(exit_code=process.returncode, stdout=stdout, stderr=stderr)
        async def write_file(self, path, data):
            assert b'private-value' not in data
            from pathlib import Path
            Path(path).write_bytes(data)
    try:
        for _ in range(100):
            if server.started: break
            await asyncio.sleep(.01)
        client = RemoteGatewayClient(Backend(), listener.getsockname()[1], key, 'instance')
        client.control_dir = str(tmp_path)
        assert await client.health()
        status, _ = await client.exec_request('POST', '/mcps', body={'secret': 'private-value' * 10000})
        assert status == 400
        assert list(tmp_path.iterdir()) == []
        await client.reset()
        await client.aclose()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)


@pytest.mark.asyncio
async def test_cancelled_bootstrap_joins_creation_then_stops_exact_child():
    from workmate_runtime.mcp.remote import start_remote_gateway, _START, _STOP
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Control:
        python = '/usr/bin/python3'
        async def write_file(self, path, data):
            import io, zipfile
            assert path == '/root/control/remote_gateway.pyz'
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                assert '__main__.py' in archive.namelist()
                assert 'workmate_runtime/mcp/remote_server.py' in archive.namelist()
                assert 'workmate_runtime/mcp/remote_wire.py' in archive.namelist()
        async def exec_shell(self, command, **kwargs):
            calls.append(command)
            if command[3] == _START:
                started.set()
                await release.wait()
                return SimpleNamespace(exit_code=0, stdout=b'{"pid":4321,"start":"42","port":12345}')
            assert command[3] == _STOP and command[-2:] == ['4321', '42']
            return SimpleNamespace(exit_code=0, stdout=b'')
    task = asyncio.create_task(start_remote_gateway(Control(), '/venv/bin/python', '/root/control', 62001))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_queued_request_retains_original_epoch_after_next_task_bind():
    from workmate_runtime.mcp.remote import RemoteGatewayClient
    from workmate_runtime.mcp.remote_wire import encrypt_message, decrypt_message
    key = b'k' * 32
    observed = []
    class Backend:
        async def exec_shell(self, command, **kwargs):
            payload = decrypt_message(key, 'instance', 'request', command[-1])
            observed.append(payload['epoch'])
            raw = encrypt_message(key, 'instance', f"response:{payload['seq']}",
                {'seq': payload['seq'], 'status': 200, 'body': ''})
            return SimpleNamespace(exit_code=0, stdout=raw)
    client = RemoteGatewayClient(Backend(), 12345, key, 'instance')
    with pytest.raises(RuntimeError):
        client.bind_epoch(7)
    client.bind_epoch('a' * 64)
    await client._lock.acquire()
    request = asyncio.create_task(client.exec_request('POST', '/mcps', body={}))
    await asyncio.sleep(0)
    client.bind_epoch('b' * 64)
    client._lock.release()
    assert await request == (200, b'')
    assert observed == ['a' * 64]
