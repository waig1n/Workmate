import asyncio
import json

import pytest

from app.channels.adapters import create_adapter
from app.channels.adapters.base import ChannelTransportError
from app.channels.adapters.parsing import normalize


async def test_wecom_stream_preserves_callback_and_replaces_same_bubble():
    adapter = create_adapter('wecom', {})
    target = normalize('wecom', {'headers': {'req_id': 'callback'}, 'body': {
        'msgid': 'm', 'from': {'userid': 'u'}, 'msgtype': 'text', 'text': {'content': 'hi'}}}).target()
    frames = []
    class Socket:
        async def send(self, value):
            frame = json.loads(value)
            frames.append(frame)
            adapter._pending[frame['headers']['req_id']].set_result({'errcode': 0})
        async def close(self):
            pass
    adapter._socket = Socket()
    assert adapter.supports_stream(target)
    assert not adapter.supports_stream({'reply_context': {}})
    receipt = await adapter.stream(target, 'first', stream_id='s')
    await adapter.stream(target, 'first second', stream_id='s', receipt=receipt, finish=True)
    assert frames[0]['cmd'] == 'aibot_respond_msg'
    assert [f['headers']['req_id'] for f in frames] == ['callback', 'callback']
    assert frames[1]['body']['stream'] == {'id': 's', 'content': 'first second', 'finish': True}
    assert adapter._pending == {}
    with pytest.raises(ChannelTransportError, match='stream_context_missing'):
        await adapter.stream({}, 'text', stream_id='s')
    await adapter.close()


async def test_feishu_stream_creates_one_card_then_updates_with_full_text():
    adapter = create_adapter('feishu', {'app_id': 'app', 'app_secret': 'secret'})
    calls = []
    async def request(method, url, **kwargs):
        if 'auth/v3' in url:
            return {'tenant_access_token': 'token'}
        calls.append((method, url, kwargs))
        return {'code': 0, 'data': {'message_id': 'om_reply'}}
    adapter.request = request
    assert adapter.supports_stream({})
    receipt = await adapter.stream({'chat_id': 'c'}, 'partial', stream_id='stable')
    assert receipt == 'om_reply'
    await adapter.stream({'chat_id': 'c'}, 'answer', stream_id='stable', receipt=receipt, finish=True)
    assert [call[0] for call in calls] == ['POST', 'PATCH']
    assert calls[0][2]['json']['uuid'] == 'stable'
    assert calls[1][1].endswith('/om_reply')
    card = json.loads(calls[1][2]['json']['content'])
    assert card['config']['update_multi'] is True
    assert card['elements'][0]['content'] == 'answer'
    await adapter.close()


@pytest.mark.parametrize('group', [False, True])
async def test_dingtalk_stream_uses_ai_template_full_replacement_and_finalization(group):
    adapter = create_adapter('dingtalk', {'client_id': 'app', 'client_secret': 'secret', 'card_template_id': 'template'})
    target = {'chat_type': 'group' if group else 'private', 'chat_id': 'c', 'sender_id': 'u'}
    calls = []
    async def request(method, url, **kwargs):
        if 'oauth2' in url:
            return {'accessToken': 'token'}
        calls.append((method, url, kwargs['json']))
        return {'success': True}
    adapter.request = request
    assert adapter.supports_stream(target)
    receipt = await adapter.stream(target, 'partial', stream_id='stable')
    await adapter.stream(target, 'answer', stream_id='stable', receipt=receipt, finish=True)
    create = calls[0][2]
    assert create['cardTemplateId'] == 'template'
    assert create['outTrackId'] == receipt == 'stable'
    assert create['openSpaceId'] == ('dtv1.card//IM_GROUP.c' if group else 'dtv1.card//IM_ROBOT.u')
    assert calls[-1][0] == 'PUT' and calls[-1][1].endswith('/card/streaming')
    assert calls[-1][2]['isFull'] is True and calls[-1][2]['isFinalize'] is True
    assert calls[-1][2]['content'] == 'answer'
    assert calls[-1][2]['key'] == 'content'
    await adapter.close()


async def test_dingtalk_without_template_does_not_claim_streaming():
    adapter = create_adapter('dingtalk', {})
    assert not adapter.supports_stream({})
    with pytest.raises(ChannelTransportError, match='stream_template_missing'):
        await adapter.stream({}, 'text', stream_id='s')
    await adapter.close()


async def test_stream_failure_does_not_leak_platform_message():
    adapter = create_adapter('feishu', {'app_id': 'app', 'app_secret': 'secret'})
    async def request(method, url, **kwargs):
        return {'tenant_access_token': 'token'} if 'auth/v3' in url else {'code': 999, 'msg': 'secret'}
    adapter.request = request
    with pytest.raises(ChannelTransportError, match='^platform_rejected_request$'):
        await adapter.stream({'chat_id': 'c'}, 'text', stream_id='s')
    await adapter.close()


async def test_wecom_stream_failure_cleans_pending_and_rejects_overlapping_updates():
    adapter = create_adapter('wecom', {})
    target = {'reply_context': {'req_id': 'r'}}
    with pytest.raises(ChannelTransportError, match='channel_disconnected'):
        await adapter.stream(target, 'text', stream_id='s')
    class Socket:
        async def send(self, value):
            adapter._pending['r'].set_result({'errcode': 400, 'errmsg': 'secret'})
        async def close(self):
            pass
    adapter._socket = Socket()
    adapter._pending['r'] = asyncio.get_running_loop().create_future()
    with pytest.raises(ChannelTransportError, match='stream_update_in_progress'):
        await adapter.stream(target, 'text', stream_id='s')
    adapter._pending.clear()
    with pytest.raises(ChannelTransportError, match='^platform_rejected_request$'):
        await adapter.stream(target, 'text', stream_id='s')
    assert adapter._pending == {}
    await adapter.close()


async def test_feishu_missing_receipt_never_claims_create_succeeded():
    adapter = create_adapter('feishu', {'app_id': 'app', 'app_secret': 'secret'})
    async def request(method, url, **kwargs):
        return {'tenant_access_token': 'token'} if 'auth/v3' in url else {'code': 0}
    adapter.request = request
    with pytest.raises(ChannelTransportError, match='platform_receipt_missing'):
        await adapter.stream({'chat_id': 'c'}, 'text', stream_id='s')
    await adapter.close()


@pytest.mark.parametrize('failure_step', ['auth', 'create', 'update'])
async def test_dingtalk_stream_rejects_failed_operations(failure_step):
    adapter = create_adapter('dingtalk', {'client_id': 'app', 'client_secret': 'secret', 'card_template_id': 'template'})
    async def request(method, url, **kwargs):
        if 'oauth2' in url:
            return {} if failure_step == 'auth' else {'accessToken': 'token'}
        if 'createAndDeliver' in url:
            return {'success': failure_step != 'create', 'message': 'secret'}
        return {'success': False, 'message': 'secret'}
    adapter.request = request
    with pytest.raises(ChannelTransportError) as error:
        await adapter.stream({'chat_type': 'private', 'sender_id': 'u'}, 'text', stream_id='s')
    assert 'secret' not in str(error.value)
    await adapter.close()


async def test_wecom_replayed_callback_does_not_complete_pending_stream_receipt(monkeypatch):
    import websockets
    from aibot.types import WsCmd

    callback = {'cmd': WsCmd.CALLBACK, 'headers': {'req_id': 'same'},
                'body': {'msgid': 'm', 'from': {'userid': 'u'},
                         'msgtype': 'text', 'text': {'content': 'hello'}}}
    adapter = create_adapter('wecom', {'bot_id': 'bot', 'secret': 'secret'})
    pending = asyncio.get_running_loop().create_future()
    seen = []
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def send(self, value):
            pass
        async def recv(self):
            return json.dumps({'errcode': 0})
        def __aiter__(self):
            return self.frames()
        async def frames(self):
            adapter._pending['same'] = pending
            yield json.dumps(callback)
            assert seen == ['m']
            assert not pending.done()
            yield json.dumps({'headers': {'req_id': 'same'}, 'errcode': 0})
        async def close(self):
            pass
    monkeypatch.setattr(websockets, 'connect', lambda *args, **kwargs: Socket())
    async def emit(message):
        seen.append(message.message_id)
    await adapter.run(emit)
    assert pending.result() == {'headers': {'req_id': 'same'}, 'errcode': 0}
    await adapter.close()
