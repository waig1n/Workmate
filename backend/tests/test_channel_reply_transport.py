from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.channels.reply_transport import deliver_reply, plan_reply


NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def row(*, wecom=True, elapsed=0, finished=False, revision=1, receipt=None, state=None):
    received = NOW - timedelta(seconds=elapsed)
    target = {'chat_type': 'private', 'received_at': received.isoformat(),
              'reply_context': {'req_id': 'callback'} if wecom else {}}
    return SimpleNamespace(target=target, text='正在处理…', platform_message_id=receipt,
                           reply_state=state or {}, reply_finished=finished, reply_revision=revision)


def adapter(supported=True):
    return SimpleNamespace(supports_stream=lambda target: supported,
                           stream=AsyncMock(return_value='stream-receipt'),
                           send=AsyncMock(return_value='plain-receipt'))


async def test_early_wecom_bootstrap_has_no_processing_words_and_final_reuses_receipt():
    platform = adapter()
    item = row(state={'bootstrap': True, 'hint_at': (NOW + timedelta(seconds=30)).isoformat()})
    target, text, receipt, state, bootstrap, defer = plan_reply(item, platform, NOW)
    assert text == '' and bootstrap and defer is None
    receipt, state = await deliver_reply(platform, target, text, receipt, state, finished=False, sequence=1)
    assert platform.stream.await_args.args[1] == ''
    item.reply_state = state
    item.platform_message_id = receipt
    item.text = 'Final answer'
    item.reply_finished = True
    item.reply_revision = 2
    target, text, next_receipt, state, bootstrap, defer = plan_reply(item, platform, NOW + timedelta(seconds=20))
    assert next_receipt == receipt and text == 'Final answer' and not bootstrap
    _, state = await deliver_reply(platform, target, text, next_receipt, state, finished=True, sequence=2)
    assert platform.stream.await_args.kwargs['receipt'] == receipt
    assert platform.stream.await_args.kwargs['finish'] is True
    assert state['closed'] is True
    platform.send.assert_not_awaited()


def test_late_wecom_callback_defers_plain_hint_until_30_seconds():
    platform = adapter()
    item = row(elapsed=4, state={'bootstrap': True, 'hint_at': (NOW + timedelta(seconds=26)).isoformat()})
    _, text, _, state, bootstrap, defer = plan_reply(item, platform, NOW)
    assert state['mode'] == 'plain' and not bootstrap
    assert defer == NOW + timedelta(seconds=26)
    assert text == '正在处理…'
    # A defer is not a delivery; retrying the original persisted row is safe.
    _, _, _, state, bootstrap, defer = plan_reply(item, platform, NOW + timedelta(seconds=26))
    assert state['mode'] == 'plain' and defer is None and not bootstrap


def test_wecom_stream_expiry_stops_partials_and_allows_final_delivery():
    platform = adapter()
    item = row(elapsed=300, receipt='old', revision=4,
               state={'started_at': (NOW - timedelta(seconds=300)).isoformat(),
                      'stream_id': 's', 'body_seen': True, 'mode': 'stream'})
    _, _, _, state, _, defer = plan_reply(item, platform, NOW)
    assert state['mode'] == 'plain' and defer is not None
    item.reply_finished = True
    _, _, _, state, _, defer = plan_reply(item, platform, NOW)
    assert state['mode'] == 'plain' and defer is None


def test_wecom_stream_remains_open_before_300_seconds():
    item = row(elapsed=299, receipt='existing', state={
        'started_at': (NOW - timedelta(seconds=299)).isoformat(), 'stream_id': 's'})
    assert plan_reply(item, adapter(), NOW)[3]['mode'] == 'stream'


def test_reopened_closed_feishu_segment_gets_new_stream_and_no_old_receipt():
    item = row(wecom=False, receipt='finished-card', revision=5, state={
        'closed': True, 'body_seen': True, 'stream_id': 'old', 'mode': 'stream',
        'started_at': (NOW - timedelta(seconds=60)).isoformat()})
    _, _, receipt, state, _, defer = plan_reply(item, adapter(), NOW)
    assert receipt is None and state['stream_id'] != 'old'
    assert state['body_seen'] is True and 'closed' not in state
    assert state['mode'] == 'stream' and defer is None


async def test_dingtalk_without_template_suppresses_partial_spam_but_sends_final():
    platform = adapter(False)
    item = row(wecom=False, revision=2, state={'body_seen': True})
    target, text, receipt, state, _, defer = plan_reply(item, platform, NOW)
    assert state['mode'] == 'plain' and defer is not None
    platform.send.assert_not_awaited()
    item.reply_finished = True
    item.text = 'Final answer'
    target, text, receipt, state, _, defer = plan_reply(item, platform, NOW)
    assert defer is None
    await deliver_reply(platform, target, text, receipt, state, finished=True, sequence=3)
    platform.send.assert_awaited_once_with(target, 'Final answer')
    platform.stream.assert_not_awaited()


def test_group_never_streams_private_body():
    item = row(wecom=False, revision=2)
    item.target['chat_type'] = 'group'
    assert plan_reply(item, adapter(), NOW)[3]['mode'] == 'plain'


def test_generated_but_undelivered_body_keeps_hint_deadline_after_downgrade():
    due = NOW + timedelta(seconds=25)
    item = row(elapsed=5, revision=2, state={'bootstrap':True, 'body_seen':True, 'hint_at':due.isoformat()})
    item.text = 'Undelivered body'
    _, _, _, state, _, defer = plan_reply(item, adapter(), NOW)
    assert defer == due
    item.reply_state = state
    _, text, _, state, _, defer = plan_reply(item, adapter(), due)
    assert defer is None and text == '正在处理…'
