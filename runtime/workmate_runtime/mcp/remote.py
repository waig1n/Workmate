"""Encrypted Backend relay for the remote memory-only MCP gateway."""
import asyncio
import base64
import json
import re
import secrets

from agentscope.workspace._gateway_client import GatewayClient

from .bundle import build_gateway_bundle
from .gateway import MCPGatewayError
from .remote_wire import (
    MAX_REMOTE_BODY, MAX_REMOTE_ENVELOPE, encrypt_message, decrypt_message,
)


_RELAY = '''import os,sys,urllib.request
if len(sys.argv)>3 and sys.argv[3]=='file':
    try:
        with open(sys.argv[2],'rb') as source: data=source.read(41943041)
    finally: os.unlink(sys.argv[2])
else: data=sys.argv[2].encode()
if len(data)>41943040: raise RuntimeError('Gateway request too large')
request=urllib.request.Request('http://127.0.0.1:'+sys.argv[1]+'/envelope',
    data=data,headers={'Content-Type':'application/octet-stream'},method='POST')
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=300) as response:
    body=response.read(41943041)
    if len(body)>41943040: raise RuntimeError('Gateway response too large')
    sys.stdout.buffer.write(body)
'''

_START = '''import base64,json,os,socket,subprocess,sys
listener=socket.socket()
listener.bind(('127.0.0.1',0))
listener.listen(128)
process=subprocess.Popen([sys.argv[1],'-I',sys.argv[2],str(listener.fileno()),
    sys.argv[3],sys.argv[4],sys.argv[5]],stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(listener.fileno(),),
    start_new_session=True,cwd='/')
process.stdin.write(base64.b64decode(sys.argv[6])+b'\\n')
process.stdin.close()
start=open('/proc/'+str(process.pid)+'/stat').read().rsplit(')',1)[1].split()[19]
print(json.dumps({'pid':process.pid,'start':start,'port':listener.getsockname()[1]}))
'''

_STOP = '''import os,signal,sys,time
pid=int(sys.argv[1]); expected=sys.argv[2]
def alive():
    try:
        fields=open('/proc/'+str(pid)+'/stat').read().rsplit(')',1)[1].split()
    except FileNotFoundError: return False
    if fields[19]!=expected: raise RuntimeError('Gateway process identity changed')
    return fields[0]!='Z'
if alive(): os.kill(pid,signal.SIGTERM)
until=time.monotonic()+3
while alive() and time.monotonic()<until: time.sleep(.02)
if alive(): os.kill(pid,signal.SIGKILL)
until=time.monotonic()+2
while alive() and time.monotonic()<until: time.sleep(.02)
if alive(): raise RuntimeError('Gateway process did not stop')
'''


async def _join_control(operation):
    """Retain a management launch across cancellation until its identity is known."""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    return task.result(), cancelled


class RemoteGatewayClient(GatewayClient):
    def __init__(self, control_backend, port, key, nonce):
        if (len(key) != 32 or not isinstance(nonce, str) or not nonce
                or type(port) is not int or not 1 <= port <= 65535):
            raise MCPGatewayError('Invalid remote gateway identity')
        super().__init__(control_backend, port, instance_nonce=nonce, timeout=300)
        self._key = key
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._process = None
        self.control_dir = None
        self._epoch = None

    def bind_epoch(self, epoch):
        if not isinstance(epoch, str) or re.fullmatch(r'[0-9a-f]{64}', epoch) is None or self._closed:
            raise MCPGatewayError('A live Task execution epoch is required')
        self._epoch = epoch

    async def exec_request(self, method, path, *, params=None, body=None, include_auth=True):
        epoch = self._epoch
        # Authentication and instance binding are always provided by AEAD,
        # including the inherited GatewayClient.health() call.
        if (self._closed or method not in {'GET', 'POST', 'DELETE'}
                or not isinstance(path, str) or not path.startswith('/')
                or path.startswith('//') or '?' in path or '#' in path):
            raise MCPGatewayError('Invalid or closed remote gateway request')
        if len(json.dumps(body).encode()) > MAX_REMOTE_BODY:
            raise MCPGatewayError('MCP gateway request exceeds the transport limit')
        async with self._lock:
            self._sequence += 1
            seq = self._sequence
            payload = {'seq': seq, 'method': method, 'path': path, 'params': params, 'body': body, 'epoch': epoch}
            try:
                ciphertext = encrypt_message(self._key, self.instance_nonce, 'request', payload).decode()
                relay_args = [str(self.gateway_port), ciphertext]
                if len(ciphertext) > 48 * 1024:
                    if not self.control_dir:
                        raise ValueError('Protected gateway control directory is required')
                    # Only ciphertext may spill. This avoids OS argv limits;
                    # the protected relay unlinks it before doing network I/O.
                    encrypted_path = self.control_dir + '/envelope-' + secrets.token_hex(16)
                    await self.backend.write_file(encrypted_path, ciphertext.encode())
                    relay_args = [str(self.gateway_port), encrypted_path, 'file']
                result = await self.backend.exec_shell(
                    [getattr(self.backend, 'python', '/usr/bin/python3'), '-I', '-c', _RELAY,
                     *relay_args], timeout=self.timeout)
                if result.exit_code != 0 or len(result.stdout) > MAX_REMOTE_ENVELOPE:
                    raise ValueError()
                response = decrypt_message(self._key, self.instance_nonce, f'response:{seq}', result.stdout)
                raw = base64.b64decode(response['body'], validate=True)
                if (response['seq'] != seq or type(response['status']) is not int
                        or not 100 <= response['status'] <= 599 or len(raw) > MAX_REMOTE_BODY):
                    raise ValueError()
                return response['status'], raw
            except Exception:
                raise MCPGatewayError('Remote gateway did not produce an authenticated response') from None

    async def reset(self):
        """Close every actor before UID cleanup; failures retain gateway state."""
        status, _ = await self.exec_request('POST', '/reset')
        if status != 200:
            raise MCPGatewayError('Remote MCP actors still have unresolved resources')

    async def stop(self):
        """Release the gateway only after all actor transports closed successfully."""
        if self._closed:
            return
        await self.reset()
        if self._process is not None:
            result = await self.backend.exec_shell(
                [getattr(self.backend, 'python', '/usr/bin/python3'), '-I', '-c', _STOP,
                 str(self._process['pid']), self._process['start']], timeout=10)
            if result.exit_code != 0:
                raise MCPGatewayError('Remote gateway shutdown could not be verified')
        await self.aclose()

    async def aclose(self):
        self._closed = True
        self._key = b''


async def start_remote_gateway(control, gateway_python, control_dir, uid):
    """Called only before Task admission, after the execution UID probe is clear.

    The bootstrap command temporarily contains initial key material; it must
    finish before any untrusted UID process can start. The child receives the
    key through stdin. No key or MCP configuration is written into a file.
    Provider shutdown calls returned_client.stop(), Task shutdown calls reset().
    """
    directory = str(control_dir).rstrip('/')
    if not directory.startswith('/') or type(uid) is not int or uid < 1:
        raise MCPGatewayError('Protected gateway directory and execution UID are required')
    script = directory + '/remote_gateway.pyz'
    await control.write_file(script, build_gateway_bundle(remote=True))
    key, nonce = secrets.token_bytes(32), secrets.token_hex(24)
    init = base64.b64encode(json.dumps({'key': base64.b64encode(key).decode(), 'nonce': nonce}).encode()).decode()
    result, cancelled = await _join_control(control.exec_shell(
        [control.python, '-I', '-c', _START, gateway_python, script,
         directory + '/launch.py', str(uid), control.python, init], timeout=30))
    if result.exit_code != 0:
        raise MCPGatewayError('Remote gateway bootstrap did not complete')
    try:
        process = json.loads(result.stdout)
        if type(process['pid']) is not int or process['pid'] <= 1 or not str(process['start']).isdigit():
            raise ValueError()
        client = RemoteGatewayClient(control, process['port'], key, nonce)
        client._process = process
        client.control_dir = directory
    except Exception:
        raise MCPGatewayError('Remote gateway bootstrap identity is unavailable') from None
    try:
        if cancelled:
            raise asyncio.CancelledError()
        for _ in range(100):
            if await client.health():
                return client
            await asyncio.sleep(.1)
        raise MCPGatewayError('Remote gateway did not become ready')
    except BaseException:
        # A failed health probe cannot authenticate /reset; no Task was
        # admitted, so stop only the exact root process created above.
        stop, _ = await _join_control(control.exec_shell(
            [control.python, '-I', '-c', _STOP, str(process['pid']), process['start']], timeout=10))
        if stop.exit_code != 0:
            raise MCPGatewayError('Failed remote gateway bootstrap retained unresolved resources') from None
        raise
