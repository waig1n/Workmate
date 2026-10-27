# -*- coding: utf-8 -*-
"""Application settings: explicit args > env/.env > YAML > defaults."""

import json
from functools import lru_cache
from typing import Annotated, Literal
from pydantic import AliasChoices, Field, field_validator, model_validator

from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .config_file import ConfigFileSettingsSource, DEFAULT_ORGANIZATION_SKILL_IDS, SkillIdentifier
from workmate_product.config.skill_storage import SkillStoragePolicy, SkillS3Endpoint, SkillS3Bucket, skill_storage_overrides


from workmate_product.config.artifact_storage import ArtifactStoragePolicy, artifact_storage_overrides
from workmate_product.config.nodes import WorkerNode
from workmate_product.config.coordination import CoordinationRedisURL
from workmate_product.config.command_queue import CommandQueuePolicy, command_queue_overrides
from workmate_product.config.realtime import RealtimePolicy, realtime_overrides


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    @field_validator("builtin_skill_organization_use_ids", mode="before")
    @classmethod
    def parse_builtin_skill_ids(cls, value):
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, list):
            raise ValueError("builtin_skill_organization_use_ids must be a JSON array")
        return value

    database_url: str = "postgresql+asyncpg://workmate:workmate-dev-password@localhost:5432/workmate"

    # Canonical browser-facing frontend URL; deployment setting shared by outbound links.
    workmate_public_url: str = ''

    @field_validator('workmate_public_url')
    @classmethod
    def validate_workmate_public_url(cls, value):
        from urllib.parse import urlsplit
        value = value.strip()
        if not value:
            return value
        url = urlsplit(value)
        if (url.port is not None and not 1 <= url.port <= 65535
                or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value
                or not url.hostname or url.username or url.password or url.query or url.fragment
                or url.scheme not in {'http', 'https'}):
            raise ValueError('Workmate public URL requires HTTP or HTTPS without credentials, query or fragment')
        return value.rstrip('/')

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24 * 7

    # Self-registration policy (user management plan §3.1):
    # bootstrap_only = only the very first user may self-register (prod
    # default); closed = admin-created users only; open = anyone may
    # register and becomes a root-scope member.
    registration_mode: Literal["bootstrap_only", "closed", "open"] = (
        "bootstrap_only"
    )

    # Deployment storage affinity for durable Worker admission; must match the
    # target Worker's stable mount identity, never a transient process owner ID.
    worker_storage_node_id: str = Field(
        default="local-worker-storage",
        validation_alias=AliasChoices("WORKMATE_WORKER_STORAGE_NODE_ID", "worker_storage_node_id"),
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )

    coordination_redis_url: CoordinationRedisURL = Field(
        default="", repr=False,
        validation_alias=AliasChoices("WORKMATE_COORDINATION_REDIS_URL", "coordination_redis_url"),
    )

    command_queue: CommandQueuePolicy = Field(default_factory=CommandQueuePolicy)
    realtime: RealtimePolicy = Field(default_factory=RealtimePolicy)
    coordination_namespace: str = Field(default='workmate', pattern=r'^[A-Za-z0-9_-]{1,64}$',
        validation_alias=AliasChoices('WORKMATE_COORDINATION_NAMESPACE','coordination_namespace'))

    runtime_base_url: str = "http://localhost:8000"
    runtime_timeout_secs: float = 30.0
    # Shared secret for workmate-internal runtime endpoints
    # (/workmate/workspace/capabilities/sync); must match the runtime's
    # WORKMATE_INTERNAL_TOKEN. Empty disables those calls (startup warns).
    runtime_internal_token: str = ""

    worker_nodes: list[WorkerNode] = Field(default_factory=list,
        validation_alias=AliasChoices('WORKMATE_WORKER_NODES', 'worker_nodes'))
    artifact_storage: ArtifactStoragePolicy = Field(default_factory=ArtifactStoragePolicy)
    artifact_s3_endpoint: SkillS3Endpoint = ''
    artifact_s3_bucket: SkillS3Bucket = 'workmate-artifacts'
    artifact_s3_access_key: str = Field(default='', repr=False)
    artifact_s3_secret_key: str = Field(default='', repr=False)

    @property
    def artifact_storage_backend(self):
        return self.artifact_storage.backend

    @model_validator(mode='after')
    def validate_nodes(self):
        identities = [n.storage_node_id for n in self.worker_nodes]
        if len(set(identities)) != len(identities):
            raise ValueError('Worker storage node identities must be unique')
        if self.worker_nodes and self.artifact_storage.backend != 's3':
            raise ValueError('Explicit Worker nodes require S3 artifact storage')
        return self

    artifact_storage_dir: str = "./artifact-storage"
    skill_storage: SkillStoragePolicy = Field(default_factory=SkillStoragePolicy)
    skill_s3_endpoint: SkillS3Endpoint = ''
    skill_s3_bucket: SkillS3Bucket = 'workmate-skills'
    skill_s3_access_key: str = Field(default='', repr=False)
    skill_s3_secret_key: str = Field(default='', repr=False)

    artifact_max_file_bytes: int = 50 * 1024 * 1024
    artifact_max_files_per_run: int = 20
    artifact_preview_extensions: str = (
        "pdf,txt,md,csv,json,yaml,yml,png,jpg,jpeg,webp,gif"
    )
    artifact_force_download_extensions: str = (
        "html,htm,xhtml,svg,xml,js,mjs,zip,tar,gz"
    )
    artifact_delivery_mode: Literal["snapshot_fallback", "hybrid"] = "hybrid"
    artifact_strict_delivery: bool = False
    # Existing shared catalog policy, consumed by the native API role.
    github_mcp_hub_enabled: bool = False
    skillhub_cn_enabled: bool = False
    skillhub_cn_base_url: str = Field(default="https://api.skillhub.cn", pattern=r"^https?://\S+$")
    skillhub_cn_request_timeout_secs: float = Field(default=30.0, gt=0)
    skillhub_cn_max_retries: int = Field(default=2, ge=0, le=10)
    builtin_skill_organization_use_ids: Annotated[list[SkillIdentifier], NoDecode] = Field(default_factory=lambda: list(DEFAULT_ORGANIZATION_SKILL_IDS))
    workspace_assistant_enabled: bool = True
    event_queue_capacity: int = Field(default=1024, gt=0)
    task_max_outstanding: int = Field(default=64, gt=0)
    task_max_outstanding_per_user: int = Field(default=8, gt=0)
    task_max_pending_approval: int = Field(default=256, gt=0)
    task_max_pending_approval_per_user: int = Field(default=32, gt=0)
    approval_timeout_secs: int = Field(default=86400, gt=0)
    channel_enabled: bool = True
    channel_wechat_enabled: bool = False
    channel_reply_delay_secs: int = Field(default=30, ge=30, le=300)
    channel_stream_interval_secs: float = Field(default=1, ge=1, le=10)
    channel_max_live_replies: int = Field(default=64, ge=1, le=256)
    channel_poll_interval_secs: float = Field(default=2, ge=1, le=30)
    channel_batch_size: int = Field(default=20, ge=1, le=100)
    channel_lease_secs: int = Field(default=60, ge=30, le=300)
    channel_max_connections: int = Field(default=32, ge=1, le=256)
    channel_max_inbox_pending: int = Field(default=1000, ge=1, le=100000)
    channel_message_max_chars: int = Field(default=16000, ge=1, le=64000)
    schedule_enabled: bool = True
    schedule_poll_interval_secs: float = Field(default=5, ge=0.1, le=60)
    schedule_batch_size: int = Field(default=20, ge=1, le=100)
    schedule_misfire_grace_secs: int = Field(default=300, ge=1, le=3600)

    # Session lock TTL on the runtime side; reconciliation must wait
    # past this before declaring a run dead (design §7, correction #3).
    session_lock_ttl_secs: int = 600

    # Chat model selection lives in workmate.yaml (product.models), read
    # through app.config_file — never env vars. The environment carries
    # only each profile's API key (its api_key_env) and the
    # CHAT_MODEL_BASE_URL gateway fallback for null base_url profiles.

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        def with_skill_policy(source):
            def values():
                result = source()
                realtime = realtime_overrides(source.env_vars)
                if realtime:
                    result['realtime'] = {**result.get('realtime', {}), **realtime}
                queue = command_queue_overrides(source.env_vars)
                if queue:
                    result['command_queue'] = {**result.get('command_queue', {}), **queue}
                overrides = skill_storage_overrides(source.env_vars)
                if overrides:
                    result['skill_storage'] = {**result.get('skill_storage', {}), **overrides}
                artifacts = artifact_storage_overrides(source.env_vars)
                if artifacts:
                    result['artifact_storage'] = {**result.get('artifact_storage', {}), **artifacts}
                return result
            return values

        return (
            init_settings,
            with_skill_policy(env_settings),
            with_skill_policy(dotenv_settings),
            ConfigFileSettingsSource(settings_cls),
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
