"""Shared per-conversation realtime log budget, independent of Redis connectivity."""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RealtimePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    retention_seconds: int = Field(strict=True, default=3600, ge=60, le=86400)
    max_log_bytes: int = Field(strict=True, default=8388608, ge=266240, le=134217728)
    ttl_seconds: int = Field(strict=True, default=7200, ge=60, le=172800)

    snapshot_interval_seconds: float = Field(strict=True, default=5.0, ge=0.1, le=60)
    snapshot_max_bytes: int = Field(strict=True, default=65536, ge=1024, le=8388608)
    max_buffer_bytes: int = Field(strict=True, default=2097152, ge=266240, le=67108864)
    max_buffer_events: int = Field(strict=True, default=256, ge=16, le=8192)
    operation_timeout_seconds: float = Field(strict=True, default=2.0, ge=0.1, le=30)
    outage_grace_seconds: float = Field(strict=True, default=30.0, ge=1, le=300)

    reader_page_events: int = Field(strict=True, default=100, ge=1, le=1000)
    client_buffer_events: int = Field(strict=True, default=256, ge=16, le=8192)
    client_buffer_bytes: int = Field(strict=True, default=2097152, ge=266240, le=67108864)
    reader_block_ms: int = Field(strict=True, default=1000, ge=1, le=60000)
    reset_max_rows: int = Field(strict=True, default=10000, ge=1, le=100000)
    reset_max_bytes: int = Field(strict=True, default=8388608, ge=266240, le=67108864)
    authorize_interval_seconds: float = Field(strict=True, default=15.0, ge=0.1, le=60)
    heartbeat_interval_seconds: float = Field(strict=True, default=15.0, ge=0.1, le=30)
    reset_retry_seconds: float = Field(strict=True, default=2.0, ge=0.1, le=30)
    max_gateway_readers: int = Field(strict=True, default=1024, ge=1, le=16384)
    max_gateway_subscriptions: int = Field(strict=True, default=4096, ge=1, le=65536)

    @model_validator(mode='after')
    def ttl_covers_retention(self):
        if self.ttl_seconds < self.retention_seconds:
            raise ValueError('realtime ttl_seconds must cover retention_seconds')
        if self.outage_grace_seconds < self.operation_timeout_seconds:
            raise ValueError('realtime outage_grace_seconds must cover operation_timeout_seconds')
        return self


def realtime_overrides(environment):
    result = {}
    for name in RealtimePolicy.model_fields:
        key = 'WORKMATE_REALTIME_' + name.upper()
        raw = environment.get(key)
        if raw is None:
            raw = environment.get(key.lower())
        if raw not in (None, ''):
            parse = float if RealtimePolicy.model_fields[name].annotation is float else int
            result[name] = parse(raw)
    return result
