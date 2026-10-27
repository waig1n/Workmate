import asyncio

import httpx
import pytest

from app.channels.adapters import create_adapter
from app.channels.adapters.parsing import normalize
from app.channels.adapters.wechat import WechatAdapter, validate_base_url


def test_feishu_group_strips_only_bot_mention():
    message = normalize('feishu', {'event': {'sender': {'sender_id': {'open_id': 'u'}}, 'message': {'message_id': 'm', 'chat_id': 'g', 'chat_type': 'group', 'message_type': 'text', 'content': '{"text":"@_user_1 /new"}', 'mentions': [{'key': '@_user_1', 'id': {'open_id': 'bot'}}]}}}, bot_id='bot')
    assert message.mentioned and message.text == '/new'
    assert message.target()['sender_id'] == 'u'


def test_group_other_mentions_do_not_trigger():
    message = normalize('feishu', {'event': {'sender': {'sender_id': {'open_id': 'u'}}, 'message': {'message_id': 'm', 'chat_id': 'g', 'chat_type': 'group', 'message_type': 'image', 'content': '{}', 'mentions': [{'key': '@x', 'id': {'open_id': 'other'}}]}}}, bot_id='bot')
    assert not message.mentioned
    assert message.unsupported


def test_wecom_private_and_unsupported():
    message = normalize('wecom', {'headers': {'req_id': 'r'}, 'body': {'msgid': 'm', 'from': {'userid': 'u'}, 'chattype': 'single', 'msgtype': 'image'}})
    assert message.chat_id == 'u'
    assert message.unsupported


def test_dingtalk_requires_staff_identity():
    with pytest.raises(ValueError):
        normalize('dingtalk', {'msgId': 'm', 'senderId': 'anonymous', 'conversationId': 'g'})


@pytest.mark.parametrize('url', ['http://ilinkai.weixin.qq.com', 'https://evil.test', 'https://ilinkai.weixin.qq.com/evil', 'https://ilinkai.weixin.qq.com:444'])
def test_wechat_rejects_untrusted_base(url):
    with pytest.raises(ValueError):
        validate_base_url(url)


def test_credentials_hidden_and_provider_validated():
    adapter = create_adapter('wechat', {'bot_token': 'secret'})
    assert 'secret' not in repr(adapter)
    with pytest.raises(ValueError):
        create_adapter('unknown', {})


async def test_wechat_cursor_only_after_entire_batch_durable():
    calls = []
    adapter = WechatAdapter({'bot_token': 'secret'}, cursor='old', persist_cursor=lambda cursor: record_cursor(calls, cursor))
    async def request(*args, **kwargs):
        return {'msgs': [{'message_id': '1', 'from_user_id': 'u', 'message_type': 1, 'item_list': [{'type': 1, 'text_item': {'text': 'hello'}}]}], 'get_updates_buf': 'new'}
    adapter.request = request
    async def emit(message):
        calls.append(message.message_id)
        raise RuntimeError('database unavailable')
    with pytest.raises(RuntimeError):
        await adapter.poll_once(emit)
    assert adapter.cursor == 'old'
    assert calls == ['1']
    async def accepted(message):
        calls.append('durable')
    await adapter.poll_once(accepted)
    assert calls[-2:] == ['durable', 'new']
    assert adapter.cursor == 'new'
    await adapter.close()


async def record_cursor(calls, cursor):
    calls.append(cursor)


async def test_http_bounds_redirects_and_errors():
    from app.channels.adapters.base import Adapter, ChannelTransportError
    adapter = Adapter({})
    await adapter._http.aclose()
    responses = [httpx.Response(302, headers={'Location': 'https://evil.test'}),
                 httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1)),
                 httpx.Response(200, json=['not an object']),
                 httpx.Response(200, json={'ok': True})]
    adapter._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: responses.pop(0)))
    for _ in range(3):
        with pytest.raises(ChannelTransportError):
            await adapter.request('GET', 'https://example.test')
    assert await adapter.request('GET', 'https://example.test') == {'ok': True}
    await adapter.close()


@pytest.mark.parametrize('endpoint', ['ws://api.feishu.cn', 'wss://feishu.cn.evil.test', 'wss://user@feishu.cn', 'wss://feishu.cn:444'])
def test_websocket_discovery_rejects_untrusted_endpoint(endpoint):
    from app.channels.adapters.base import websocket_url, ChannelTransportError
    with pytest.raises(ChannelTransportError):
        websocket_url(endpoint, 'feishu.cn')


async def test_wechat_send_requires_context_and_checks_provider_error():
    from app.channels.adapters.base import ChannelTransportError
    adapter = WechatAdapter({'bot_token': 'secret'})
    with pytest.raises(ChannelTransportError, match='context_missing'):
        await adapter.send({'sender_id': 'u'}, 'hello')
    sent = []
    async def request(method, url, **kwargs):
        sent.append(kwargs)
        return {'ret': 0}
    adapter.request = request
    assert await adapter.send({'sender_id': 'u', 'reply_context': {'context_token': 'ctx'}}, 'hello')
    assert sent[0]['json']['msg']['context_token'] == 'ctx'
    assert sent[0]['json']['msg']['to_user_id'] == 'u'
    async def failed(*args, **kwargs):
        return {'ret': -1, 'errmsg': 'secret should not escape'}
    adapter.request = failed
    with pytest.raises(ChannelTransportError) as error:
        await adapter.send({'sender_id': 'u', 'reply_context': {'context_token': 'ctx'}}, 'hello')
    assert 'secret' not in str(error.value)
    await adapter.close()


async def test_qr_confirmed_base_is_validated():
    adapter = WechatAdapter({})
    async def request(*args, **kwargs):
        return {'status': 'confirmed', 'baseurl': 'https://evil.test', 'bot_token': 'secret'}
    adapter.request = request
    with pytest.raises(ValueError):
        await adapter.get_qrcode_status('code')
    await adapter.close()


async def test_dingtalk_send_uses_staff_id_and_never_message_webhook():
    adapter = create_adapter('dingtalk', {'client_id': 'app', 'client_secret': 'secret'})
    calls = []
    async def request(method, url, **kwargs):
        calls.append((url, kwargs))
        return {'accessToken': 'token'} if len(calls) == 1 else {'processQueryKey': 'receipt'}
    adapter.request = request
    result = await adapter.send({'chat_type': 'private', 'chat_id': 'chat', 'sender_id': 'staff',
        'reply_context': {'sessionWebhook': 'https://evil.test'}}, 'hello')
    assert result == 'receipt'
    assert calls[1][0] == 'https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend'
    assert calls[1][1]['json']['userIds'] == ['staff']
    await adapter.close()


async def test_feishu_send_checks_error_code():
    from app.channels.adapters.base import ChannelTransportError
    adapter = create_adapter('feishu', {'app_id': 'app', 'app_secret': 'secret'})
    async def request(method, url, **kwargs):
        return {'tenant_access_token': 'token'} if 'auth/v3' in url else {'code': 999, 'msg': 'secret'}
    adapter.request = request
    with pytest.raises(ChannelTransportError):
        await adapter.send({'chat_id': 'chat'}, 'hello')
    await adapter.close()


async def test_listen_shutdown_cancels_and_joins_transport():
    from app.channels.adapters.base import Adapter
    adapter = Adapter({})
    stopped = asyncio.Event()
    async def run(emit):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    adapter.run = run
    shutdown = asyncio.Event()
    task = asyncio.create_task(adapter.listen(None, shutdown))
    await asyncio.sleep(0)
    shutdown.set()
    await task
    assert stopped.is_set()
    await adapter.close()


class Socket:
    def __init__(self, inbound=()):
        self.inbound = list(inbound)
        self.sent = []
        self.closed = False
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        self.closed = True
    def __aiter__(self):
        return self
    async def __anext__(self):
        if not self.inbound:
            raise StopAsyncIteration
        return self.inbound.pop(0)
    async def send(self, value):
        self.sent.append(value)
    async def recv(self):
        return self.inbound.pop(0)
    async def close(self):
        self.closed = True


async def test_feishu_frame_ack_after_durable_ingest_and_fragment_assembly():
    import json
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    adapter = create_adapter('feishu', {})
    adapter._bot_id = 'bot'
    socket = Socket()
    event = {'header': {'event_type': 'im.message.receive_v1'}, 'event': {'sender': {'sender_id': {'open_id': 'u'}},
        'message': {'message_id': 'm', 'chat_id': 'c', 'chat_type': 'p2p', 'message_type': 'text', 'content': '{"text":"hi"}'}}}
    payload = json.dumps(event).encode()
    events = []
    async def emit(message):
        assert not socket.sent
        events.append(message)
    for seq, chunk in enumerate((payload[:15], payload[15:])):
        frame = Frame(SeqID=1, LogID=2, service=3, method=1, payload=chunk)
        for key, value in {'type': 'event', 'sum': '2', 'seq': str(seq), 'message_id': 'm'}.items():
            frame.headers.add(key=key, value=value)
        await adapter.handle_frame(frame, socket, emit)
    assert len(events) == len(socket.sent) == 1
    ack = Frame()
    ack.ParseFromString(socket.sent[0])
    assert json.loads(ack.payload)['code'] == 200
    async def fail(message):
        raise RuntimeError('durability failure')
    frame = Frame(SeqID=1, LogID=2, service=3, method=1, payload=payload)
    frame.headers.add(key='type', value='event')
    with pytest.raises(RuntimeError):
        await adapter.handle_frame(frame, socket, fail)
    assert len(socket.sent) == 1
    await adapter.close()


async def test_dingtalk_wire_ack_after_durable_ingest(monkeypatch):
    import json
    import websockets
    from dingtalk_stream import ChatbotMessage
    socket = Socket([json.dumps({'type': 'CALLBACK', 'headers': {'topic': ChatbotMessage.TOPIC, 'messageId': 'wire'},
        'data': json.dumps({'msgId': 'm', 'senderStaffId': 'u', 'conversationId': 'c', 'conversationType': '1',
                           'msgtype': 'text', 'text': {'content': 'hi'}})})])
    monkeypatch.setattr(websockets, 'connect', lambda *args, **kwargs: socket)
    adapter = create_adapter('dingtalk', {'client_id': 'app', 'client_secret': 'secret'})
    async def request(*args, **kwargs):
        return {'endpoint': 'wss://wss-open-connection.dingtalk.com/connect', 'ticket': 'secret'}
    adapter.request = request
    async def emit(message):
        assert not socket.sent
        assert adapter.ready.is_set()
        assert message.sender_id == 'u'
    await adapter.run(emit)
    assert json.loads(socket.sent[0])['headers']['messageId'] == 'wire'
    assert socket.closed
    await adapter.close()


async def test_wecom_official_wire_commands_and_durable_callback(monkeypatch):
    import json
    import websockets
    from aibot.types import WsCmd
    socket = Socket([json.dumps({'errcode': 0}), json.dumps({'cmd': WsCmd.CALLBACK,
        'body': {'msgid': 'm', 'from': {'userid': 'u'}, 'msgtype': 'text', 'text': {'content': 'hi'}}})])
    def connect(*args, **kwargs):
        assert kwargs['ping_interval'] is None
        assert kwargs['ping_timeout'] is None
        return socket
    monkeypatch.setattr(websockets, 'connect', connect)
    adapter = create_adapter('wecom', {'bot_id': 'bot', 'secret': 'secret'})
    messages = []
    async def emit(message):
        assert adapter.ready.is_set()
        messages.append(message)
    await adapter.run(emit)
    assert messages[0].text == 'hi'
    assert json.loads(socket.sent[0])['cmd'] == WsCmd.SUBSCRIBE
    assert socket.closed
    await adapter.close()


async def test_wecom_send_receipt_and_pending_cleanup():
    import json
    adapter = create_adapter('wecom', {})
    class ReceiptSocket(Socket):
        async def send(self, value):
            frame = json.loads(value)
            adapter._pending[frame['headers']['req_id']].set_result({'errcode': 0, 'body': {'msgid': 'receipt'}})
    adapter._socket = ReceiptSocket()
    assert await adapter.send({'chat_id': 'c'}, 'hello') == 'receipt'
    assert adapter._pending == {}
    await adapter.close()


async def test_feishu_wire_startup_bot_identity_and_heartbeat(monkeypatch):
    import json
    import websockets
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    event = {'header': {'event_type': 'im.message.receive_v1'}, 'event': {'sender': {'sender_id': {'open_id': 'u'}},
        'message': {'message_id': 'm', 'chat_id': 'c', 'chat_type': 'group', 'message_type': 'text',
                    'content': '{"text":"@bot hello"}', 'mentions': [{'key': '@bot', 'id': {'open_id': 'bot'}}]}}}
    frame = Frame(SeqID=1, LogID=2, service=3, method=1, payload=json.dumps(event).encode())
    frame.headers.add(key='type', value='event')
    class EndingSocket(Socket):
        async def recv(self):
            if not self.inbound:
                raise asyncio.CancelledError
            return self.inbound.pop(0)
    socket = EndingSocket([frame.SerializeToString()])
    monkeypatch.setattr(websockets, 'connect', lambda *args, **kwargs: socket)
    adapter = create_adapter('feishu', {'app_id': 'app', 'app_secret': 'secret'})
    async def request(method, url, **kwargs):
        if 'auth/v3' in url:
            return {'tenant_access_token': 'token'}
        if 'bot/v3' in url:
            return {'bot': {'open_id': 'bot'}}
        return {'data': {'URL': 'wss://msg.feishu.cn/ws?service_id=3', 'ClientConfig': {'PingInterval': 10}}}
    adapter.request = request
    messages = []
    async def emit(message):
        assert adapter.ready.is_set()
        messages.append(message)
    with pytest.raises(asyncio.CancelledError):
        await adapter.run(emit)
    assert messages[0].text == 'hello' and messages[0].mentioned
    ping = Frame()
    ping.ParseFromString(socket.sent[0])
    assert ping.method == 0 and ping.headers[0].value == 'ping'
    assert socket.closed
    await adapter.close()


def test_wecom_group_command_removes_platform_bot_prefix():
    message = normalize('wecom', {'body': {'msgid': 'm', 'from': {'userid': 'u'}, 'chatid': 'g',
        'chattype': 'group', 'msgtype': 'text', 'text': {'content': '@Workmate /status'}}})
    assert message.text == '/status'
    assert message.mentioned


async def test_ready_state_cleared_by_close():
    adapter = create_adapter('wecom', {})
    assert not adapter.ready.is_set()
    adapter.ready.set()
    await adapter.close()
    assert not adapter.ready.is_set()


async def test_transport_exception_never_discloses_endpoint_credentials():
    from app.channels.adapters.base import Adapter, ChannelTransportError
    adapter = Adapter({})
    async def run(emit):
        raise ValueError('wss://platform.test/?ticket=private-secret')
    adapter.run = run
    with pytest.raises(ChannelTransportError) as error:
        await adapter.listen(None, asyncio.Event())
    assert str(error.value) == 'platform_transport_failed'
    await adapter.close()


def test_wechat_group_metadata_is_never_normalized_as_private():
    message = normalize('wechat', {'message_id': 42, 'from_user_id': 'u', 'group_id': 'group',
        'item_list': [{'type': 1, 'text_item': {'text': 'hello'}}]})
    assert message.chat_type == 'group'
    assert message.chat_id == 'group'


async def test_wechat_expired_session_has_safe_distinct_error():
    from app.channels.adapters.base import ChannelTransportError
    adapter = WechatAdapter({'bot_token': 'secret'})
    async def request(*args, **kwargs):
        return {'ret': -14, 'errmsg': 'untrusted secret'}
    adapter.request = request
    with pytest.raises(ChannelTransportError, match='^wechat_session_expired$'):
        await adapter.api('getupdates', {})
    await adapter.close()


async def test_wechat_qr_uses_official_post_and_application_headers_without_bearer():
    adapter = WechatAdapter({'bot_token': 'existing-secret'})
    seen = []
    async def request(method, url, **kwargs):
        seen.append((method, url, kwargs))
        if 'get_bot_qrcode' in url:
            return {'qrcode': 'qr', 'qrcode_img_content': 'https://liteapp.weixin.qq.com/qr'}
        return {'status': 'wait'}
    adapter.request = request
    await adapter.get_qrcode()
    await adapter.get_qrcode_status('qr')
    assert seen[0][0] == 'POST'
    assert seen[0][2]['json'] == {'local_token_list': []}
    for _, _, kwargs in seen:
        assert kwargs['headers']['iLink-App-Id'] == 'bot'
        assert 'Authorization' not in kwargs['headers']
    await adapter.close()


async def test_feishu_bot_messages_ack_without_entering_employee_input():
    import json
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    from unittest.mock import AsyncMock
    adapter=create_adapter('feishu',{'app_id':'a','app_secret':'s'})
    adapter._bot_id='self'
    socket=Socket()
    emit=AsyncMock()
    for sender_id,sender_type in [('self','user'),('other-bot','app')]:
        payload={'header':{'event_type':'im.message.receive_v1'},
            'event':{'sender':{'sender_type':sender_type,'sender_id':{'open_id':sender_id}}}}
        frame=Frame(SeqID=1,LogID=2,service=3,method=1,payload=json.dumps(payload).encode())
        frame.headers.add(key='type',value='event')
        await adapter.handle_frame(frame,socket,emit)
    emit.assert_not_awaited()
    assert len(socket.sent)==2
    await adapter.close()
