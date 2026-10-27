import asyncio
import pytest
from app.channels.adapters.wechat import WechatAdapter


def target(sender='user', context='ctx'):
    return {'chat_type': 'private', 'sender_id': sender, 'reply_context': {'context_token': context}}


async def flush():
    for _ in range(8):
        await asyncio.sleep(0)


async def test_typing_is_background_deduplicated_and_stopped_before_close():
    adapter = WechatAdapter({})
    calls = []
    async def api(path, body):
        calls.append((path, body))
        return {'typing_ticket': 'ticket'}
    adapter.api = api
    assert adapter.supports_typing()
    await adapter.sync_typing([target(), target()])
    assert calls == []
    await flush()
    assert calls == [('getconfig', {'ilink_user_id': 'user', 'context_token': 'ctx'}),
                     ('sendtyping', {'ilink_user_id': 'user', 'typing_ticket': 'ticket', 'status': 1})]
    await adapter.stop_typing(target())
    assert calls[-1][1]['status'] == 2
    await adapter.close()
    assert not adapter._typing_jobs


async def test_invalid_targets_and_capacity_are_bounded():
    adapter = WechatAdapter({})
    async def api(path, body):
        await asyncio.Event().wait()
    adapter.api = api
    await adapter.sync_typing([{}, {**target(), 'chat_type': 'group'}] + [target(str(i)) for i in range(100)])
    assert len(adapter._typing_jobs) <= 64
    await adapter.close()
    assert not adapter._typing_jobs


async def test_failure_backoff_does_not_retry_each_sync():
    adapter = WechatAdapter({})
    calls = []
    async def api(path, body):
        calls.append(path)
        raise RuntimeError('credential must not escape')
    adapter.api = api
    await adapter.sync_typing([target()])
    await flush()
    await adapter.sync_typing([target()])
    await flush()
    assert calls == ['getconfig']
    await adapter.close()


async def test_context_replacement_stops_old_before_starting_new():
    adapter = WechatAdapter({})
    calls = []
    async def api(path, body):
        calls.append((path, body))
        return {'typing_ticket': body.get('context_token', 'ticket')}
    adapter.api = api
    await adapter.sync_typing([target()])
    await flush()
    await adapter.sync_typing([target(context='new')])
    await flush()
    await adapter.sync_typing([target(context='new')])
    await flush()
    old_stop = next(i for i, (_, b) in enumerate(calls) if b.get('status') == 2)
    new_start = next(i for i, (_, b) in enumerate(calls) if b.get('context_token') == 'new')
    assert old_stop < new_start
    await adapter.close()


async def test_stop_timeout_is_bounded_and_plain_send_does_not_stop_typing():
    adapter = WechatAdapter({})
    calls = []
    async def api(path, body):
        calls.append((path, body))
        if body.get('status') == 2:
            await asyncio.Event().wait()
        return {'typing_ticket': 'ticket'}
    adapter.api = api
    await adapter.sync_typing([target()])
    await flush()
    await adapter.send(target(), 'hint')
    assert not any(body.get('status') == 2 for _, body in calls)
    async with asyncio.timeout(1):
        await adapter.stop_typing(target())
    assert not adapter._typing_jobs
    await adapter.close()


async def test_expired_authorization_never_sends_typing_after_config_returns():
    adapter = WechatAdapter({})
    gate = asyncio.Event()
    calls = []
    async def api(path, body):
        calls.append((path, body))
        if path == 'getconfig':
            await gate.wait()
        return {'typing_ticket': 'ticket'}
    adapter.api = api
    await adapter.sync_typing([target()])
    await flush()
    adapter._typing_desired['user'] = ('ctx', 0)
    gate.set()
    await flush()
    assert not any(body.get('status') == 1 for _, body in calls)
    await adapter.close()


async def test_old_context_final_stop_cannot_cancel_new_context_authorization():
    adapter = WechatAdapter({})
    async def api(path, body):
        return {'typing_ticket': 'ticket'}
    adapter.api = api
    await adapter.sync_typing([target(context='new')])
    await flush()
    await adapter.stop_typing(target(context='old'))
    assert adapter._typing_authorized('user', 'new')
    await adapter.close()
