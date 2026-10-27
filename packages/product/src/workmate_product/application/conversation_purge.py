"""Purge retired content and publish one durable completion receipt."""
from datetime import datetime, timezone
import re
from .storage_cleanup import remove_storage_path
from .security_audit import SecurityAuditService
from ..persistence.repositories.conversation_purge import lock_purge, object_keys, delete_content


_AUDIT_FIELDS = frozenset({'toolCallId', 'tool', 'user', 'status', 'actorUserId',
    'decisionVersion', 'requestVersion', 'permissionMode', 'scope', 'from', 'to',
    'replyId', 'interactionId', 'taskId', 'artifactId', 'source', 'changeType',
    'size', 'durationMs', 'steps', 'model', 'synthetic'})


def retained_audit(payload):
    """Keep governance facts, never the recoverable original call/content."""
    result = {'purged': True, **{key: value for key, value in payload.items()
        if key in _AUDIT_FIELDS and isinstance(value, (str, int, float, bool))}}
    for key, fields in (('decisions', {'toolCallId', 'approved'}), ('toolCalls', {'id', 'name'})):
        if isinstance(payload.get(key), list):
            result[key] = [{k: v for k, v in item.items() if k in fields
                and isinstance(v, (str, bool))} for item in payload[key] if isinstance(item, dict)]
    return result


async def purge_conversation(db, *, conversation_id, storage_root, worker_id,
                             purge_workspace, run_io, object_store=None):
    """Caller transaction holds storage exclusion through file deletion and commit.

    Files go first. On failure/rollback all descriptors remain for an idempotent
    retry; a missing file is success. Other conversations' sealed objects remain.
    """
    conv, retirement = await lock_purge(db, conversation_id)
    if retirement.purged_at is not None:
        return False
    if retirement.session_id is not None:
        await purge_workspace(conv, retirement)
    keys = await object_keys(db, conversation_id)
    shared = await object_keys(db, conversation_id, others=True)
    for key in sorted(keys - shared):
        match = re.fullmatch(r'\.objects/sha256/([0-9a-f]{2})/([0-9a-f]{64})', key)
        if match is None or match[1] != match[2][:2]:
            raise RuntimeError('Unrecognized artifact storage key; deletion refused')
        if object_store is None:
            await run_io(remove_storage_path, storage_root, key)
        else:
            await run_io(object_store.delete, key)
    if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', conversation_id) is None:
        raise RuntimeError('Invalid conversation storage identity')
    if object_store is None:
        await run_io(remove_storage_path, storage_root, '.inputs/' + conversation_id)
    else:
        await run_io(object_store.delete_inputs, conversation_id)
    await delete_content(db, conversation_id, redact_audit=retained_audit)
    retirement.purged_at = datetime.now(timezone.utc)
    retirement.purged_by = worker_id
    SecurityAuditService(db).record('conversation.purge', actor_user_id=retirement.requested_by,
        metadata={'conversation_id': conversation_id, 'worker_id': worker_id})
    await db.flush()
    return True
