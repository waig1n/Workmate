"""Shared bounded command delivery policy; connection and namespace are deployment values."""
from pydantic import BaseModel, ConfigDict, Field


class CommandQueuePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    poll_interval_seconds: float = Field(strict=True, default=.2, gt=0, le=10)
    batch_size: int = Field(strict=True, default=64, ge=1, le=1000)
    lease_seconds: int = Field(strict=True, default=30, ge=3, le=300)
    repair_interval_seconds: int = Field(strict=True, default=30, ge=1, le=3600)
    redelivery_after_seconds: int = Field(strict=True, default=120, ge=10, le=86400)
    max_attempts: int = Field(strict=True, default=20, ge=1, le=1000)
    retry_max_seconds: int = Field(strict=True, default=60, ge=1, le=3600)
    pending_idle_seconds: int = Field(strict=True, default=30, ge=1, le=3600)
    retention_seconds: int = Field(strict=True, default=86400, ge=60, le=604800)


def command_queue_overrides(environment):
    result = {}
    for name in CommandQueuePolicy.model_fields:
        raw = environment.get('WORKMATE_COMMAND_QUEUE_'+name.upper())
        if raw is None:
            raw = environment.get(('WORKMATE_COMMAND_QUEUE_'+name.upper()).lower())
        if raw not in (None, ''):
            result[name] = float(raw) if name == 'poll_interval_seconds' else int(raw)
    return result
