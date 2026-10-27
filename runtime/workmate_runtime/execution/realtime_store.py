"""Short SQL transactions around the Runtime's process-local display writer.

Core checkpoints and display cuts are different recovery records. The factory
restores display content only from its stable Task/Reply message row. Redis I/O
runs after every database context has exited and never holds a guard write lock.
"""

from sqlalchemy import select, text

from workmate_contracts.realtime import message_id
from workmate_product.application.message_snapshots import save_message_snapshot
from workmate_product.persistence.models import Conversation, TaskMessage
from workmate_product.persistence.repositories.message_snapshots import from_message
from workmate_product.realtime.message_accumulator import MessageAccumulator

from .cancellation import cancel_once
from .realtime_writer import RealtimeAuthorityLost, RealtimeWriter


class StoredRealtimeWriter:
    """Do not advertise a display cut when no assistant content was persisted."""

    def __init__(self, writer, should_save):
        self._writer = writer
        self._should_save = should_save

    async def accept(self, events):
        return await self._writer.accept(events)

    async def checkpoint(self, phase, incomplete=False):
        snapshot = await self._writer.checkpoint(phase, incomplete=incomplete)
        return snapshot if self._should_save(snapshot) else None

    async def emergency_checkpoint(self):
        snapshot = await self._writer.emergency_checkpoint()
        return snapshot if self._should_save(snapshot) else None

    async def run_flush_timer(self):
        await self._writer.run_flush_timer()

    async def close(self):
        await self._writer.close()


class RealtimeWriterFactory:
    def __init__(self, sessions, *, log_manager, log, policy):
        self.sessions = sessions
        self.log_manager = log_manager
        self.log = log
        self.policy = policy

    @cancel_once
    async def __call__(self, guard, initial):
        """Create lazily after Reply binding; ``initial`` is a Core boundary.

        ``guard.display_authority`` is an async, I/O-free lease/failure check.
        Database ownership is checked at restore and each durable display cut,
        never for each incoming text delta.
        """
        if guard.reply_id is None or await guard.display_authority() is not True:
            raise RealtimeAuthorityLost("Reply has no current display authority")
        owner = guard.owner
        reply_id = guard.reply_id
        async with self.sessions.begin() as db:
            conversation_id = await db.scalar(
                text("""
                SELECT s.conversation_id FROM runtime_sessions s
                JOIN tasks t ON t.id=s.active_task_id AND t.conversation_id=s.conversation_id
                JOIN conversations c ON c.id=s.conversation_id
                JOIN runtime_task_executions e ON e.task_id=t.id AND e.session_id=s.id
                WHERE s.superseded_at IS NULL AND s.id=:session_id AND s.active_task_id=:task_id
                  AND s.owner_id=:owner_id AND s.execution_epoch=:epoch
                  AND s.reply_id=:reply_id AND s.lease_until>clock_timestamp()
                  AND e.execution_epoch=:epoch AND e.reply_id=:reply_id
                  AND (t.run_id IS NULL OR t.run_id=:reply_id)
                  AND c.deleted_at IS NULL AND c.active_product_task_id=t.id
            """),
                dict(
                    session_id=owner.session_id,
                    task_id=owner.task_id,
                    owner_id=owner.worker_id,
                    epoch=owner.execution_epoch,
                    reply_id=reply_id,
                ),
            )
            if conversation_id is None:
                raise RealtimeAuthorityLost(
                    "Display restore has no current Task/Reply owner"
                )
            row = await db.get(TaskMessage, message_id(owner.task_id, reply_id))
            restored = None
            if row is not None:
                if (
                    row.role != "assistant"
                    or row.task_id != owner.task_id
                    or row.conversation_id != conversation_id
                    or row.snapshot_version == 0
                ):
                    raise RealtimeAuthorityLost(
                        "Display message identity is inconsistent"
                    )
                restored = from_message(row)

        accumulator = MessageAccumulator(
            conversation_id, owner.task_id, reply_id, owner.execution_epoch, restored
        )

        def should_save(snapshot):
            return restored is not None or any(block.text for block in snapshot.blocks)

        @cancel_once
        async def business_seq():
            async with self.sessions.begin() as db:
                value = await db.scalar(
                    select(Conversation.business_seq).where(
                        Conversation.id == conversation_id,
                        Conversation.deleted_at.is_(None),
                    )
                )
                if value is None:
                    raise RealtimeAuthorityLost("Display Conversation is unavailable")
                return value

        @cancel_once
        async def save(snapshot, *, unanchored):
            if not should_save(snapshot):
                return
            async with self.sessions.begin() as db:
                await save_message_snapshot(
                    db, snapshot, owner_id=owner.worker_id, unanchored=unanchored
                )

        writer = RealtimeWriter(
            accumulator,
            conversation_id=conversation_id,
            log_manager=self.log_manager,
            log=self.log,
            policy=self.policy,
            check_authority=guard.display_authority,
            read_business_seq=business_seq,
            save_snapshot=save,
        )
        return StoredRealtimeWriter(writer, should_save)
