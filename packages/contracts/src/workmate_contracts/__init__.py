"""Versioned role-neutral wire contracts. No application or engine imports."""

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

Identity = Annotated[str, Field(min_length=1, max_length=128)]
MAX_PAYLOAD_BYTES = 262_144


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    protocol_version: Literal[1] = 1


class Command(Contract):
    """Trusted internal envelope; authorization is rechecked by the executor.

    Payloads are JSON values, never credentials. Operation-specific validation
    and database idempotency are responsibilities of the application service.
    """

    command_id: Identity
    kind: Literal["PrepareSession", "StartTask", "ResumeInteraction", "StopTask"]
    conversation_id: Identity
    runtime_session_id: Identity
    product_task_id: Identity | None = None
    reply_id: Identity | None = None
    interaction_id: Identity | None = None
    # Product approval_request_version is a content digest, not a row revision.
    request_version: Identity | None = None
    expected_epoch: Annotated[int, Field(ge=0)] = 0
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_envelope(self):
        if self.kind != "PrepareSession" and self.product_task_id is None:
            raise ValueError("Task command requires product_task_id")
        if self.kind == "ResumeInteraction" and any(value is None for value in (
            self.reply_id, self.interaction_id, self.request_version,
        )):
            raise ValueError("Resume requires reply, interaction and request version")
        if len(self.canonical_payload()) > MAX_PAYLOAD_BYTES:
            raise ValueError("Command payload exceeds 256 KiB")
        return self

    def canonical_payload(self) -> bytes:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")

    def content_digest(self) -> str:
        content = self.model_dump(exclude={"command_id"})
        return hashlib.sha256(json.dumps(
            content, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()


class ProductCursor(Contract):
    """Browser replay watermark; never accepts a Runtime sequence."""

    conversation_id: Identity
    product_seq: Annotated[int, Field(ge=0)]


class PendingInteraction(Contract):
    interaction_id: Identity
    request_version: Identity
    kind: Literal["permission", "external_execution"]
    pending_tool_call_ids: list[Identity] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_calls(self):
        if len(set(self.pending_tool_call_ids)) != len(self.pending_tool_call_ids):
            raise ValueError("Pending tool call identities must be unique")
        return self


class TaskExecution(Contract):
    """Persisted query result; a disconnected stream does not imply completion.

    This is execution status only. Product delivery/admission gates are separate.
    A partial approval may park without a new Core event; Worker persistence must
    derive pending_interaction from the complete snapshot, not just the stream.
    """

    conversation_id: Identity
    runtime_session_id: Identity
    product_task_id: Identity
    reply_id: Identity | None = None
    status: Literal[
        "accepted", "preparing", "running", "awaiting_interaction", "stopping",
        "completed", "failed", "cancelled", "interrupted",
    ]
    execution_epoch: Annotated[int, Field(ge=0)]
    state_version: Annotated[int, Field(ge=0)]
    pending_interaction: PendingInteraction | None = None

    @model_validator(mode="after")
    def consistent_state(self):
        if self.status in {"preparing", "running", "awaiting_interaction", "completed"}:
            if self.execution_epoch == 0:
                raise ValueError("Execution requires an acquired epoch")
        if self.status in {"running", "awaiting_interaction", "completed"} and not self.reply_id:
            raise ValueError("Started execution requires a reply identity")
        if self.status == "awaiting_interaction" and self.pending_interaction is None:
            raise ValueError("Parked execution requires pending interaction")
        if self.pending_interaction is not None and (not self.reply_id or self.execution_epoch == 0):
            raise ValueError("Pending interaction requires its original reply and acquired epoch")
        if self.pending_interaction is not None and self.status not in {
            "awaiting_interaction", "stopping",
        }:
            raise ValueError("Pending interaction is only valid while parked or stopping")
        return self


class Event(Contract):
    conversation_id: Identity
    product_task_id: Identity | None = None
    reply_id: Identity | None = None
    event_type: Identity
    occurred_at: AwareDatetime = Field(strict=False)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class RuntimeEvent(Event):
    """Worker outbox identity, not a browser cursor or audit record."""

    runtime_event_id: Identity
    runtime_session_id: Identity
    runtime_seq: Annotated[int, Field(ge=1)]
    execution_epoch: Annotated[int, Field(ge=1)]


class ProductEvent(Event):
    """Durable public replay identity, allocated in the projection transaction."""

    product_event_id: Identity
    product_seq: Annotated[int, Field(ge=1)]
    source_runtime_event_id: Identity | None = None


def snapshot_digest(snapshot: dict) -> str:
    """SHA-256 of the complete persisted snapshot envelope, independent of engine."""
    return hashlib.sha256(_snapshot_json(snapshot).encode('utf-8')).hexdigest()


def _snapshot_json(value) -> str:
    # PostgreSQL JSONB can expand exponent numbers and erase a float/int spelling
    # distinction. Encode numeric VALUES in fixed decimal notation, preserving
    # arbitrary integer precision and keeping booleans distinct from numbers.
    if isinstance(value,dict):
        if any(not isinstance(key,str) for key in value):
            raise ValueError('Snapshot object keys must be strings')
        return '{'+','.join(json.dumps(key,ensure_ascii=False)+':'+_snapshot_json(value[key])
                            for key in sorted(value))+'}'
    if isinstance(value,list):
        return '['+','.join(_snapshot_json(item) for item in value)+']'
    if type(value) in (int,float):
        number=Decimal(str(value))
        if not number.is_finite():
            raise ValueError('Snapshot numbers must be finite')
        text=format(number,'f')
        if '.' in text:
            text=text.rstrip('0').rstrip('.')
        return '0' if number==0 else text
    if value is None or isinstance(value,(str,bool)):
        return json.dumps(value,ensure_ascii=False,allow_nan=False)
    raise ValueError('Snapshot must contain JSON values')


class PendingToolCall(Contract):
    """Worker-derived pending identity; raw input is preserved without re-encoding."""
    id: Identity
    name: Annotated[str, Field(min_length=1)]
    input: str
    kind: Literal['permission','external_execution']


class BoundaryEvidence(Contract):
    """Bound to an immutable snapshot; only a trusted Worker may derive this view.

    The hash binds canonical snapshot values, not proof that an untrusted sender has
    correctly summarized Core. Worker construction is separately Core-tested.
    """
    snapshot_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    pending_tools: list[PendingToolCall]

    @model_validator(mode='after')
    def validate_pending(self):
        if len({call.id for call in self.pending_tools})!=len(self.pending_tools):
            raise ValueError('Pending tool identities must be unique')
        if len(self.model_dump_json().encode('utf-8'))>MAX_PAYLOAD_BYTES:
            raise ValueError('Boundary evidence exceeds 256 KiB')
        return self
