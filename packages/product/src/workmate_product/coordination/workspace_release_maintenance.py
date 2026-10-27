"""Bounded Gateway retirement of internal release notifications.

This never releases resources, changes a holding, or ACKs an unresolved request.
An unavailable incarnation gets a durable diagnostic, not fabricated cleanup.
Streams/group metadata and their final retained entry intentionally remain:
PG references and concurrent Redis producers cannot be atomically proven absent.
"""
from sqlalchemy import text

from .command_queue import _text
from .directed_queue_maintenance import DirectedQueueMaintenance
from .workspace_release_queue import WorkspaceReleaseEnvelope
from ..persistence.repositories.command_deliveries import quarantine_message


class WorkspaceReleaseMaintenance(DirectedQueueMaintenance):
    consumer = 'gateway-release-maintenance'

    def __init__(self, sessions, queue, registry):
        super().__init__(sessions, queue, registry, suffixes=('workspace-release',))

    async def _diagnostic(self, stream, message_id, reason):
        async with self.sessions.begin() as db:
            await quarantine_message(db, stream, message_id, reason)

    async def _disposition(self, route, message_id, fields):
        stream = self.queue.stream_key(route)
        try:
            normalized = {_text(key): value for key, value in fields.items()}
            if set(normalized) != {'payload'}:
                raise ValueError('Unexpected envelope fields')
            envelope = WorkspaceReleaseEnvelope.model_validate_json(normalized['payload'])
        except (ValueError, TypeError, UnicodeError):
            await self._diagnostic(stream, message_id, 'invalid_envelope')
            return True
        async with self.sessions.begin() as db:
            row = (await db.execute(text('SELECT * FROM workspace_release_requests WHERE id=:id'),
                                   {'id':envelope.request_id})).mappings().first()
        if (envelope.target_incarnation != route.worker_id or row is None or
                any(row[key] != value for key, value in {
                    'session_id':envelope.session_id, 'storage_node_id':envelope.storage_node_id,
                    'expected_worker_id':envelope.target_incarnation,
                    'workspace_generation':envelope.workspace_generation,
                    'expected_hold_version':envelope.expected_hold_version}.items())):
            await self._diagnostic(stream, message_id, 'release_identity_mismatch')
            return True
        # Recheck after PG read: a renewed or draining live holder handles its own
        # notices. A race after this check is harmless: only terminal facts ACK.
        if await self.registry.worker(route.worker_id, node_id=row['storage_node_id'], allow_draining=True):
            return False
        if row['status'] in {'released', 'stale', 'blocked'}:
            return True
        await self._diagnostic(stream, message_id, 'release_holder_unavailable')
        return False
