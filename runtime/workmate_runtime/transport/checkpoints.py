"""Publish checkpoint metadata only under the current execution fence."""
import hashlib
import json
import uuid
from .delivery import DeliveryStore


class CheckpointStore:
    def __init__(self, db):
        self.db = db

    async def record(self, owner, node, *, boundary, manifest=None, error_code=None):
        if boundary not in {'terminal', 'approval'} or (manifest is None) == (error_code is None):
            raise ValueError('Invalid checkpoint result')
        encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')) if manifest is not None else None
        digest = hashlib.sha256(encoded.encode()).hexdigest() if encoded else None
        async with self.db.transaction():
            _, _, start = await DeliveryStore(self.db)._owner(owner, node)
            checkpoint_id = str(uuid.uuid4())
            await self.db.execute('''INSERT INTO workspace_checkpoints
                (id,conversation_id,task_id,session_id,execution_epoch,storage_node_id,
                 boundary,status,manifest,digest,error_code)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11)''',
                checkpoint_id, start['conversation_id'], owner.task_id, owner.session_id,
                owner.execution_epoch, node, boundary,
                'available' if manifest is not None else 'unavailable', encoded, digest, error_code)
            # The owner check locks the execution rows; also reject lease expiry
            # during insertion instead of publishing a stale recovery point.
            await DeliveryStore(self.db)._owner(owner, node)
            return checkpoint_id
