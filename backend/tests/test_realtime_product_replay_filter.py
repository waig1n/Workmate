"""Final snapshot replay optimization without Redis or weakened live authority."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services.realtime_product_stream import RealtimeProductStream, _Handle
from workmate_product.realtime.reset_builder import ActiveDisplay


def snapshot(task='old', epoch=1, phase='final'):
    return NS(task_id=task, reply_id='reply', execution_epoch=epoch, phase=phase)


def entry(task='old', epoch=1, position='5-0', generation='g'):
    return NS(cursor=NS(generation=generation, stream_id=position), envelope=NS(
        conversation_id='conversation', source='worker', task_id=task,
        reply_id='reply', execution_epoch=epoch, payload={'type':'TEXT_MESSAGE_CONTENT'}))


async def installed(snapshots=None, active=None):
    authority = NS(active_display=AsyncMock(return_value=active), authorize=AsyncMock(), read_view=AsyncMock())
    stream = RealtimeProductStream(NS(), NS(), NS())
    handle = _Handle(stream, 'conversation', authority)
    reset = NS(snapshots=snapshots or [], business_seq=0, model_dump=lambda **kw: {})
    handle.builder = NS(build=AsyncMock(return_value=NS(subscription=NS(aclose=AsyncMock()),
        ref=NS(generation='g'), verified_through=NS(generation='g', stream_id='10-0'),
        active=active, content_ready=True, reset=reset, replay_from=None)))
    await handle._install_reset('initial')
    return handle


async def test_600_proven_final_events_require_no_active_display_queries():
    handle = await installed([snapshot()])
    for _ in range(600):
        assert await handle._entry_frames(entry()) == ()
    handle.authority.active_display.assert_not_awaited()


@pytest.mark.parametrize('kwargs', [{'position':'11-0'}, {'generation':'other'}, {'epoch':2}, {'task':'next'}])
async def test_outside_exact_verified_identity_and_cut_uses_authority(kwargs):
    handle = await installed([snapshot()])
    assert await handle._entry_frames(entry(**kwargs)) == ()
    handle.authority.active_display.assert_awaited_once()


@pytest.mark.parametrize('phase', ['partial', 'approval', 'interrupted'])
async def test_nonfinal_snapshot_does_not_suppress_authority(phase):
    handle = await installed([snapshot(phase=phase)])
    await handle._entry_frames(entry())
    handle.authority.active_display.assert_awaited_once()


async def test_current_identity_is_never_filtered_and_next_epoch_can_follow():
    handle = await installed([snapshot()], ActiveDisplay('old', 'reply', 1))
    handle._boundary = AsyncMock(return_value=(None, False))
    await handle._entry_frames(entry())
    handle._boundary.assert_awaited_once()
    handle.authority.active_display.return_value = ActiveDisplay('old', 'reply', 2)
    await handle._entry_frames(entry(epoch=2, position='11-0'))
    assert handle.active.execution_epoch == 2


async def test_reset_replaces_evidence_and_set_is_bounded():
    handle = await installed([snapshot(str(i)) for i in range(65)])
    for i in range(65):
        await handle._entry_frames(entry(task=str(i)))
    assert handle.authority.active_display.await_count == 1
    handle.builder.build.return_value.reset.snapshots = []
    await handle._reset('generation_changed')
    handle.authority.active_display.reset_mock()
    await handle._entry_frames(entry(task='0'))
    handle.authority.active_display.assert_awaited_once()


async def test_authorization_revocation_still_precedes_cached_replay():
    handle = await installed([snapshot()])
    handle.next_authorize = 0
    handle.authority.authorize.side_effect = PermissionError('revoked')
    with pytest.raises(PermissionError, match='revoked'):
        await anext(handle.frames())
    handle.authority.active_display.assert_not_awaited()


async def test_live_reply_can_become_current_even_when_final_identity_was_cached():
    handle = await installed([snapshot()])
    handle.authority.active_display.return_value = ActiveDisplay('old', 'reply', 1)
    handle._boundary = AsyncMock(return_value=(None, False))
    await handle._entry_frames(entry(position='11-0'))
    assert handle.active.task_id == 'old'
    handle.authority.active_display.assert_awaited_once()
    await handle._entry_frames(entry())
    assert handle._boundary.await_count == 2


async def test_failed_reset_and_close_clear_prior_evidence():
    handle = await installed([snapshot()])
    handle.builder.build.side_effect = RuntimeError('failed handshake')
    with pytest.raises(RuntimeError, match='failed handshake'):
        await handle._reset('generation_changed')
    assert not handle.final_replay_identities
    handle = await installed([snapshot()])
    await handle.aclose()
    assert not handle.final_replay_identities
