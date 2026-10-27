"""Rebuildable Worker health and Workspace affinity hints, never execution locks.

TTL expiry proves neither resource cleanup nor ownership release. Readers must
compare affinity against the current PostgreSQL residence tuple before routing;
an old hint can reappear after Redis loss/expiry. Capacity zero still permits
sticky queuing, while draining suppresses new execution affinity.
"""
import hashlib
import json
from typing import Annotated

from workmate_contracts import Identity
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .command_queue import Identifier, _identifier, _text


class WorkerRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    incarnation: Identifier
    node_id: Identifier
    total_capacity: Annotated[int, Field(ge=1, le=1024)]
    available_capacity: Annotated[int, Field(ge=0, le=1024)]
    draining: bool = False

    @model_validator(mode='after')
    def capacity_bounds(self):
        if self.available_capacity > self.total_capacity:
            raise ValueError('Available capacity exceeds total capacity')
        return self


class AffinityRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    session_id: Identity
    node_id: Identifier
    incarnation: Identifier
    workspace_generation: Identifier
    hold_version: Annotated[int, Field(ge=1, le=2**63-1)]


# Version is stored as a decimal STRING outside the record. Redis Lua cjson uses
# doubles and cannot order full PostgreSQL bigint versions exactly as numbers.
_AFFINITY_PUBLISH = """
local previous=redis.call('GET',KEYS[1])
if previous then
  local ok,old=pcall(cjson.decode,previous)
  if ok and type(old)=='table' and type(old.version)=='string' and type(old.holder)=='string' then
    local version=ARGV[3]
    if #old.version>#version or (#old.version==#version and old.version>version) then return 0 end
    if old.version==version and old.holder~=ARGV[4] then return 0 end
  end
end
redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2])
return 1
"""
_AFFINITY_WITHDRAW = """
local previous=redis.call('GET',KEYS[1])
if not previous then return 0 end
local ok,old=pcall(cjson.decode,previous)
if not ok or type(old)~='table' or old.version~=ARGV[1] or old.holder~=ARGV[2] then return 0 end
return redis.call('DEL',KEYS[1])
"""
_WORKER_WITHDRAW = """
local previous=redis.call('GET',KEYS[1])
if not previous then return 0 end
local ok,old=pcall(cjson.decode,previous)
if not ok or type(old)~='table' or old.incarnation~=ARGV[1] or old.node_id~=ARGV[2] then return 0 end
return redis.call('DEL',KEYS[1])
"""


def _ttl(seconds):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError('Worker hint TTL must be an integer between 1 and 3600 seconds')
    return seconds


def _holder(record):
    return json.dumps([record.session_id, record.node_id, record.incarnation,
                       record.workspace_generation], separators=(',', ':'), ensure_ascii=True)


class RedisWorkerRegistry:
    def __init__(self, client, *, namespace: str):
        self.client = client
        self.namespace = _identifier(namespace)

    def worker_key(self, incarnation: str) -> str:
        return f'workmate:{self.namespace}:worker:{_identifier(incarnation)}:health'

    def affinity_key(self, session_id: str) -> str:
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
            raise ValueError('Invalid session identity')
        digest = hashlib.sha256(session_id.encode('utf-8')).hexdigest()
        return f'workmate:{self.namespace}:session:{digest}:affinity'

    async def heartbeat(self, record: WorkerRecord, *, ttl_seconds: int = 30) -> None:
        """Caller passes the existing Worker lease TTL and refreshes at TTL/3."""
        record = WorkerRecord.model_validate(record.model_dump())
        await self.client.set(self.worker_key(record.incarnation), record.model_dump_json(), ex=_ttl(ttl_seconds))

    async def worker(self, incarnation: str, *, node_id: str | None = None,
                     allow_draining: bool = False) -> WorkerRecord | None:
        if node_id is not None:
            _identifier(node_id)
        raw = await self.client.get(self.worker_key(incarnation))
        if raw is None:
            return None
        try:
            record = WorkerRecord.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError):
            return None
        if record.incarnation != incarnation or (node_id is not None and record.node_id != node_id):
            return None
        return record if allow_draining or not record.draining else None

    async def withdraw_worker(self, record: WorkerRecord) -> bool:
        # Incarnation-specific keys prevent an old process deleting a new one's
        # health; compare the stored identity too rather than blindly deleting.
        record = WorkerRecord.model_validate(record.model_dump())
        return bool(await self.client.eval(_WORKER_WITHDRAW, 1, self.worker_key(record.incarnation),
                                           record.incarnation, record.node_id))

    async def publish_affinity(self, record: AffinityRecord, *, ttl_seconds: int = 30) -> bool:
        record = AffinityRecord.model_validate(record.model_dump())
        holder = _holder(record)
        payload = json.dumps({'version': str(record.hold_version), 'holder': holder,
                              'record': record.model_dump()}, separators=(',', ':'), ensure_ascii=True)
        return bool(await self.client.eval(_AFFINITY_PUBLISH, 1, self.affinity_key(record.session_id),
                                          payload, _ttl(ttl_seconds), str(record.hold_version), holder))

    async def affinity(self, session_id: str, node_id: str) -> AffinityRecord | None:
        _identifier(node_id)
        raw = await self.client.get(self.affinity_key(session_id))
        if raw is None:
            return None
        try:
            wrapper = json.loads(_text(raw))
            record = AffinityRecord.model_validate(wrapper['record'])
            if wrapper['version'] != str(record.hold_version) or wrapper['holder'] != _holder(record):
                return None
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None
        if record.session_id != session_id or record.node_id != node_id:
            return None
        if await self.worker(record.incarnation, node_id=node_id) is None:
            return None
        return record

    async def withdraw_affinity(self, record: AffinityRecord) -> bool:
        record = AffinityRecord.model_validate(record.model_dump())
        return bool(await self.client.eval(_AFFINITY_WITHDRAW, 1, self.affinity_key(record.session_id),
                                           str(record.hold_version), _holder(record)))
