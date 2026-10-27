"""Provider-specific sandbox policy; connection secrets are environment-only."""
import os
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, TypeAdapter

Nonempty = Annotated[str, Field(min_length=1, pattern=r'.*\S.*')]
Timeout = Annotated[int, Field(strict=True, ge=1, le=86400)]


class _Policy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class BubblewrapSettings(_Policy):
    provider: Literal['bubblewrap'] = 'bubblewrap'


class LocalSettings(_Policy):
    """Explicit host execution without a security isolation boundary."""
    provider: Literal['local'] = 'local'


class DockerSettings(_Policy):
    provider: Literal['docker'] = 'docker'
    base_image: Nonempty = 'python:3.11-slim'
    node_version: Nonempty | None = None
    extra_pip: list[Nonempty] = Field(default_factory=list)


class KubernetesSettings(_Policy):
    provider: Literal['kubernetes'] = 'kubernetes'
    image: Nonempty = 'python:3.11-slim'
    namespace: Nonempty = 'agentscope'
    storage_class: Nonempty | None = None
    storage_size: Nonempty = '1Gi'
    kubeconfig: str | None = Field(default=None, exclude=True, repr=False)


class E2BSettings(_Policy):
    provider: Literal['e2b'] = 'e2b'
    template: Nonempty = 'base'
    timeout_seconds: Timeout = 300
    api_key: SecretStr = Field(default=SecretStr(''), exclude=True)
    domain: str = Field(default='', exclude=True, repr=False)


class DaytonaSettings(_Policy):
    provider: Literal['daytona'] = 'daytona'
    target: str = ''
    timeout_seconds: Timeout = 300
    api_key: SecretStr = Field(default=SecretStr(''), exclude=True)
    api_url: str = Field(default='', exclude=True, repr=False)


class OpenSandboxSettings(_Policy):
    provider: Literal['opensandbox'] = 'opensandbox'
    image: Nonempty = 'python:3.11-slim'
    protocol: Literal['http','https'] = 'http'
    timeout_seconds: Timeout = 300
    api_key: SecretStr = Field(default=SecretStr(''), exclude=True)
    domain: str = Field(default='', exclude=True, repr=False)


def _file_policy(value):
    if isinstance(value, dict) and {'api_key','api_url','domain','kubeconfig'} & value.keys():
        raise ValueError('Sandbox connections and credentials must use environment variables')
    if isinstance(value,dict) and 'provider' not in value:
        return {'provider':'bubblewrap',**value}
    return value


SandboxSettings = Annotated[
    BubblewrapSettings | LocalSettings | DockerSettings | KubernetesSettings | E2BSettings | DaytonaSettings | OpenSandboxSettings,
    Field(discriminator='provider'), BeforeValidator(_file_policy),
]
_adapter = TypeAdapter(SandboxSettings)


def parse_sandbox_settings(value) -> SandboxSettings:
    return _adapter.validate_python(value)


def sandbox_environment(policy: SandboxSettings) -> SandboxSettings:
    """Changing provider selects its defaults; same-provider overrides retain YAML policy."""
    selected = os.getenv('WORKMATE_SANDBOX_PROVIDER', '').strip() or policy.provider
    if selected != policy.provider:
        policy = parse_sandbox_settings({'provider': selected})
    names = {
        'kubernetes': {'kubeconfig':'WORKMATE_SANDBOX_KUBECONFIG'},
        'e2b': {'api_key':'E2B_API_KEY','domain':'E2B_DOMAIN'},
        'daytona': {'api_key':'DAYTONA_API_KEY','api_url':'DAYTONA_API_URL'},
        'opensandbox': {'api_key':'OPEN_SANDBOX_API_KEY','domain':'OPEN_SANDBOX_DOMAIN'},
    }.get(selected, {})
    updates = {}
    for field, name in names.items():
        value = os.getenv(name, '').strip()
        if value:
            updates[field] = SecretStr(value) if field=='api_key' else value
    return policy.model_copy(update=updates)
