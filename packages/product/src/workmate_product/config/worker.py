"""Shared durable Worker scheduling policy; no deployment secrets."""
from pydantic import BaseModel,ConfigDict,Field


class WorkerPolicy(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    capacity:int=Field(strict=True,default=4,ge=1,le=1024)
    lease_seconds:int=Field(strict=True,default=30,ge=3,le=300)
    drain_seconds:int=Field(strict=True,default=30,ge=0,le=3600)
    startup_timeout_seconds:int=Field(strict=True,default=60,ge=1,le=600)


class WorkspaceLifecyclePolicy(BaseModel):
    """Idle compute eviction only; never authorizes deletion of working files."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    warm_ttl_seconds: int = Field(strict=True, default=600, ge=0, le=86400)
    sweep_interval_seconds: int = Field(strict=True, default=60, ge=1, le=3600)
    max_warm_per_worker: int = Field(strict=True, default=16, ge=0, le=1024)
