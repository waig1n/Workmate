"""Bounded Product IM channel policy; credentials are business-private data."""
from pydantic import BaseModel, ConfigDict, Field

class ChannelPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    enabled: bool = True
    wechat_enabled: bool = False
    reply_delay_secs: int = Field(default=30, ge=30, le=300)
    stream_interval_secs: float = Field(default=1, ge=1, le=10)
    max_live_replies: int = Field(default=64, ge=1, le=256)
    poll_interval_secs: float = Field(default=2, ge=1, le=30)
    batch_size: int = Field(default=20, ge=1, le=100)
    lease_secs: int = Field(default=60, ge=30, le=300)
    max_connections: int = Field(default=32, ge=1, le=256)
    max_inbox_pending: int = Field(default=1000, ge=1, le=100000)
    message_max_chars: int = Field(default=16000, ge=1, le=64000)

    @classmethod
    def from_settings(cls, settings):
        return cls(**{name: getattr(settings, 'channel_' + name) for name in cls.model_fields})
