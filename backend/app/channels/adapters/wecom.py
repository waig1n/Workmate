"""WeCom API bot protocol, using the pinned official SDK command definitions.

The SDK emits callbacks synchronously and does not expose an inbound ACK. The
transport serializes durable ingestion and bounds frames instead of detaching
callback tasks. WeCom replay guarantees still require real-account acceptance.
"""
import asyncio
import json
import uuid

from .base import safe_transport, Adapter, ChannelTransportError, MAX_RESPONSE
from .parsing import normalize


class WecomAdapter(Adapter):
    def __init__(self, credentials):
        super().__init__(credentials)
        self._pending = {}

    async def run(self, emit):
        import websockets
        from aibot.types import WsCmd

        async with websockets.connect('wss://openws.work.weixin.qq.com', max_size=MAX_RESPONSE,
                # Official SDK uses protocol-level JSON heartbeats, not WS control ping/pong.
                ping_interval=None, ping_timeout=None,
                max_queue=16, open_timeout=20, close_timeout=5, proxy=None) as socket:
            self._socket = socket
            await socket.send(json.dumps({'cmd': WsCmd.SUBSCRIBE,
                'headers': {'req_id': str(uuid.uuid4())},
                'body': {'bot_id': self._credentials['bot_id'], 'secret': self._credentials['secret']}}))
            auth = json.loads(await asyncio.wait_for(socket.recv(), 20))
            self.check(auth)
            self.ready.set()
            heartbeat = asyncio.create_task(self._heartbeat(socket))
            try:
                async for raw in socket:
                    frame = json.loads(raw)
                    req_id = (frame.get('headers') or {}).get('req_id')
                    # Stream replies reuse the callback req_id. A replayed
                    # callback is still input, never an outbound acknowledgement.
                    if frame.get('cmd') == WsCmd.CALLBACK:
                        await emit(normalize('wecom', frame))
                    elif req_id in self._pending and 'errcode' in frame:
                        future = self._pending[req_id]
                        if not future.done():
                            future.set_result(frame)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                self._socket = None
                for pending in self._pending.values():
                    if not pending.done():
                        pending.set_exception(ChannelTransportError('channel_disconnected'))

    async def _heartbeat(self, socket):
        while True:
            await asyncio.sleep(25)
            await socket.send(json.dumps({'cmd': 'ping', 'headers': {'req_id': str(uuid.uuid4())}}))

    @safe_transport
    async def send(self, target, text):
        from aibot.types import WsCmd

        if self._socket is None:
            raise ChannelTransportError('channel_disconnected')
        req_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._socket.send(json.dumps({'cmd': WsCmd.SEND_MSG, 'headers': {'req_id': req_id},
                'body': {'chatid': target['chat_id'], 'msgtype': 'markdown', 'markdown': {'content': text}}}))
            result = await asyncio.wait_for(future, 20)
            self.check(result)
            return (result.get('body') or {}).get('msgid')
        finally:
            self._pending.pop(req_id, None)

    def supports_stream(self, target):
        return bool((target.get('reply_context') or {}).get('req_id'))

    @safe_transport
    async def stream(self, target, text, *, stream_id, receipt=None, finish=False, sequence=1):
        from aibot.types import WsCmd

        req_id = (target.get('reply_context') or {}).get('req_id')
        if not req_id:
            raise ChannelTransportError('stream_context_missing')
        if self._socket is None:
            raise ChannelTransportError('channel_disconnected')
        # The official protocol reuses callback req_id; serialize its updates.
        if req_id in self._pending:
            raise ChannelTransportError('stream_update_in_progress')
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._socket.send(json.dumps({'cmd': WsCmd.RESPONSE,
                'headers': {'req_id': req_id}, 'body': {'msgtype': 'stream',
                'stream': {'id': stream_id, 'content': text, 'finish': finish}}}))
            result = await asyncio.wait_for(future, 20)
            self.check(result)
            return stream_id
        finally:
            self._pending.pop(req_id, None)
