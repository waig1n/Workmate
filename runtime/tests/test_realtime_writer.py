"""Single-writer cuts survive transport and persistence ambiguity without reapplying Core."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from workmate_contracts.realtime import RealtimeCursor
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.realtime.message_accumulator import MessageAccumulator
from workmate_product.realtime.redis_log import LogRef
from workmate_runtime.execution.realtime_writer import (
    RealtimeWriter,
    RealtimeUnavailable,
    RealtimeAuthorityLost,
)

pytestmark = pytest.mark.asyncio


def raw(kind, **values):
    return {"type": kind, "reply_id": "reply", **values}


def start():
    return raw("TEXT_BLOCK_START", block_id="b")


def delta(value):
    return raw("TEXT_BLOCK_DELTA", block_id="b", delta=value)


class Fixture:
    def __init__(self, *, restored=None, epoch=1, policy=None):
        self.now = 0.0
        self.current = LogRef("c", str(uuid4()), str(uuid4()))
        self.sent = []
        self.saved = []
        self.authorized = True
        self.business_seq = 0
        self.failure = None
        self.after_append = None
        self.save_failure = None
        self.save_calls = []
        self.calls = 0
        self.accumulator = MessageAccumulator("c", "t", "reply", epoch, restored)
        self.writer = RealtimeWriter(
            self.accumulator,
            conversation_id="c",
            log_manager=self,
            log=self,
            check_authority=self.authority,
            read_business_seq=self.business,
            save_snapshot=self.save,
            policy=policy or RealtimePolicy(),
            clock=lambda: self.now,
        )

    async def authority(self):
        return self.authorized

    async def business(self):
        return self.business_seq

    async def ensure(self, cid):
        assert cid == "c"
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.current

    async def is_current(self, ref):
        return self.current == ref

    async def append(self, ref, envelope):
        self.sent.append(envelope)
        if self.after_append:
            await self.after_append(ref, envelope)
        return SimpleNamespace(
            cursor=RealtimeCursor(
                conversation_id="c",
                generation=ref.generation,
                stream_id=f"{len(self.sent)}-0",
            )
        )

    async def save(self, snapshot, *, unanchored):
        self.save_calls.append((snapshot.model_copy(deep=True), unanchored))
        previous = next(
            (
                s
                for s, _ in self.saved
                if s.snapshot_version == snapshot.snapshot_version
            ),
            None,
        )
        if previous is not None:
            assert previous == snapshot
        else:
            self.saved.append((snapshot.model_copy(deep=True), unanchored))
        if self.save_failure:
            raise self.save_failure


async def test_batch_applied_once_and_cut_uses_confirmed_append_not_later_tail():
    f = Fixture()
    await f.writer.accept([start(), delta("你"), delta("😀")])
    assert [e.text.content_version for e in f.sent if e.text] == [1, 2]
    f.business_seq = 7
    cut = await f.writer.checkpoint("approval")
    assert cut.blocks[0].text == "你😀" and cut.blocks[0].content_version == 2
    assert cut.business_seq == 7 and cut.covered_cursor.stream_id == "3-0"
    assert not f.saved[-1][1]
    final = await f.writer.checkpoint("final")
    assert final.snapshot_version == cut.snapshot_version + 1 and final.blocks[0].closed
    assert await f.writer.checkpoint("final") == final
    assert len(f.saved) == 2
    await f.writer.close()


async def test_response_lost_retries_original_envelope_and_does_not_duplicate_text():
    f = Fixture()
    lost = True

    async def loss(ref, event):
        nonlocal lost
        if lost:
            lost = False
            raise ConnectionError("reply lost")

    f.after_append = loss
    await f.writer.accept([start(), delta("once")])
    assert f.saved and f.saved[0][1] and f.saved[0][0].covered_cursor is None
    f.now = 3
    cut = await f.writer.checkpoint("approval")
    assert f.sent[0] == f.sent[1]
    assert cut.blocks[0].text == "once" and cut.blocks[0].content_version == 1
    assert cut.covered_cursor.stream_id == "3-0"
    await f.writer.close()


async def test_snapshot_response_loss_blocks_new_raw_until_same_cut_confirmed():
    f = Fixture()
    await f.writer.accept([start(), delta("first")])
    f.save_failure = TimeoutError("commit response lost")
    with pytest.raises(RealtimeUnavailable):
        await f.writer.checkpoint("approval")
    pending = f.save_calls[-1][0]
    with pytest.raises(RealtimeUnavailable):
        await f.writer.accept(delta("must not apply"))
    assert f.save_calls[-1][0] == pending
    f.save_failure = None
    await f.writer.accept(delta(" next"))
    assert f.save_calls[-1][0] == pending
    cut = await f.writer.checkpoint("final")
    assert cut.blocks[0].text == "first next" and cut.blocks[0].content_version == 2
    await f.writer.close()


async def test_terminal_capture_retries_exact_same_version_after_unknown_save():
    f = Fixture()
    await f.writer.accept([start(), delta("done")])
    f.save_failure = ConnectionError("lost")
    with pytest.raises(RealtimeUnavailable):
        await f.writer.checkpoint("final")
    cut = f.save_calls[-1][0]
    f.save_failure = None
    assert await f.writer.checkpoint("final") == cut
    assert f.save_calls[-1][0] == cut and len(f.saved) == 1
    await f.writer.close()


async def test_outage_has_first_cut_and_backoff_without_per_delta_pg_writes():
    f = Fixture()
    f.failure = ConnectionError("offline")
    await f.writer.accept([start(), delta("a")])
    assert len(f.saved) == 1 and f.saved[-1][1]
    for _ in range(10):
        await f.writer.accept(delta("b"))
    assert len(f.saved) == 1 and f.calls == 1
    f.now = 5
    cut = await f.writer.checkpoint("approval")
    assert cut.blocks[0].text == "a" + "b" * 10 and cut.covered_cursor is None
    assert len(f.saved) == 2
    await f.writer.close()


async def test_buffer_limit_saves_applied_body_before_controlled_stop():
    f = Fixture(policy=RealtimePolicy(max_buffer_events=16))
    f.failure = ConnectionError("offline")
    await f.writer.accept([start(), delta("a")])
    with pytest.raises(RealtimeUnavailable, match="buffer"):
        await f.writer.accept([delta("b") for _ in range(20)])
    assert f.saved[-1][1] and f.saved[-1][0].blocks[0].text == "a" + "b" * 20
    stopped = await f.writer.checkpoint("interrupted")
    assert stopped.phase == "interrupted" and stopped.blocks[0].closed
    assert stopped.blocks[0].text == "a" + "b" * 20
    assert f.writer.pending_event_count <= 16
    assert f.writer.pending_bytes <= f.writer.policy.max_buffer_bytes
    with pytest.raises(RealtimeUnavailable):
        await f.writer.accept(delta("later"))
    await f.writer.close()


async def test_authority_loss_never_appends_or_saves_more_state():
    f = Fixture()
    await f.writer.accept([start(), delta("a")])
    f.authorized = False
    with pytest.raises(RealtimeAuthorityLost):
        await f.writer.accept(delta("forbidden"))
    with pytest.raises(RealtimeAuthorityLost):
        await f.writer.checkpoint("final")
    assert len(f.sent) == 2 and not f.saved
    await f.writer.close()


async def test_rotation_invalidates_old_cursor_before_saving_cut():
    f = Fixture()
    await f.writer.accept([start(), delta("a")])
    old = f.current
    f.current = LogRef("c", str(uuid4()), str(uuid4()))
    cut = await f.writer.checkpoint("approval")
    assert cut.covered_cursor is None and f.saved[-1][1]
    await f.writer.accept(delta("b"))
    cut = await f.writer.checkpoint("final")
    assert (
        cut.blocks[0].text == "ab" and cut.covered_cursor.generation != old.generation
    )
    await f.writer.close()


async def test_restored_same_reply_keeps_message_and_block_versions():
    first = Fixture()
    await first.writer.accept([start(), delta("old")])
    cut = await first.writer.checkpoint("approval")
    await first.writer.close()
    resumed = Fixture(restored=cut, epoch=2)
    await resumed.writer.accept(delta(" new"))
    final = await resumed.writer.checkpoint("final")
    assert final.message_id == cut.message_id and final.execution_epoch == 2
    assert final.blocks[0].text == "old new" and final.blocks[0].content_version == 2
    assert final.snapshot_version == cut.snapshot_version + 1
    await resumed.writer.close()


async def test_close_does_not_fabricate_terminal_and_oversize_batch_is_rejected():
    f = Fixture()
    with pytest.raises(ValueError):
        await f.writer.accept([start()] * 65)
    assert not f.sent and not f.saved
    await f.writer.close()
    assert not f.saved
    with pytest.raises(RuntimeError):
        await f.writer.accept(start())


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_timer_saves_quiet_tool_period_and_serializes_concurrent_accept():
    f = Fixture(policy=RealtimePolicy(snapshot_interval_seconds=0.1))
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(ref, event):
        entered.set()
        await release.wait()

    f.after_append = pause
    accepting = asyncio.create_task(f.writer.accept([start(), delta("complete cut")]))
    await entered.wait()
    f.now = 1
    timer = asyncio.create_task(f.writer.run_flush_timer())
    await asyncio.sleep(0)
    assert not f.saved and not accepting.done()
    release.set()
    await accepting
    await until(lambda: bool(f.saved))
    assert f.saved[-1][0].blocks[0].text == "complete cut"
    assert f.saved[-1][0].covered_cursor.stream_id == "2-0"
    await f.writer.close()
    assert timer.done()
    assert all(s.phase == "partial" for s, _ in f.saved)


async def test_timer_snapshot_failure_propagates_to_controller():
    f = Fixture(policy=RealtimePolicy(snapshot_interval_seconds=0.1))
    await f.writer.accept([start(), delta("save me")])
    f.now = 1
    f.save_failure = ConnectionError("PG unavailable")
    timer = asyncio.create_task(f.writer.run_flush_timer())
    with pytest.raises(RealtimeUnavailable, match="commit"):
        await asyncio.wait_for(timer, 1)
    assert f.save_calls[-1][0].blocks[0].text == "save me"
    await f.writer.close()


async def test_actual_network_timeout_and_outage_grace_save_before_stop():
    f = Fixture(
        policy=RealtimePolicy(operation_timeout_seconds=0.1, outage_grace_seconds=1.0)
    )

    async def hung(cid):
        await asyncio.Event().wait()

    f.ensure = hung
    await asyncio.wait_for(f.writer.accept([start(), delta("retained")]), 1)
    assert f.saved[-1][1] and f.saved[-1][0].blocks[0].text == "retained"
    f.now = 1.1
    with pytest.raises(RealtimeUnavailable, match="grace"):
        await asyncio.wait_for(f.writer.checkpoint("approval"), 1)
    assert f.saved[-1][0].blocks[0].text == "retained"
    stopped = await f.writer.checkpoint("interrupted")
    assert stopped.phase == "interrupted" and stopped.blocks[0].closed
    await f.writer.close()


async def test_rotation_saves_prefix_before_new_generation_suffix():
    f = Fixture()
    await f.writer.accept([start(), delta("prefix")])
    f.current = LogRef("c", str(uuid4()), str(uuid4()))

    async def check_cut(ref, envelope):
        assert f.saved and f.saved[-1][1]
        assert f.saved[-1][0].blocks[0].text == "prefix suffix"

    f.after_append = check_cut
    await f.writer.accept(delta(" suffix"))
    final = await f.writer.checkpoint("final")
    assert final.covered_cursor.generation == f.current.generation
    await f.writer.close()


async def test_generation_changed_after_append_never_uses_old_receipt():
    f = Fixture()
    old = f.current

    async def rotate(ref, envelope):
        f.current = LogRef("c", str(uuid4()), str(uuid4()))
        f.after_append = None

    f.after_append = rotate
    await f.writer.accept([start(), delta("safe")])
    assert f.saved[-1][1] and f.saved[-1][0].covered_cursor is None
    f.now = 3
    final = await f.writer.checkpoint("final")
    assert f.sent[0] == f.sent[1] and final.blocks[0].text == "safe"
    assert final.covered_cursor.generation != old.generation
    await f.writer.close()


async def test_dirty_byte_threshold_flushes_without_waiting_interval():
    f = Fixture(policy=RealtimePolicy(snapshot_max_bytes=1024))
    await f.writer.accept([start(), delta("x" * 1500)])
    assert len(f.saved) == 1 and f.saved[0][0].blocks[0].text == "x" * 1500
    assert f.saved[0][0].phase == "partial"
    await f.writer.close()


async def test_wrong_conversation_accumulator_never_writes_other_conversation():
    f = Fixture()
    f.writer.conversation_id = "other"
    with pytest.raises(ValueError, match="Conversation"):
        await f.writer.accept(start())
    with pytest.raises(ValueError, match="Conversation"):
        await f.writer.checkpoint("approval")
    assert not f.sent and not f.saved
    await f.writer.close()


async def test_byte_buffer_limit_keeps_transport_bounded_and_saves_full_applied_text():
    f = Fixture(policy=RealtimePolicy(max_buffer_bytes=266240))
    f.failure = ConnectionError("offline")
    await f.writer.accept([start(), delta("a" * 140000)])
    with pytest.raises(RealtimeUnavailable, match="buffer"):
        await f.writer.accept([delta("b" * 140000), delta("trailing")])
    assert f.writer.pending_bytes <= 266240
    assert f.saved[-1][0].blocks[0].text == "a" * 140000 + "b" * 140000 + "trailing"
    stopped = await f.writer.checkpoint("interrupted")
    assert stopped.phase == "interrupted" and stopped.blocks[0].closed
    assert stopped.covered_cursor is None
    assert stopped.blocks[0].text == "a" * 140000 + "b" * 140000 + "trailing"
    await f.writer.close()


async def test_timer_alone_captures_after_tool_becomes_silent():
    f = Fixture(policy=RealtimePolicy(snapshot_interval_seconds=0.1))
    await f.writer.accept(
        [start(), delta("before tool"), raw("TOOL_CALL_START", tool_call_id="call")]
    )
    assert not f.saved
    f.now = 0.2
    timer = asyncio.create_task(f.writer.run_flush_timer())
    await until(lambda: bool(f.saved))
    assert f.saved[-1][0].process_end == len("before tool")
    assert f.saved[-1][0].blocks[0].text == "before tool"
    await f.writer.close()
    assert timer.done()


from test_execution_context import database
from test_realtime_redis_log import log as real_log


async def test_real_pg_redis_exact_cut_commit_loss_and_same_reply_resume(
    database, real_log, monkeypatch
):
    from sqlalchemy import text
    from workmate_contracts.realtime import decode_cursor
    from workmate_product.application.message_snapshots import save_message_snapshot
    from workmate_product.realtime.log_manager import EventLogManager
    from test_message_snapshots import running

    sessions, admin = database
    await running(admin)
    manager = EventLogManager(sessions, real_log)
    owner, epoch = "owner", 1

    async def authority():
        async with sessions.begin() as db:
            return bool(
                await db.scalar(
                    text("""SELECT owner_id=:owner AND execution_epoch=:epoch
                AND lease_until>clock_timestamp() FROM runtime_sessions WHERE id='s' """),
                    {"owner": owner, "epoch": epoch},
                )
            )

    async def business():
        async with sessions.begin() as db:
            return await db.scalar(
                text("SELECT business_seq FROM conversations WHERE id='c'")
            )

    cuts = []
    lose_commit = True

    async def save(cut, *, unanchored):
        nonlocal lose_commit
        cuts.append(cut.model_copy(deep=True))
        async with sessions.begin() as db:
            await save_message_snapshot(db, cut, owner_id=owner, unanchored=unanchored)
        if lose_commit:
            lose_commit = False
            raise ConnectionError("commit acknowledgement lost")

    append = real_log.append
    confirmed = []

    async def later_tail(ref, envelope):
        assert sessions.kw["bind"].pool.checkedout() == 0
        result = await append(ref, envelope)
        confirmed.append(result.cursor)
        await append(
            ref, envelope
        )  # Response-loss duplicate advances tail, not this cut.
        return result

    monkeypatch.setattr(real_log, "append", later_tail)
    writer = RealtimeWriter(
        MessageAccumulator("c", "t", "reply", 1),
        conversation_id="c",
        log_manager=manager,
        log=real_log,
        check_authority=authority,
        read_business_seq=business,
        save_snapshot=save,
        policy=RealtimePolicy(),
    )
    await writer.accept([start(), delta("你")])
    with pytest.raises(RealtimeUnavailable, match="commit"):
        await writer.checkpoint("approval")
    approval = await writer.checkpoint("approval")
    assert cuts[-1] == cuts[-2] == approval
    stored = decode_cursor(
        await admin.fetchval(
            "SELECT covered_cursor FROM task_messages WHERE role='assistant'"
        )
    )
    ref = await manager.ensure("c")
    assert stored == confirmed[-1] and stored != (await real_log.window(ref)).last
    assert (
        await admin.fetchval(
            "SELECT snapshot_version FROM task_messages WHERE role='assistant'"
        )
        == 1
    )
    await writer.close()
    owner, epoch = "next-worker", 2
    await admin.execute(
        "UPDATE runtime_sessions SET owner_id=$1,execution_epoch=2", owner
    )
    await admin.execute("UPDATE runtime_task_executions SET execution_epoch=2")
    resumed = RealtimeWriter(
        MessageAccumulator("c", "t", "reply", 2, approval),
        conversation_id="c",
        log_manager=manager,
        log=real_log,
        check_authority=authority,
        read_business_seq=business,
        save_snapshot=save,
        policy=RealtimePolicy(),
    )
    await resumed.accept(delta("😀"))
    final = await resumed.checkpoint("final")
    assert final.message_id == approval.message_id and final.snapshot_version == 2
    assert final.blocks[0].text == "你😀" and final.blocks[0].content_version == 2
    assert (
        await admin.fetchval(
            "SELECT content->>'text' FROM task_messages WHERE role='assistant'"
        )
        == "你😀"
    )
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM task_messages WHERE role='assistant'"
        )
        == 1
    )
    await resumed.close()


async def test_real_pg_redis_outage_keeps_unanchored_body_and_rotation_reanchors(
    database, real_log, monkeypatch
):
    from sqlalchemy import text
    from workmate_product.application.message_snapshots import save_message_snapshot
    from workmate_product.realtime.log_manager import EventLogManager
    from test_message_snapshots import running

    sessions, admin = database
    await running(admin)
    manager = EventLogManager(sessions, real_log)

    async def authority():
        return True  # Persistence independently fences real owner/epoch.

    async def business():
        async with sessions.begin() as db:
            return await db.scalar(
                text("SELECT business_seq FROM conversations WHERE id='c'")
            )

    saved = []

    async def save(cut, *, unanchored):
        async with sessions.begin() as db:
            await save_message_snapshot(
                db, cut, owner_id="owner", unanchored=unanchored
            )
        saved.append((cut, unanchored))

    writer = RealtimeWriter(
        MessageAccumulator("c", "t", "reply", 1),
        conversation_id="c",
        log_manager=manager,
        log=real_log,
        check_authority=authority,
        read_business_seq=business,
        save_snapshot=save,
        policy=RealtimePolicy(),
    )
    await writer.accept([start(), delta("prefix")])
    first = await writer.checkpoint("partial")
    old = await manager.ensure("c")
    original = manager.ensure

    async def offline(cid):
        raise ConnectionError("Redis offline")

    monkeypatch.setattr(manager, "ensure", offline)
    await writer.accept(delta(" offline"))
    assert saved[-1][1] and saved[-1][0].covered_cursor is None
    assert (
        await admin.fetchval(
            "SELECT covered_cursor FROM task_messages WHERE role='assistant'"
        )
        is None
    )
    assert (
        await admin.fetchval(
            "SELECT content->>'text' FROM task_messages WHERE role='assistant'"
        )
        == "prefix offline"
    )
    monkeypatch.setattr(manager, "ensure", original)
    await real_log.client.delete(*real_log.keys("c", old.generation))
    final = await writer.checkpoint("final")
    assert final.covered_cursor.generation != first.covered_cursor.generation
    assert (
        final.blocks[0].text == "prefix offline"
        and final.blocks[0].content_version == 2
    )
    assert (
        await admin.fetchval(
            "SELECT snapshot_phase FROM task_messages WHERE role='assistant'"
        )
        == "final"
    )
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM task_messages WHERE role='assistant'"
        )
        == 1
    )
    await writer.close()

async def test_emergency_cut_after_transport_exhaustion_never_retries_redis():
    fixture = Fixture(policy=RealtimePolicy(outage_grace_seconds=1,operation_timeout_seconds=.1))
    fixture.failure = ConnectionError('unavailable')
    await fixture.writer.accept([start(), delta('retained')])
    fixture.now = 2
    with pytest.raises(RealtimeUnavailable):
        await fixture.writer.accept([delta(' prefix')])
    calls = fixture.calls
    cut = await fixture.writer.emergency_checkpoint()
    assert fixture.calls == calls
    assert cut.phase == 'interrupted' and cut.incomplete
    assert cut.covered_cursor is None
    assert ''.join(block.text for block in cut.blocks) == 'retained prefix'
    assert fixture.saved[-1][0] == cut

async def test_emergency_cut_still_rejects_lost_authority():
    fixture=Fixture(policy=RealtimePolicy(outage_grace_seconds=1,operation_timeout_seconds=.1))
    fixture.failure=ConnectionError('offline')
    await fixture.writer.accept([start(),delta('prefix')])
    fixture.now=2
    with pytest.raises(RealtimeUnavailable):
        await fixture.writer.accept([delta(' tail')])
    fixture.authorized=False
    with pytest.raises(RealtimeAuthorityLost):
        await fixture.writer.emergency_checkpoint()
    assert not any(s.phase=='interrupted' for s,_ in fixture.saved)
