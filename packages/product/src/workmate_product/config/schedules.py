"""Non-secret, bounded Product scheduler policy."""
from pydantic import BaseModel, ConfigDict, Field


class SchedulePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    enabled: bool = True
    poll_interval_secs: float = Field(default=5, ge=0.1, le=60)
    batch_size: int = Field(default=20, ge=1, le=100)
    misfire_grace_secs: int = Field(default=300, ge=1, le=3600)

    @classmethod
    def from_settings(cls, settings):
        return cls(**{name: getattr(settings, 'schedule_' + name) for name in cls.model_fields})
