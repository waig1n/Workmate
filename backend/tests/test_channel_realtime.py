import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from workmate_contracts.realtime import message_id

from app.channels.realtime import ChannelTextFeed, ChannelTextGap, ChannelTextReducer


def event(kind, *, task='t', delta=None, version=1, offset=0, block='b', source='worker'):
    value = dict(event_id='e', conversation_id='c', task_id=task, reply_id='r',
                 source=source, payload={'type': kind, 'messageId': block})
    value.update(execution_epoch=1) if source == 'worker' else value.update(business_seq=1)
    if delta is not None:
        value['payload']['delta'] = delta
        value['text'] = dict(message_id=message_id(task, 'r'), block_id=block,
                             content_version=version, from_offset=offset,
                             to_offset=offset + len(delta.encode()))
    return value


def snapshot(text='过程答案', process_end=2):
    return dict(message_id=message_id('t', 'r'), conversation_id='c', task_id='t', reply_id='r',
                execution_epoch=1, snapshot_version=1, business_seq=0, phase='final',
                incomplete=False, process_end=process_end,
                blocks=[dict(block_id='b', kind='text', text=text, content_version=1, closed=True)])


def test_visible_text_excludes_other_tasks_reasoning_and_process_prefix():
    reducer = ChannelTextReducer('c', 't')
    reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_START', task='other'))
    reducer.accept('REALTIME_EVENT', event('REASONING_MESSAGE_CONTENT', delta='secret', block='think'))
    reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_START'))
    assert reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_CONTENT', delta='过程')) == '过程'
    assert reducer.accept('REALTIME_EVENT', event('TOOL_CALL_START', source='business')) == ''
    assert reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_CONTENT', delta='答案', version=2, offset=6)) == '答案'
    assert reducer.accept('REALTIME_SNAPSHOT', snapshot()) == '答案'
    assert reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_CONTENT', delta='late', version=3, offset=12)) == '答案'


def test_gap_and_memory_limit_fail_closed():
    reducer = ChannelTextReducer('c', 't', max_bytes=8)
    reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_START'))
    with pytest.raises(ChannelTextGap):
        reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_CONTENT', delta='x', version=2))
    with pytest.raises(ChannelTextGap):
        reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_CONTENT', delta='too much text'))


def test_reset_replaces_state_and_incomplete_reset_withholds_body():
    reducer = ChannelTextReducer('c', 't')
    reducer.accept('REALTIME_SNAPSHOT', snapshot())
    reset = dict(conversation_id='c', generation='00000000-0000-0000-0000-000000000001',
                 reason='initial', snapshots=[], business_state={}, business_seq=0, sync_status='synced')
    assert reducer.accept('REALTIME_RESET', reset) == ''
    reset.update(snapshots=[snapshot()], sync_status='awaiting_snapshot')
    assert reducer.accept('REALTIME_RESET', reset) == ''


@pytest.mark.asyncio
async def test_feed_uses_authorized_handle_and_closes_on_cancellation():
    started = asyncio.Event()
    class Handle:
        aclose = AsyncMock()
        async def frames(self):
            yield 'data: ' + json.dumps(dict(type='CUSTOM', name='REALTIME_SNAPSHOT', value=snapshot())) + '\n\n'
            started.set()
            await asyncio.Event().wait()
    handle = Handle()
    stream = type('Stream', (), {'open': AsyncMock(return_value=handle), 'policy': None})()
    callback = AsyncMock()
    task = asyncio.create_task(ChannelTextFeed(stream, 'sessions').watch('c', 't', 'u', callback))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    callback.assert_awaited_once_with('答案')
    authority = stream.open.call_args.kwargs['authority']
    assert authority.actor_id == 'u' and authority.token_version is None
    handle.aclose.assert_awaited_once()


def test_duplicate_utf8_delta_and_end_do_not_duplicate_or_append():
    reducer = ChannelTextReducer('c', 't')
    reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_START'))
    delta = event('TEXT_MESSAGE_CONTENT', delta='你好')
    assert reducer.accept('REALTIME_EVENT', delta) == '你好'
    assert reducer.accept('REALTIME_EVENT', delta) == '你好'
    reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_END'))
    assert reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_CONTENT', delta='!', version=2, offset=6)) == '你好'


@pytest.mark.parametrize('name,value', [
    ('REALTIME_EVENT', {**event('TEXT_MESSAGE_START'), 'conversation_id': 'wrong'}),
    ('REALTIME_SNAPSHOT', {**snapshot(), 'conversation_id': 'wrong'}),
    ('REALTIME_SNAPSHOT', {**snapshot(), 'phase': 'partial', 'incomplete': True}),
    ('REALTIME_EVENT', event('TEXT_MESSAGE_CONTENT', delta='missing start')),
])
def test_untrusted_or_missing_state_requires_reset(name, value):
    with pytest.raises(ChannelTextGap):
        ChannelTextReducer('c', 't').accept(name, value)


def test_terminal_snapshot_blocks_new_late_block_and_old_snapshot():
    reducer = ChannelTextReducer('c', 't')
    reducer.accept('REALTIME_SNAPSHOT', {**snapshot(), 'snapshot_version': 2})
    reducer.accept('REALTIME_SNAPSHOT', snapshot('old', 0))
    reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_START', block='late'))
    assert reducer.accept('REALTIME_EVENT', event('TEXT_MESSAGE_CONTENT', block='late', delta='late')) == '答案'


@pytest.mark.asyncio
async def test_feed_closes_on_gap_and_deduplicates_callbacks():
    class Handle:
        aclose = AsyncMock()
        async def frames(self):
            yield ': heartbeat\n\n'
            for kind, value in [('REALTIME_SNAPSHOT', snapshot()), ('REALTIME_SNAPSHOT', snapshot()),
                                ('REALTIME_EVENT', event('TEXT_MESSAGE_CONTENT', block='missing', delta='x', task='t2')),
                                ('REALTIME_EVENT', {**event('TEXT_MESSAGE_START'), 'conversation_id': 'wrong'})]:
                yield 'data: ' + json.dumps(dict(type='CUSTOM', name=kind, value=value)) + '\n\n'
    handle = Handle()
    stream = type('Stream', (), {'open': AsyncMock(return_value=handle), 'policy': None})()
    callback = AsyncMock()
    with pytest.raises(ChannelTextGap):
        await ChannelTextFeed(stream, None).watch('c', 't', 'u', callback)
    callback.assert_awaited_once_with('答案')
    handle.aclose.assert_awaited_once()
