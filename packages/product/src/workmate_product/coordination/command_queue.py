"""At-least-once command transport; PostgreSQL alone grants execution authority.

Reading or reclaiming never acknowledges a message. Callers must durably settle,
reroute, or quarantine it before ACK. No transport method opens a DB transaction.
"""
from dataclasses import dataclass
import re
from typing import Annotated, Literal

from workmate_contracts import Identity

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.exceptions import ResponseError

_IDENTIFIER = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.-]+$')]


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError('invalid queue identifier')
    return value


@dataclass(frozen=True)
class QueueRoute:
    node_id: str
    worker_id: str | None = None
    control: bool = False

    def __post_init__(self):
        _identifier(self.node_id)
        if self.worker_id is not None:
            _identifier(self.worker_id)
        if type(self.control) is not bool:
            raise ValueError('control must be boolean')


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    # int strictness prevents True from passing Literal[1] validation.
    protocol_version: Annotated[int, Field(ge=1, le=1)] = 1
    command_id: Identity
    delivery_generation: Annotated[int, Field(ge=1)]
    storage_node_id: Identifier
    target_incarnation: Identifier | None = None


@dataclass(frozen=True)
class QueueMessage:
    message_id: str
    envelope: CommandEnvelope | None
    protocol_error: Literal['invalid_envelope', 'route_mismatch'] | None = None


@dataclass(frozen=True)
class PendingMessage:
    message_id: str
    consumer: str
    idle_ms: int
    delivery_count: int


# Run all observations and XTRIM atomically: ACK/READ/GROUP CREATE cannot change
# the proof mid-cleanup. Keep last-delivered itself (conservative) and everything
# at/after the oldest PEL entry, for EVERY group. No groups means no trim proof.
# Decimal string comparison avoids Lua floating-point precision loss for IDs.
_TRIM = """
local function less(a,b)
  local am,as=string.match(a,'^(%d+)%-(%d+)$')
  local bm,bs=string.match(b,'^(%d+)%-(%d+)$')
  if #am ~= #bm then return #am < #bm end
  if am ~= bm then return am < bm end
  if #as ~= #bs then return #as < #bs end
  return as < bs
end
if redis.call('EXISTS',KEYS[1]) == 0 then return 0 end
local groups=redis.call('XINFO','GROUPS',KEYS[1])
if #groups == 0 then return 0 end
local now=redis.call('TIME')
local cutoff=math.max(0,tonumber(now[1])*1000+math.floor(tonumber(now[2])/1000)-tonumber(ARGV[1]))
local boundary=string.format('%.0f',cutoff)..'-0'
for _,group in ipairs(groups) do
  local name=nil
  local delivered=nil
  for i=1,#group,2 do
    if group[i]=='name' then name=group[i+1] end
    if group[i]=='last-delivered-id' then delivered=group[i+1] end
  end
  if not name or not delivered then return 0 end
  if less(delivered,boundary) then boundary=delivered end
  local pending=redis.call('XPENDING',KEYS[1],name)
  if pending[1] > 0 and less(pending[2],boundary) then boundary=pending[2] end
end
return redis.call('XTRIM',KEYS[1],'MINID','=',boundary)
"""


def _text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else value


def _bounded(value: int, *, minimum=1, maximum=1000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('invalid queue operation bound')
    return value


class RedisCommandQueue:
    group = 'workers'

    def __init__(self, client, *, namespace: str):
        self.client = client
        self.namespace = _identifier(namespace)

    def stream_key(self, route: QueueRoute) -> str:
        prefix = f'workmate:{self.namespace}:'
        if route.worker_id is not None:
            return prefix + f'worker:{route.worker_id}:' + ('control' if route.control else 'execute')
        return prefix + f'node:{route.node_id}:' + ('control' if route.control else 'ready')

    async def ensure_group(self, route: QueueRoute) -> None:
        try:
            await self.client.xgroup_create(self.stream_key(route), self.group, id='0-0', mkstream=True)
        except ResponseError as exc:
            if not str(exc).startswith('BUSYGROUP'):
                raise

    def _consumer(self, route, consumer):
        _identifier(consumer)
        if route.worker_id is not None and consumer != route.worker_id:
            raise ValueError('directed route belongs to another worker')

    @staticmethod
    def _matches(route, envelope):
        return (envelope.storage_node_id == route.node_id
                and envelope.target_incarnation == route.worker_id)

    def _messages(self, route, records):
        result = []
        for message_id, fields in records:
            try:
                normalized = {_text(key): value for key, value in fields.items()}
                if set(normalized) != {'payload'}:
                    raise ValueError('unexpected envelope fields')
                envelope = CommandEnvelope.model_validate_json(normalized['payload'])
                if not self._matches(route, envelope):
                    result.append(QueueMessage(_text(message_id), None, 'route_mismatch'))
                else:
                    result.append(QueueMessage(_text(message_id), envelope))
            except (ValidationError, ValueError, TypeError, UnicodeError):
                result.append(QueueMessage(_text(message_id), None, 'invalid_envelope'))
        return result

    async def publish(self, route: QueueRoute, envelope: CommandEnvelope) -> str:
        # Revalidate even when a caller bypassed Pydantic with model_construct.
        envelope = CommandEnvelope.model_validate(envelope.model_dump())
        if not self._matches(route, envelope):
            raise ValueError('envelope does not match queue route')
        return _text(await self.client.xadd(self.stream_key(route), {'payload': envelope.model_dump_json()}))

    async def read(self, route: QueueRoute, consumer: str, *, count=1, block_ms=None) -> list[QueueMessage]:
        self._consumer(route, consumer)
        _bounded(count)
        if block_ms is not None:
            _bounded(block_ms, maximum=60000)  # Redis BLOCK 0 means forever.
        streams = await self.client.xreadgroup(
            self.group, consumer, {self.stream_key(route): '>'}, count=count, block=block_ms,
        )
        return [message for _, records in streams for message in self._messages(route, records)]

    async def pending(self, route: QueueRoute, *, min_idle_ms=0, count=100, start="-") -> list[PendingMessage]:
        _bounded(count)
        _bounded(min_idle_ms, minimum=0, maximum=2**63-1)
        records = await self.client.xpending_range(
            self.stream_key(route), self.group, start, '+', count, idle=min_idle_ms,
        )
        return [PendingMessage(_text(row['message_id']), _text(row['consumer']),
                               row['time_since_delivered'], row['times_delivered']) for row in records]

    async def claim_pending(self, route: QueueRoute, consumer: str, message_ids: list[str],
                            *, min_idle_ms: int) -> list[QueueMessage]:
        self._consumer(route, consumer)
        _bounded(min_idle_ms, minimum=0, maximum=2**63-1)
        if not message_ids:
            return []
        _bounded(len(message_ids))
        records = await self.client.xclaim(self.stream_key(route), self.group, consumer,
                                          min_idle_ms, message_ids)
        return self._messages(route, records)

    async def ack(self, route: QueueRoute, message_ids: list[str]) -> int:
        if not message_ids:
            return 0
        _bounded(len(message_ids))
        return await self.client.xack(self.stream_key(route), self.group, *message_ids)

    async def peek(self, route: QueueRoute, message_id: str) -> QueueMessage | None:
        """Read identity before PG-authorized reclaim, without changing the PEL."""
        records = await self.client.xrange(self.stream_key(route), min=message_id, max=message_id, count=1)
        messages = self._messages(route, records)
        return messages[0] if messages else None

    async def stream_has(self, route: QueueRoute, message_id: str) -> bool:
        return bool(await self.client.xrange(self.stream_key(route), min=message_id, max=message_id, count=1))

    async def trim_acknowledged(self, route: QueueRoute, *, retain_ms: int) -> int:
        _bounded(retain_ms, minimum=0, maximum=2**53-1)
        return await self.client.eval(_TRIM, 1, self.stream_key(route), retain_ms)

    async def trim_page(self, *, cursor: int = 0, count: int = 64, retain_ms: int) -> int:
        """Incrementally retain acknowledged history even on settled/idle routes.

        SCAN COUNT is a Redis work hint, not a strict result-size guarantee. Keep
        the returned cursor between maintenance ticks until it wraps to zero.
        Only exact command-stream names in this deployment are eligible; every
        eligible key still requires the atomic all-group confirmation proof.
        """
        _bounded(cursor, minimum=0, maximum=2**64-1)
        _bounded(count)
        _bounded(retain_ms, minimum=0, maximum=2**53-1)
        prefix = f'workmate:{self.namespace}:'
        command_key = re.compile(
            re.escape(prefix) + r'(?:node:[A-Za-z0-9_.-]{1,128}:(?:ready|control)'
            r'|worker:[A-Za-z0-9_.-]{1,128}:(?:execute|control))\Z',
        )
        following, keys = await self.client.scan(
            cursor=cursor, match=prefix + '*', count=count, _type='stream',
        )
        for key in keys:
            try:
                eligible = command_key.fullmatch(_text(key))
            except UnicodeError:
                continue
            if eligible:
                await self.client.eval(_TRIM, 1, key, retain_ms)
        return int(following)
