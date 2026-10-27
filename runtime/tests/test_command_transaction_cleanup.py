"""Control-transaction cleanup remains bounded and joined under cancellation."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from workmate_runtime.execution import command_transaction as module


class Session:
    def __init__(self, *, block=None, fail=None):
        self.block = block
        self.fail = fail
        self.events = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.sql = []

    async def stage(self, name):
        self.events.append(name + '-start')
        if self.block == name:
            self.entered.set()
            await self.release.wait()
        if self.fail == name:
            raise OSError(name + ' failed')
        self.events.append(name + '-done')

    @asynccontextmanager
    async def session(self):
        try:
            yield self
        finally:
            await self.stage('close')

    @asynccontextmanager
    async def begin(self):
        try:
            yield
        except BaseException:
            await self.stage('rollback')
            raise
        else:
            await self.stage('commit')

    async def execute(self, statement):
        self.sql.append(str(statement))

    async def invalidate(self):
        await self.stage('invalidate')


async def test_success_commits_then_closes_without_invalidating():
    db = Session()
    async with module.command_transaction(db.session) as acquired:
        assert acquired is db
        assert db.events == []
    assert db.events == ['commit-start', 'commit-done', 'close-start', 'close-done']
    assert len(db.sql) == 2
    assert all(statement.startswith('SET LOCAL ') for statement in db.sql)


@pytest.mark.parametrize('block', ['rollback', 'close'])
async def test_repeated_cancellation_joins_rollback_and_close(block):
    db = Session(block=block)
    body_entered = asyncio.Event()

    async def run():
        async with module.command_transaction(db.session):
            body_entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(body_entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(db.entered.wait(), 1)
        for _ in range(5):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), 'Caller must not return before cleanup is joined'
        db.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert db.events == ['rollback-start', 'rollback-done', 'close-start', 'close-done']
    finally:
        db.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('fail', ['commit', 'close'])
async def test_cleanup_failure_invalidates_connection_and_propagates(fail):
    db = Session(fail=fail)
    with pytest.raises(OSError, match=fail + ' failed'):
        async with module.command_transaction(db.session):
            pass
    assert db.events[-2:] == ['invalidate-start', 'invalidate-done']
    assert 'close-start' in db.events


async def test_rollback_failure_invalidates_and_retains_body_exception_context():
    db = Session(fail='rollback')
    original = ValueError('body failed')
    with pytest.raises(OSError, match='rollback failed') as caught:
        async with module.command_transaction(db.session):
            raise original
    assert caught.value.__context__ is original
    assert db.events[-2:] == ['invalidate-start', 'invalidate-done']
    assert 'close-done' in db.events


async def test_cleanup_deadline_invalidates_connection(monkeypatch):
    monkeypatch.setattr(module, '_CLEANUP_SECONDS', .02)
    db = Session(block='close')
    watchdog = asyncio.timeout(1)
    with pytest.raises(TimeoutError):
        async with watchdog:
            async with module.command_transaction(db.session):
                pass
    assert not watchdog.expired(), 'The cleanup deadline, not the test watchdog, must fire'
    assert db.entered.is_set()
    assert 'close-done' not in db.events
    assert db.events[-2:] == ['invalidate-start', 'invalidate-done']


async def test_invalidation_itself_has_a_deadline(monkeypatch):
    monkeypatch.setattr(module, '_INVALIDATE_SECONDS', .02)
    db = Session(fail='close', block='invalidate')
    watchdog = asyncio.timeout(1)
    with pytest.raises(TimeoutError) as caught:
        async with watchdog:
            async with module.command_transaction(db.session):
                pass
    assert not watchdog.expired(), 'The invalidation deadline must fire before the watchdog'
    assert db.entered.is_set()
    assert db.events[-1] == 'invalidate-start'
    assert isinstance(caught.value.__cause__, asyncio.CancelledError)


async def test_successful_context_inside_handled_exception_commits():
    session = Session()
    try:
        raise ValueError('already handled delivery failure')
    except ValueError:
        async with module.command_transaction(session.session):
            pass
    assert 'commit-done' in session.events
    assert 'rollback-start' not in session.events
