"""Worker-local retirement and purge loop; never starts a Sandbox for cleanup."""
import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select, or_
from workmate_product.persistence.models import (
    Conversation, ConversationRetirement, RuntimeSession, Task, RuntimeCommand, ACTIVE_TASK_STATUSES,
)


logger = logging.getLogger(__name__)


class RetirementWorker:
    def __init__(self, sessions, service, workspaces, *, artifact_dir=None, object_store=None):
        self.sessions, self.service, self.workspaces = sessions, service, workspaces
        self.failures = {}
        self._after = ''
        self._purge_after = ''
        self.artifact_dir = artifact_dir
        self.object_store = object_store

    async def tick(self):
        async with self.sessions() as db:
            rows = (await db.scalars(select(ConversationRetirement).where(
                ConversationRetirement.completed_at.is_(None),
                ConversationRetirement.storage_node_id == self.service.node_id,
                ConversationRetirement.conversation_id > self._after)
                .order_by(ConversationRetirement.conversation_id).limit(100))).all()
        self._after = rows[-1].conversation_id if rows else ''
        for row in rows:
            sid = row.session_id
            if sid in self.service.active_sessions:
                continue
            try:
                # Logical deletion of failed work does not prove quiescence.
                # Check durable gates BEFORE touching the local Workspace, even
                # when a crashed execution is absent from active_sessions.
                async with self.sessions() as db:
                    conv = await db.get(Conversation, row.conversation_id)
                    session = await db.get(RuntimeSession, sid)
                    if (conv is None or conv.deleted_at is None or session is None
                            or session.conversation_id != row.conversation_id
                            or session.storage_node_id != row.storage_node_id
                            or session.execution_epoch != row.execution_epoch):
                        raise RuntimeError('Retirement identity or gates changed')
                    historical = await db.scalar(select(RuntimeSession.id).where(
                        RuntimeSession.conversation_id == row.conversation_id,
                        RuntimeSession.id != sid).limit(1))
                    # The retirement receipt covers one Workspace only. A new
                    # Session being clean says nothing about historical files.
                    if historical is not None:
                        self.failures[sid] = 'HistoricalWorkspacesRequireRetirement'
                        continue
                    active = await db.scalar(select(Task.id).where(
                        Task.conversation_id == row.conversation_id,
                        Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1))
                    pending = await db.scalar(select(RuntimeCommand.id).where(
                        RuntimeCommand.conversation_id == row.conversation_id,
                        RuntimeCommand.status == 'accepted').limit(1))
                    if conv.active_product_task_id or session.active_task_id or active or pending:
                        self.failures[sid] = 'RetirementRequiresClosedExecution'
                        continue
                # Prefer actual locally held resource closure. After restart,
                # only persisted clean ownership can authorize storage cleanup.
                if not await self.workspaces.retire_session(sid):
                    if self.artifact_dir is None:
                        self.failures[sid] = 'RetirementRequiresWorkspaceOwner'
                        continue
                    from workmate_runtime.assembly.workspaces import WorkspaceIdentity
                    identity = WorkspaceIdentity(conv.user_id, conv.agent_id, conv.id, sid, row.storage_node_id)
                    await self.workspaces.retire_stored_workspace(identity)
                async with self.sessions.begin() as db:
                    conv = await db.scalar(select(Conversation).where(
                        Conversation.id == row.conversation_id).with_for_update())
                    session = await db.scalar(select(RuntimeSession).where(RuntimeSession.id == sid).with_for_update())
                    current = await db.scalar(select(ConversationRetirement).where(
                        ConversationRetirement.conversation_id == row.conversation_id).with_for_update())
                    if (conv is None or conv.deleted_at is None or conv.active_product_task_id is not None
                            or session is None or session.active_task_id is not None
                            or current is None or current.session_id != sid
                            or current.storage_node_id != self.service.node_id
                            or session.storage_node_id != current.storage_node_id
                            or session.execution_epoch != current.execution_epoch):
                        raise RuntimeError('Retirement identity or gates changed')
                    if current.completed_at is None:
                        current.completed_at = datetime.now(timezone.utc)
                        current.completed_by = self.service.worker_id
                self.failures.pop(sid, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failures[sid] = type(exc).__name__

    async def purge_pending(self):
        if self.artifact_dir is None:
            return
        from workmate_product.application.conversation_purge import purge_conversation
        from workmate_runtime.assembly.workspaces import WorkspaceIdentity
        from workmate_runtime.delivery.coordinator import _joined_io
        async with self.sessions() as db:
            ids = list(await db.scalars(select(ConversationRetirement.conversation_id).where(
                ConversationRetirement.completed_at.is_not(None),
                ConversationRetirement.purged_at.is_(None),
                or_(ConversationRetirement.storage_node_id == self.service.node_id,
                    ConversationRetirement.storage_node_id.is_(None)),
                ConversationRetirement.conversation_id > self._purge_after)
                .order_by(ConversationRetirement.conversation_id).limit(100)))
        self._purge_after = ids[-1] if ids else ''
        async def workspace(conv, row):
            identity = WorkspaceIdentity(conv.user_id, conv.agent_id, conv.id, row.session_id, row.storage_node_id)
            # Clean persistent ownership permits a restart retry; never starts a Sandbox.
            await self.workspaces.retire_stored_workspace(identity)
            await self.workspaces.purge_workspace(identity)
        for cid in ids:
            try:
                async with self.sessions.begin() as db:
                    await purge_conversation(db, conversation_id=cid, storage_root=self.artifact_dir, object_store=self.object_store,
                        worker_id=self.service.worker_id, purge_workspace=workspace, run_io=_joined_io)
                self.failures.pop('purge:' + cid, None)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.failures['purge:' + cid] = type(error).__name__
                logger.warning('Conversation purge pending conversation_id=%s exception_type=%s',
                    cid, type(error).__name__)

    async def serve(self, shutdown):
        while not shutdown.is_set():
            try:
                await self.tick()
                await self.purge_pending()
                self.failures.pop('database', None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failures['database'] = type(exc).__name__
            try:
                await asyncio.wait_for(shutdown.wait(), .2)
            except asyncio.TimeoutError:
                pass
