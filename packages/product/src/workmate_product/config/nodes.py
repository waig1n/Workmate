"""Explicit trusted deployment nodes; never supplied by a task or model."""
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkerNode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    storage_node_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
    base_url: str
    enabled: bool = Field(default=True, strict=True)

    @field_validator('base_url')
    @classmethod
    def endpoint(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http','https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or parsed.path not in {'','/'} or '?' in value or '#' in value
            or any(c.isspace() for c in value)):
            raise ValueError('Node URL requires an HTTP(S) origin without credentials and a port in 1..65535')
        return value.rstrip('/')
