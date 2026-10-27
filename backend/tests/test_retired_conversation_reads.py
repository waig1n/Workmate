"""Retirement hides history and closes authorized Redis-backed browser streams."""
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.services.conversation_business_state import ConversationBusinessState, ConversationUnavailable
from app.services.durable_runtime import compose_product_services
from workmate_contracts.realtime import RealtimeCursor, RealtimeEnvelope, encode_cursor
from test_durable_projection import projection_database  # noqa: F401
from test_business_reset_composition import composition  # noqa: F401


def make_stream(composition):
    sessions, _, log, _, _ = composition
    return compose_product_services(sessions, Settings(_env_file=None), realtime_log=log)[2]


async def close(stream):
    await stream.close()
    await stream.reader.close()


async def test_retired_conversation_rejected_before_replay(composition, monkeypatch):
    sessions, admin, _, _, _ = composition
    stream = make_stream(composition)
    ensure = AsyncMock(wraps=stream.manager.ensure)
    monkeypatch.setattr(stream.manager, 'ensure', ensure)
    await admin.execute("UPDATE conversations SET deleted_at=clock_timestamp() WHERE id='c'")
    try:
        with pytest.raises(ConversationUnavailable):
            await stream.open('c', authority=ConversationBusinessState(sessions, 'u', token_version=0))
        ensure.assert_not_awaited()
        assert not stream._handles and not stream.reader._hubs
        assert sessions.kw['bind'].pool.checkedout() == 0
    finally:
        await close(stream)


async def test_retirement_closes_connected_stream_before_next_page(composition):
    sessions, admin, _, _, _ = composition
    stream = make_stream(composition)
    try:
        handle = await stream.open('c', authority=ConversationBusinessState(sessions, 'u', token_version=0))
        frames = handle.frames()
        assert 'REALTIME_RESET' in await anext(frames)
        assert 'REPLAY_DONE' in await anext(frames)
        await admin.execute("UPDATE conversations SET deleted_at=clock_timestamp() WHERE id='c'")
        handle.next_authorize = 0
        with pytest.raises(ConversationUnavailable):
            await anext(frames)
        assert handle.closed
        assert not stream._handles and not stream.reader._hubs
        assert sessions.kw['bind'].pool.checkedout() == 0
    finally:
        await close(stream)


async def test_pg_retirement_hides_persisted_replay_and_closes_existing_stream(composition):
    sessions, admin, log, _, _ = composition
    stream = make_stream(composition)
    try:
        ref = await stream.manager.ensure('c')
        for seq in (1, 2):
            await log.append(ref, RealtimeEnvelope(event_id=f'notice-{seq}', conversation_id='c',
                task_id='t', reply_id='reply', source='business', business_seq=seq,
                payload={'type':'CUSTOM','name':'NOTICE','value':{'index':seq}}))
        token = encode_cursor(RealtimeCursor(conversation_id='c', generation=ref.generation, stream_id='0-0'))
        authority = ConversationBusinessState(sessions, 'u', token_version=0)
        handle = await stream.open('c', authority=authority, last_event_id=token)
        frames = handle.frames()
        assert 'notice-1' in await anext(frames)
        await admin.execute("UPDATE conversations SET deleted_at=clock_timestamp() WHERE id='c'")
        handle.next_authorize = 0
        with pytest.raises(ConversationUnavailable):
            await anext(frames)
        with pytest.raises(ConversationUnavailable):
            await stream.open('c', authority=authority, last_event_id=token)
        assert not stream._handles and not stream.reader._hubs
        assert sessions.kw['bind'].pool.checkedout() == 0
    finally:
        await close(stream)


async def test_pg_retired_tasks_and_artifacts_hidden_and_usage_empty(projection_database):
    from datetime import datetime,timezone
    from fastapi import HTTPException
    from workmate_product.persistence.models import User
    from app.api.routers.tasks import list_tasks,_get_owned_task
    from app.api.routers.artifacts import list_artifacts
    from app.api.routers.conversations import _get_owned
    from app.services.usage import usage_base
    from sqlalchemy import select,func
    sessions,admin=projection_database
    await admin.execute("UPDATE conversations SET deleted_at=clock_timestamp() WHERE id='c'")
    async with sessions() as db:
        user=await db.get(User,'u')
        assert await list_tasks(user=user,db=db,limit=200,offset=0)==[]
        assert await list_artifacts(user=user,db=db,limit=200,offset=0)==[]
        with pytest.raises(HTTPException) as task_missing:
            await _get_owned_task('t',user,db)
        assert task_missing.value.status_code==404
        with pytest.raises(HTTPException) as conv_missing:
            await _get_owned('c',user,db)
        assert conv_missing.value.status_code==404
        report=await usage_base(db,'u',datetime(2000,1,1,tzinfo=timezone.utc),
                               datetime(2100,1,1,tzinfo=timezone.utc))
        assert await db.scalar(select(func.count()).select_from(report))==0
