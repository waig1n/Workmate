"""Plan one bounded platform update; durable state belongs to the reply slot."""
import uuid
from datetime import datetime, timedelta


def plan_reply(row, adapter, instant):
    state = dict(row.reply_state or {})
    target, text = dict(row.target), row.text
    receipt = row.platform_message_id
    bootstrap = False
    defer = None
    supported = (target.get('chat_type') == 'private'
                 and adapter.supports_stream(target) if hasattr(adapter, 'supports_stream') else False)
    if state.get('closed'):
        state = {key: value for key, value in state.items() if key in ('body_seen','manual_retries','hint_at')}
        receipt = None
    received = datetime.fromisoformat(target['received_at']) if target.get('received_at') else instant
    wecom = bool((target.get('reply_context') or {}).get('req_id'))
    if wecom and not receipt and (instant-received).total_seconds() >= 4:
        state['mode'] = 'plain'
    if receipt and wecom and state.get('started_at'):
        if (instant-datetime.fromisoformat(state['started_at'])).total_seconds() >= 300:
            state['mode'] = 'plain'
    supported = supported and state.get('mode') != 'plain'
    if state.pop('bootstrap', False):
        hint_at = datetime.fromisoformat(state['hint_at'])
        if row.reply_revision == 1 and not row.reply_finished:
            if supported:
                text, bootstrap = '', True
            elif instant < hint_at:
                defer = hint_at
    if not supported and state.get('body_seen') and not row.reply_finished:
        # No updates on plain transports. Live subscriptions only exist on
        # capable transports, but may have ended because of platform expiry.
        if receipt or state.get('hint_sent'):
            defer = instant + timedelta(days=1)
        else:
            hint_at = datetime.fromisoformat(state['hint_at']) if state.get('hint_at') else received + timedelta(seconds=30)
            text = '正在处理…'
            defer = hint_at if instant < hint_at else None
    state['mode'] = 'stream' if supported else 'plain'
    state.setdefault('stream_id', str(uuid.uuid4()))
    state.setdefault('started_at', instant.isoformat())
    return target, text, receipt, state, bootstrap, defer


async def deliver_reply(adapter, target, text, receipt, state, *, finished, sequence):
    if state['mode'] == 'stream':
        receipt = await adapter.stream(target, text, stream_id=state['stream_id'],
            receipt=receipt, finish=finished, sequence=sequence)
    else:
        receipt = await adapter.send(target, text)
        state = {**state, 'hint_sent': not finished}
    return receipt, {**state, 'closed': finished}
