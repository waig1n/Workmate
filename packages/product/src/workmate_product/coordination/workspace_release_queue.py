"""Typed internal Workspace release notices, separate from user commands.

A notice locates an already committed PG release request. Before any resource
operation the Worker validates the entire expected holding tuple against PG.
Busy/pending requests remain pending; ACK requires a durable released, stale or
blocked disposition. Missing Redis notices can be republished from PG requests.
"""
from dataclasses import dataclass
import re
from typing import Annotated, Literal

from workmate_contracts import Identity
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .command_queue import Identifier, QueueRoute, RedisCommandQueue, _bounded, _text, _TRIM


class WorkspaceReleaseEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    protocol_version: Annotated[int, Field(ge=1, le=1)] = 1
    kind: Literal['workspace_release'] = 'workspace_release'
    request_id: Identity
    session_id: Identity
    storage_node_id: Identifier
    target_incarnation: Identifier
    workspace_generation: Identifier
    expected_hold_version: Annotated[int, Field(ge=1, le=2**63-1)]


@dataclass(frozen=True)
class WorkspaceReleaseMessage:
    message_id: str
    envelope: WorkspaceReleaseEnvelope | None
    protocol_error: Literal['invalid_envelope', 'route_mismatch'] | None = None


class RedisWorkspaceReleaseQueue(RedisCommandQueue):
    """Reuse bounded Streams mechanics while isolating key, group and protocol.

    QueueRoute must be directed with control=True. These are lifecycle messages,
    never StopTask commands; command consumers cannot read this separate Stream.
    """
    group = 'workspace-release-workers'

    def stream_key(self, route: QueueRoute) -> str:
        if route.worker_id is None or not route.control:
            raise ValueError('Workspace release requires a directed control route')
        return f'workmate:{self.namespace}:worker:{route.worker_id}:workspace-release'

    def _messages(self, route, records):
        result = []
        for message_id, fields in records:
            try:
                normalized = {_text(key): value for key, value in fields.items()}
                if set(normalized) != {'payload'}:
                    raise ValueError('Unexpected release envelope fields')
                envelope = WorkspaceReleaseEnvelope.model_validate_json(normalized['payload'])
                if not self._matches(route, envelope):
                    result.append(WorkspaceReleaseMessage(_text(message_id), None, 'route_mismatch'))
                else:
                    result.append(WorkspaceReleaseMessage(_text(message_id), envelope))
            except (ValidationError, ValueError, TypeError, UnicodeError):
                result.append(WorkspaceReleaseMessage(_text(message_id), None, 'invalid_envelope'))
        return result

    async def publish(self, route: QueueRoute, envelope: WorkspaceReleaseEnvelope) -> str:
        envelope = WorkspaceReleaseEnvelope.model_validate(envelope.model_dump())
        if not self._matches(route, envelope):
            raise ValueError('Release envelope does not match route')
        return _text(await self.client.xadd(self.stream_key(route), {'payload': envelope.model_dump_json()}))

    async def trim_page(self, *, cursor: int = 0, count: int = 64, retain_ms: int) -> int:
        _bounded(cursor, minimum=0, maximum=2**64-1)
        _bounded(count)
        _bounded(retain_ms, minimum=0, maximum=2**53-1)
        prefix = f'workmate:{self.namespace}:worker:'
        release_key = re.compile(re.escape(prefix) + r'[A-Za-z0-9_.-]{1,128}:workspace-release\Z')
        following, keys = await self.client.scan(
            cursor=cursor, match=prefix+'*:workspace-release', count=count, _type='stream',
        )
        for key in keys:
            try:
                eligible = release_key.fullmatch(_text(key))
            except UnicodeError:
                continue
            if eligible:
                await self.client.eval(_TRIM, 1, key, retain_ms)
        return int(following)
