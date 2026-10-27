"""Typing follows authorized active Tasks, never raw platform input."""
from unittest.mock import AsyncMock
import pytest
from app.channels.supervisor import ChannelSupervisor
from test_channel_supervisor import prepare, Transport
from test_channel_processing import channels
from test_native_admission import admission_database
from test_durable_projection import projection_database

class TypingTransport(Transport):
    def __init__(self):
        super().__init__()
        self.sync_typing = AsyncMock()
        self.stop_typing = AsyncMock()
    def supports_typing(self): return True

async def test_typing_includes_plain_pending_task_without_text_feed(channels):
    supervisor,epoch,version=await prepare(channels)
    _,_,admin=channels
    await admin.execute("UPDATE channel_outbox SET reply_state='{\"mode\":\"plain\"}'")
    adapter=TypingTransport()
    await supervisor._sync_typing('ch',epoch,version,adapter)
    targets=adapter.sync_typing.call_args.args[0]
    assert len(targets)==1 and targets[0]['chat_type']=='private'

@pytest.mark.parametrize('change', ['lease','identity','terminal','approval','disabled','revoked'])
async def test_typing_clears_when_authority_or_task_ends(channels,change):
    supervisor,epoch,version=await prepare(channels)
    _,_,admin=channels
    adapter=TypingTransport()
    await supervisor._sync_typing('ch',epoch,version,adapter)
    assert adapter.sync_typing.call_args.args[0]
    if change=='lease':
        await admin.execute("UPDATE channel_connections SET lease_until=now()-interval '1 second'")
    elif change=='disabled':
        await admin.execute('UPDATE channel_connections SET enabled=false')
    elif change=='revoked':
        supervisor.service.admission._authorize=AsyncMock(return_value=False)
    elif change=='identity':
        await admin.execute('DELETE FROM channel_identities')
    else:
        await admin.execute("UPDATE tasks SET status=$1 WHERE origin='channel'",'completed' if change=='terminal' else 'awaiting_permission')
    await supervisor._sync_typing('ch',epoch,version,adapter)
    assert adapter.sync_typing.call_args.args[0]==[]

async def test_typing_unsupported_transport_is_untouched(channels):
    supervisor,epoch,version=await prepare(channels)
    await supervisor._sync_typing('ch',epoch,version,Transport())

async def test_final_stops_typing_before_send_and_failure_does_not_block(channels):
    supervisor,epoch,version=await prepare(channels)
    _,_,admin=channels
    await admin.execute('UPDATE channel_outbox SET reply_finished=true')
    adapter=TypingTransport()
    adapter.stop_typing.side_effect=RuntimeError('typing unavailable')
    await supervisor.send_one('ch',epoch,version,adapter)
    adapter.stop_typing.assert_awaited_once()
    assert len(adapter.sent)==1

async def test_typing_query_failure_clears_stale_targets(channels,monkeypatch):
    supervisor,epoch,version=await prepare(channels)
    adapter=TypingTransport()
    monkeypatch.setattr('app.channels.supervisor.repo.live_replies',AsyncMock(side_effect=RuntimeError()))
    await supervisor._sync_typing('ch',epoch,version,adapter)
    adapter.sync_typing.assert_awaited_once_with([])
