"""Loopback encrypted entrypoint; only decrypted requests reach the existing app.

The sandbox bundle supplies this module and its explicit gateway dependencies.
The AES key arrives on stdin once and never appears in a command or disk file
during Task execution. Request sequence numbers prevent ciphertext replay.
"""
import base64
import hashlib
import json
import os
import re
import socket
import sys
from copy import deepcopy
from contextvars import ContextVar

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

from .server import GatewayState, build_app
from .http_transport import governed_client_factory
from .remote_wire import (
    MAX_REMOTE_BODY, MAX_REMOTE_ENVELOPE, encrypt_message, decrypt_message,
)

request_epoch = ContextVar('remote_gateway_request_epoch', default=None)


def remote_client_factory(launcher, uid, python, *, validate=None):
    if validate is None:
        validate = governed_client_factory

    def factory(spec):
        spec = deepcopy(spec)
        config = spec.get('mcp_config', {})
        if config.get('type') == 'stdio_mcp':
            epoch = request_epoch.get()
            if not isinstance(epoch, str) or re.fullmatch(r'[0-9a-f]{64}', epoch) is None:
                raise ValueError('MCP subprocesses require the original Task epoch')
            command = config['command']
            args = config.get('args') or []
            # Arbitrary LD_PRELOAD/PYTHONPATH must never reach root Python.
            # The trusted launcher installs this environment only after it
            # irreversibly drops privileges and sets no_new_privs.
            child_env = config.get('env') or {}
            config['env'] = {'WORKMATE_MCP_CHILD_ENV': json.dumps(child_env)}
            config['command'] = python
            config['args'] = ['-I', launcher, str(uid), '--epoch', str(epoch), '--', command, *args]
        return validate(spec)
    return factory


def build_remote_app(state, *, key, instance_nonce):
    import asyncio
    from contextlib import asynccontextmanager

    token = hashlib.sha256(key + b'workmate-gateway-auth').hexdigest()
    inner = build_app(state, auth_token=token, instance_nonce=instance_nonce)
    last_sequence = 0
    admission = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await state.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post('/envelope')
    async def envelope(request: Request):
        nonlocal last_sequence
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_REMOTE_ENVELOPE:
                return PlainTextResponse('Request too large', status_code=413)
            raw.extend(chunk)
        try:
            payload = decrypt_message(key, instance_nonce, 'request', bytes(raw))
            seq = payload['seq']
            method, path = payload['method'], payload['path']
            if (type(seq) is not int or seq < 1 or method not in {'GET', 'POST', 'DELETE'}
                    or not isinstance(path, str) or not path.startswith('/')
                    or path.startswith('//') or '?' in path or '#' in path
                    or len(json.dumps(payload.get('body')).encode()) > MAX_REMOTE_BODY):
                raise ValueError()
        except Exception:
            return PlainTextResponse('Invalid envelope', status_code=400)
        # Claim a sequence before executing, including requests whose caller
        # disappears. Retrying old ciphertext never repeats side effects.
        async with admission:
            if seq <= last_sequence:
                return PlainTextResponse('Replayed envelope', status_code=409)
            last_sequence = seq
            context_token = request_epoch.set(payload.get('epoch'))
            try:
                if path == '/reset' and method == 'POST':
                    async with state.lock:
                        await state.close()
                    status, body = 200, b'{"ok":true}'
                else:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=inner),
                            base_url='http://gateway', trust_env=False) as client:
                        response = await client.request(method, path, params=payload.get('params'),
                            json=payload.get('body'), headers={'Authorization': f'Bearer {token}'})
                    status, body = response.status_code, response.content
                if len(body) > MAX_REMOTE_BODY:
                    status, body = 502, b'Gateway response exceeds the transport limit'
            except Exception:
                status, body = 409, b'Gateway operation has unresolved resources'
            finally:
                request_epoch.reset(context_token)
        result = {'seq': seq, 'status': status, 'body': base64.b64encode(body).decode()}
        return Response(encrypt_message(key, instance_nonce, f'response:{seq}', result),
                        media_type='application/octet-stream')

    return app


async def run_remote_gateway(socket_fd, launcher, uid, python):
    import uvicorn
    init = json.loads(sys.stdin.buffer.readline(4096))
    key = base64.b64decode(init['key'], validate=True)
    if len(key) != 32 or os.geteuid() != 0:
        raise RuntimeError('Remote gateway requires a root control channel and AES-256 key')
    state = GatewayState(remote_client_factory(launcher, uid, python))
    app = build_remote_app(state, key=key, instance_nonce=init['nonce'])
    listener = socket.socket(fileno=socket_fd)
    await uvicorn.Server(uvicorn.Config(app, access_log=False, log_level='critical')).serve(sockets=[listener])


def main():
    import asyncio
    asyncio.run(run_remote_gateway(int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]))


if __name__ == "__main__":
    main()
