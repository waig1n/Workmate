"""Feishu websocket frames from pinned lark-oapi; bounded async IO and durable ACK.

Avoid Client.start(): its process-global event loop cannot safely host multiple
connections. The wire codec is the official SDK protobuf, with no SDK threads.
"""
import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

from .base import safe_transport, Adapter, ChannelTransportError, MAX_RESPONSE, websocket_url
from .parsing import normalize


class FeishuAdapter(Adapter):
    def __init__(self, credentials):
        super().__init__(credentials)
        self._bot_id = ''
        self._fragments = {}

    async def token(self):
        result = await self.request('POST', 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': self._credentials['app_id'], 'app_secret': self._credentials['app_secret']})
        self.check(result, 'code')
        return result['tenant_access_token']

    async def run(self, emit):
        import websockets
        from lark_oapi.ws.pb.pbbp2_pb2 import Frame

        token = await self.token()
        info = await self.request('GET', 'https://open.feishu.cn/open-apis/bot/v3/info/',
            headers={'Authorization': 'Bearer ' + token})
        self.check(info, 'code')
        self._bot_id = info['bot']['open_id']
        data = await self.request('POST', 'https://open.feishu.cn/callback/ws/endpoint',
            json={'AppID': self._credentials['app_id'], 'AppSecret': self._credentials['app_secret']})
        self.check(data, 'code')
        endpoint = websocket_url(data['data']['URL'], 'feishu.cn')
        service_id = int(parse_qs(urlsplit(endpoint).query)['service_id'][0])
        interval = max(5, min(60, (data['data'].get('ClientConfig') or {}).get('PingInterval', 30)))
        async with websockets.connect(endpoint, max_size=MAX_RESPONSE, max_queue=16,
                open_timeout=20, close_timeout=5, proxy=None) as socket:
            self._socket = socket
            self.ready.set()
            next_ping = time.monotonic()
            while True:
                if time.monotonic() >= next_ping:
                    ping = Frame(SeqID=0, LogID=0, service=service_id, method=0)
                    ping.headers.add(key='type', value='ping')
                    await socket.send(ping.SerializeToString())
                    next_ping = time.monotonic() + interval
                try:
                    raw = await asyncio.wait_for(socket.recv(), max(0.01, next_ping - time.monotonic()))
                except TimeoutError:
                    continue
                frame = Frame()
                frame.ParseFromString(raw)
                await self.handle_frame(frame, socket, emit)

    async def handle_frame(self, frame, socket, emit):
        if frame.method != 1:
            return
        headers = {header.key: header.value for header in frame.headers}
        if headers.get('type') != 'event':
            return
        payload = frame.payload
        total = int(headers.get('sum', '1'))
        seq = int(headers.get('seq', '0'))
        if total > 1:
            if not 1 < total <= 64 or not 0 <= seq < total:
                raise ChannelTransportError('invalid_platform_fragments')
            now = time.monotonic()
            self._fragments = {key: value for key, value in self._fragments.items() if now - value[0] < 30}
            key = headers['message_id']
            if key not in self._fragments:
                if len(self._fragments) >= 16:
                    raise ChannelTransportError('platform_fragments_overflow')
                self._fragments[key] = (now, {})
            chunks = self._fragments[key][1]
            chunks[seq] = payload
            if sum(map(len, chunks.values())) > MAX_RESPONSE:
                raise ChannelTransportError('platform_response_too_large')
            if len(chunks) < total:
                return
            payload = b''.join(chunks[index] for index in range(total))
            del self._fragments[key]
        event = json.loads(payload)
        sender = (event.get('event') or {}).get('sender') or {}
        if ((event.get('header') or {}).get('event_type') == 'im.message.receive_v1'
                and sender.get('sender_type', 'user') == 'user'
                and (sender.get('sender_id') or {}).get('open_id') != self._bot_id):
            await emit(normalize('feishu', event, bot_id=self._bot_id))
        frame.payload = b'{"code":200}'
        await socket.send(frame.SerializeToString())

    @safe_transport
    async def send(self, target, text):
        token = await self.token()
        result = await self.request('POST', 'https://open.feishu.cn/open-apis/im/v1/messages',
            params={'receive_id_type': 'open_id' if target.get('receive_id_type') == 'open_id' else 'chat_id'}, headers={'Authorization': 'Bearer ' + token},
            json={'receive_id': target['chat_id'], 'msg_type': 'text',
                  'content': json.dumps({'text': text}, ensure_ascii=False)})
        self.check(result, 'code')
        return (result.get('data') or {}).get('message_id')

    def supports_stream(self, target):
        return True

    @safe_transport
    async def stream(self, target, text, *, stream_id, receipt=None, finish=False, sequence=1):
        from urllib.parse import quote

        token = await self.token()
        headers = {'Authorization': 'Bearer ' + token}
        # Shared-card updates reach only the original recipients. Product owns
        # authorization and the private/group body policy.
        card = {'config': {'update_multi': True},
                'elements': [{'tag': 'markdown', 'content': text}]}
        content = json.dumps(card, ensure_ascii=False)
        endpoint = 'https://open.feishu.cn/open-apis/im/v1/messages'
        if receipt:
            result = await self.request('PATCH', endpoint + '/' + quote(receipt, safe=''),
                headers=headers, json={'content': content})
        else:
            result = await self.request('POST', endpoint, params={'receive_id_type': 'chat_id'},
                headers=headers, json={'receive_id': target['chat_id'], 'msg_type': 'interactive',
                                      'content': content, 'uuid': stream_id})
        self.check(result, 'code')
        message_id = receipt or (result.get('data') or {}).get('message_id')
        if not message_id:
            raise ChannelTransportError('platform_receipt_missing')
        return message_id
