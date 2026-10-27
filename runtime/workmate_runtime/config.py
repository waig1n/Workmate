# -*- coding: utf-8 -*-
"""Runtime settings loaded from YAML with environment overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal, Union
from urllib.parse import urlsplit

import yaml
from workmate_product.config.coordination import CoordinationRedisURL
from workmate_product.config.command_queue import CommandQueuePolicy, command_queue_overrides
from workmate_product.config.realtime import RealtimePolicy, realtime_overrides
from workmate_product.config.worker import WorkerPolicy, WorkspaceLifecyclePolicy
from workmate_product.config.sandbox import SandboxSettings, BubblewrapSettings, sandbox_environment
from workmate_product.config.skill_storage import SkillStoragePolicy, SkillS3Endpoint, SkillS3Bucket, skill_storage_overrides
from workmate_product.config.artifact_storage import ArtifactStoragePolicy, artifact_storage_overrides
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


ModelInput = Literal["text", "image", "audio", "video"]
PdfStrategy = Literal["extract_text", "native"]


class _MediaProfile(_StrictModel):
    model_inputs: list[ModelInput] = Field(default_factory=lambda: ["text"])
    pdf_strategy: PdfStrategy = "extract_text"

    @model_validator(mode="after")
    def validate_model_inputs(self):
        if "text" not in self.model_inputs:
            raise ValueError("media profile model_inputs must include text")
        if len(self.model_inputs) != len(set(self.model_inputs)):
            raise ValueError("media profile model_inputs must be unique")
        return self


def _default_media_profiles() -> dict[str, _MediaProfile]:
    return {"text": _MediaProfile()}


class _ModelProfile(_StrictModel):
    model_type: Literal["openai", "dashscope"] = Field(alias="type")
    model: str
    # Name of the env var holding the gateway URL — the URL itself never
    # lives in this file. Null/empty means the provider SDK default.
    base_url_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z_][A-Z0-9_]*$",
    )
    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    dimensions: int | None = Field(default=None, gt=0)
    media_profile: str = Field(
        default="text",
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    supports_thinking: bool = False
    context_size: int = Field(default=32768, gt=0)
    max_output_tokens: int = Field(default=8192, gt=0)


class ChatModelProfile(BaseModel):
    """One chat model profile, consumed by the multi-model card machinery.

    The runtime resolves capabilities per profile so conversations running
    different models get matching synthetic ModelCards, Read tool bindings
    and context sizes. Values come from workmate.yaml; the environment only
    carries secrets (``api_key_env``) and resolves ``base_url`` from the
    environment variable named by ``base_url_env``.
    """

    name: str
    model_type: Literal["openai", "dashscope"]
    model: str
    # Resolved at load time from the profile's base_url_env.
    base_url: str | None = None
    # Preserve the existing YAML reference for durable execution specs; resolved
    # endpoint values must not be copied into command or snapshot metadata.
    base_url_env: str | None = None
    api_key_env: str = "CHAT_MODEL_API_KEY"
    media_profile: str = "text"
    model_input_types: list[str] = Field(
        default_factory=lambda: ["text/plain"],
    )
    pdf_strategy: PdfStrategy = "extract_text"
    supports_thinking: bool = False
    context_size: int = Field(default=32768, gt=0)
    max_output_tokens: int = Field(default=8192, gt=0)


class _ModelsConfig(_StrictModel):
    default: str | None = None
    media_profiles: dict[str, _MediaProfile] = Field(
        default_factory=_default_media_profiles,
    )
    profiles: dict[str, _ModelProfile] = Field(default_factory=dict)
    embedding: _ModelProfile | None = None

    @model_validator(mode="before")
    @classmethod
    def include_builtin_text_media_profile(cls, value):  # noqa: ANN001
        if not isinstance(value, dict):
            return value
        result = dict(value)
        media_profiles = dict(result.get("media_profiles") or {})
        media_profiles.setdefault(
            "text",
            {"model_inputs": ["text"], "pdf_strategy": "extract_text"},
        )
        result["media_profiles"] = media_profiles
        return result

    @model_validator(mode="after")
    def validate_default_profile(self):
        if self.default is not None and self.default not in self.profiles:
            raise ValueError(
                f"default model profile does not exist: {self.default}",
            )
        adapter_inputs = {
            "openai": {"text", "image", "audio", "pdf"},
            "dashscope": {"text", "image", "audio", "video"},
        }
        for name, profile in self.profiles.items():
            media = self.media_profiles.get(profile.media_profile)
            if media is None:
                raise ValueError(
                    f"model profile {name!r} references unknown media "
                    f"profile {profile.media_profile!r}",
                )
            required = set(media.model_inputs)
            if media.pdf_strategy == "native":
                required.add("pdf")
            unsupported = required - adapter_inputs[profile.model_type]
            if unsupported:
                values = ", ".join(sorted(unsupported))
                raise ValueError(
                    f"model profile {name!r} uses adapter "
                    f"{profile.model_type!r}, which cannot encode: {values}",
                )
        return self


class _WebSearchConfig(_StrictModel):
    name: Literal["web_search"]
    group: Literal["web_access"]
    use: Literal["workmate_runtime.web_providers:SearxngSearchProvider"]
    base_url_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    max_results: int = Field(default=8, ge=1, le=20)


class _WebFetchConfig(_StrictModel):
    name: Literal["web_fetch"]
    group: Literal["web_access"]
    use: Literal["workmate_runtime.web_providers:Crawl4AIFetchProvider"]
    base_url_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    token_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z_][A-Z0-9_]*$",
    )
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    extract_mode: Literal["fit", "raw"] = "fit"


_WebToolConfig = Annotated[
    Union[_WebSearchConfig, _WebFetchConfig],
    Field(discriminator="name"),
]


class _WebLimitsConfig(_StrictModel):
    max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    max_extracted_chars: int = Field(default=50_000, gt=0)


class _WebConfig(_StrictModel):
    enabled: bool = False
    tools: list[_WebToolConfig] = Field(default_factory=list)
    limits: _WebLimitsConfig = Field(default_factory=_WebLimitsConfig)

    @model_validator(mode="after")
    def validate_unique_tools(self):
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("product.web tools must have unique names")
        if self.enabled and not self.tools:
            raise ValueError("product.web requires at least one tool when enabled")
        return self


class _KnowledgeEmbeddingProfile(_ModelProfile):
    dimensions_mode: Literal["auto", "send", "omit"] = "auto"


class _KnowledgeConfig(_StrictModel):
    embedding: _KnowledgeEmbeddingProfile | None = None
    enabled: bool = False
    chunk_size: int = Field(default=512, ge=32, le=8192)
    chunk_overlap: int = Field(default=64, ge=0)
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0, le=100 * 1024 * 1024)
    search_timeout_secs: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("knowledge chunk_overlap must be below chunk_size")
        return self


class _ProductArtifactConfig(BaseModel):
    storage: ArtifactStoragePolicy = Field(default_factory=ArtifactStoragePolicy)
    model_config = ConfigDict(extra="ignore")
    max_file_bytes: int | None = Field(default=None, gt=0)
    max_files_per_run: int | None = Field(default=None, gt=0)


class _ProductConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    models: _ModelsConfig = Field(default_factory=_ModelsConfig)
    web: _WebConfig = Field(default_factory=_WebConfig)
    knowledge: _KnowledgeConfig = Field(default_factory=_KnowledgeConfig)
    artifacts: _ProductArtifactConfig = Field(default_factory=_ProductArtifactConfig)


class _MemoryConfig(_StrictModel):
    enabled: bool = True
    max_instances: int = Field(default=256, gt=0)
    fallback: Literal["bm25", "disabled"] = "bm25"


class _ArtifactConfig(_StrictModel):
    delivery_mode: Literal["snapshot_fallback", "hybrid"] = "hybrid"
    strict_delivery: bool = False


class _SkillHubCNConfig(_StrictModel):
    enabled: bool = False
    base_url: str = Field(min_length=1)
    request_timeout_secs: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)


class _GitHubMCPHubConfig(_StrictModel):
    enabled: bool = False


class _MCPsConfig(_StrictModel):
    github: _GitHubMCPHubConfig = Field(default_factory=_GitHubMCPHubConfig)


class _SkillsConfig(_StrictModel):
    storage: SkillStoragePolicy = Field(default_factory=SkillStoragePolicy)
    discovery_mode: Literal["explicit", "discover", "none"] = "explicit"
    skillhub_cn: _SkillHubCNConfig = Field(
        default_factory=lambda: _SkillHubCNConfig(
            base_url="https://api.skillhub.cn",
        ),
    )


class _RuntimeConfig(BaseModel):
    workspace_lifecycle: WorkspaceLifecyclePolicy = Field(default_factory=WorkspaceLifecyclePolicy)
    sandbox: SandboxSettings = Field(default_factory=BubblewrapSettings)
    worker: WorkerPolicy = Field(default_factory=WorkerPolicy)
    model_config = ConfigDict(extra="ignore")
    memory: _MemoryConfig = Field(default_factory=_MemoryConfig)
    skills: _SkillsConfig = Field(default_factory=_SkillsConfig)
    mcps: _MCPsConfig = Field(default_factory=_MCPsConfig)
    artifacts: _ArtifactConfig = Field(default_factory=_ArtifactConfig)


class _FileConfig(_StrictModel):
    command_queue: CommandQueuePolicy = Field(default_factory=CommandQueuePolicy)
    realtime: RealtimePolicy = Field(default_factory=RealtimePolicy)
    version: Literal[1] = 1
    product: _ProductConfig = Field(default_factory=_ProductConfig)
    workmate_runtime: _RuntimeConfig = Field(default_factory=_RuntimeConfig)


class RuntimeSettings(BaseModel):
    command_queue: CommandQueuePolicy = Field(default_factory=CommandQueuePolicy)
    realtime: RealtimePolicy = Field(default_factory=RealtimePolicy)
    coordination_namespace: str = Field(default='workmate', pattern=r'^[A-Za-z0-9_-]{1,64}$')
    artifact_storage: ArtifactStoragePolicy = Field(default_factory=ArtifactStoragePolicy)
    artifact_s3_endpoint: SkillS3Endpoint = ''
    artifact_s3_bucket: SkillS3Bucket = 'workmate-artifacts'
    artifact_s3_access_key: str = Field(default='', repr=False)
    artifact_s3_secret_key: str = Field(default='', repr=False)

    @property
    def artifact_storage_backend(self):
        return self.artifact_storage.backend

    workspace_lifecycle: WorkspaceLifecyclePolicy = Field(default_factory=WorkspaceLifecyclePolicy)
    sandbox: SandboxSettings = Field(default_factory=BubblewrapSettings)
    skill_storage: SkillStoragePolicy = Field(default_factory=SkillStoragePolicy)
    skill_s3_endpoint: SkillS3Endpoint = ''
    skill_s3_bucket: SkillS3Bucket = 'workmate-skills'
    skill_s3_access_key: str = Field(default='', repr=False)
    skill_s3_secret_key: str = Field(default='', repr=False)
    skill_cache_dir: str = Field(default='./data/skill-cache', min_length=1)
    worker: WorkerPolicy = Field(default_factory=WorkerPolicy)
    artifact_max_file_bytes: int = Field(default=50*1024*1024,gt=0)
    artifact_max_files_per_run: int = Field(default=20,gt=0)
    artifact_strict_delivery: bool = False
    as_database_url: str = (
        "postgresql+asyncpg://workmate:workmate-dev-password@localhost:5432/"
        "workmate_agentscope"
    )
    coordination_redis_url: CoordinationRedisURL = Field(default="", repr=False)
    as_redis_url: str = "redis://localhost:6379/0"
    workspace_basedir: str = "./workspace-data"
    reme_basedir: str = "./reme-data"
    uploaded_skill_basedir: str = "./uploaded-skills"
    reme_enabled: bool = True
    reme_max_instances: int = Field(default=256, gt=0)
    artifact_delivery_mode: Literal["snapshot_fallback", "hybrid"] = "hybrid"
    skillhub_cn_enabled: bool = False
    skillhub_cn_base_url: str = "https://api.skillhub.cn"
    skillhub_cn_request_timeout_secs: float = Field(default=30.0, gt=0)
    skillhub_cn_max_retries: int = Field(default=2, ge=0, le=10)
    github_mcp_hub_enabled: bool = False
    # Shared secret guarding workmate-internal endpoints
    # (/workmate/workspace/capabilities/sync, /workmate/mcp/copy-for-migration).
    # Must match the product layer's WORKMATE_INTERNAL_TOKEN; empty disables
    # those endpoints (they answer 403).
    internal_token: str = ""
    product_api_url: str = "http://localhost:8080"
    knowledge: _KnowledgeConfig = Field(default_factory=_KnowledgeConfig)
    artifacts: _ProductArtifactConfig = Field(default_factory=_ProductArtifactConfig)
    knowledge_qdrant_url: str = ""
    knowledge_qdrant_api_key: str = ""
    knowledge_s3_endpoint: str = ""
    knowledge_s3_bucket: str = ""
    knowledge_s3_access_key: str = ""
    knowledge_s3_secret_key: str = ""
    web_enabled: bool = False
    web_tools: list[dict] = Field(default_factory=list)
    web_max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    web_max_extracted_chars: int = Field(default=50_000, gt=0)

    # All declared profiles (not only the default): conversations pick a
    # model per session, and each profile needs its own synthetic
    # ModelCard / capability resolution. Values are YAML-only; env carries
    # secrets and the referenced base_url, never model identity.
    chat_model_profiles: dict[str, ChatModelProfile] = Field(
        default_factory=dict,
    )
    # Profile used when a conversation has no explicit selection.
    default_chat_profile: str | None = None
    # Knowledge embeddings are an independent profile; never fall back to ReMe.
    knowledge_embedding_model_dimensions_mode: Literal["auto", "send", "omit"] = "auto"
    knowledge_embedding_model_pass_dimensions: StrictBool = True
    knowledge_embedding_model_type: str = ""
    knowledge_embedding_model_name: str = ""
    knowledge_embedding_model_base_url: str | None = None
    knowledge_embedding_model_api_key_env: str = "KNOWLEDGE_EMBEDDING_MODEL_API_KEY"
    knowledge_embedding_model_dimensions: int | None = Field(default=None, gt=0)

    # Embedding model for ReMe memory, YAML-only identity; the API key
    # comes from the profile's api_key_env and CHAT/EMBEDDING base URLs
    # may fall back to their env counterparts.
    embedding_model_type: str = ""
    embedding_model_name: str = ""
    embedding_model_base_url: str | None = None
    embedding_model_api_key_env: str = "EMBEDDING_MODEL_API_KEY"
    embedding_model_dimensions: int | None = Field(default=None, gt=0)


def _config_path() -> Path | None:
    explicit = os.getenv("WORKMATE_CONFIG_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"WORKMATE config file not found: {path}")
        return path
    candidates = (
        Path.cwd() / "config" / "workmate.yaml",
        Path(__file__).resolve().parents[2] / "config" / "workmate.yaml",
    )
    return next((path for path in candidates if path.is_file()), None)


def _load_file() -> _FileConfig:
    path = _config_path()
    if path is None:
        return _FileConfig()
    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"WORKMATE config root must be a mapping: {path}")
    return _FileConfig.model_validate(raw)


def _nonempty_env(name: str, fallback: str) -> str:
    return os.getenv(name, "").strip() or fallback


def _env_bool(name: str, fallback: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return fallback
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _env_int(name: str, fallback: int | None) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else fallback


def _agent_scope_input_types(media: _MediaProfile) -> list[str]:
    mapping = {
        "text": "text/plain",
        "image": "image/*",
        "audio": "audio/*",
        "video": "video/*",
    }
    values = [mapping[value] for value in media.model_inputs]
    if media.pdf_strategy == "native":
        values.append("application/pdf")
    return values


def load_runtime_settings() -> RuntimeSettings:
    """Load one immutable startup snapshot; callers decide whether to cache it.

    Model identity (type/name/capabilities) comes exclusively from
    workmate.yaml profiles — the environment never overrides it. Env only
    carries secrets (each profile's ``api_key_env``) and gateway addresses
    through the environment variables named by ``base_url_env``.
    """
    file_config = _load_file()
    memory = file_config.workmate_runtime.memory
    artifacts = file_config.workmate_runtime.artifacts
    skillhub_cn = file_config.workmate_runtime.skills.skillhub_cn
    github_mcp_hub = file_config.workmate_runtime.mcps.github
    models = file_config.product.models
    web = file_config.product.web
    embedding = models.embedding

    profiles = {
        name: ChatModelProfile(
            name=name,
            model_type=profile.model_type.lower(),
            model=profile.model,
            # Env carries only the gateway address (base_url_env).
            base_url=(
                os.getenv(profile.base_url_env) if profile.base_url_env else None
            ),
            base_url_env=profile.base_url_env,
            api_key_env=profile.api_key_env,
            media_profile=profile.media_profile,
            model_input_types=_agent_scope_input_types(
                models.media_profiles[profile.media_profile],
            ),
            pdf_strategy=(models.media_profiles[profile.media_profile].pdf_strategy),
            supports_thinking=profile.supports_thinking,
            context_size=profile.context_size,
            max_output_tokens=profile.max_output_tokens,
        )
        for name, profile in models.profiles.items()
    }

    if web.enabled:
        missing = [
            tool.base_url_env
            for tool in web.tools
            if not os.getenv(tool.base_url_env, "").strip()
        ]
        if missing:
            raise ValueError(
                "Missing enabled web provider environment variables: "
                + ", ".join(sorted(set(missing))),
            )
        for tool in web.tools:
            value = os.getenv(tool.base_url_env, "").strip()
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    f"Invalid Web provider URL in {tool.base_url_env}",
                )

    resolved_web_tools = []
    for tool in web.tools:
        item = tool.model_dump(mode="json")
        item["base_url"] = os.getenv(tool.base_url_env, "").strip()
        if isinstance(tool, _WebFetchConfig):
            item["token"] = (
                os.getenv(tool.token_env, "").strip() if tool.token_env else ""
            )
        resolved_web_tools.append(item)

    knowledge = file_config.product.knowledge
    knowledge_embedding = knowledge.embedding
    knowledge_env = {
        key.lower(): os.getenv(key, "").strip()
        for key in (
            "KNOWLEDGE_QDRANT_URL",
            "KNOWLEDGE_QDRANT_API_KEY",
            "KNOWLEDGE_S3_ENDPOINT",
            "KNOWLEDGE_S3_BUCKET",
            "KNOWLEDGE_S3_ACCESS_KEY",
            "KNOWLEDGE_S3_SECRET_KEY",
        )
    }
    knowledge_pass_dimensions = True
    if knowledge.enabled:
        if (
            not knowledge_embedding
            or not knowledge_embedding.dimensions
            or not os.getenv(knowledge_embedding.api_key_env, "").strip()
        ):
            raise ValueError(
                "Knowledge requires configured embedding credentials and dimensions"
            )
        from .embedding_policy import resolve_dimensions_policy

        knowledge_pass_dimensions = resolve_dimensions_policy(
            provider=knowledge_embedding.model_type,
            model=knowledge_embedding.model,
            base_url=(
                os.getenv(knowledge_embedding.base_url_env)
                if knowledge_embedding.base_url_env
                else None
            ),
            dimensions=knowledge_embedding.dimensions,
            mode=knowledge_embedding.dimensions_mode,
        )
        for key, value in knowledge_env.items():
            if not value:
                raise ValueError(
                    f"Missing enabled knowledge environment variable: {key.upper()}"
                )
        for key in ("knowledge_qdrant_url", "knowledge_s3_endpoint"):
            parsed = urlsplit(knowledge_env[key])
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError(f"Invalid knowledge URL in {key.upper()}")

    return RuntimeSettings(
        workspace_lifecycle=WorkspaceLifecyclePolicy(**{key: _env_int("WORKMATE_WORKSPACE_" + key.upper(), value)
            for key, value in file_config.workmate_runtime.workspace_lifecycle.model_dump().items()}),
        sandbox=sandbox_environment(file_config.workmate_runtime.sandbox),
        knowledge=knowledge,
        knowledge_embedding_model_pass_dimensions=knowledge_pass_dimensions,
        knowledge_embedding_model_dimensions_mode=(
            knowledge_embedding.dimensions_mode if knowledge_embedding else "auto"
        ),
        knowledge_embedding_model_type=(
            knowledge_embedding.model_type.lower() if knowledge_embedding else ""
        ),
        knowledge_embedding_model_name=knowledge_embedding.model
        if knowledge_embedding
        else "",
        knowledge_embedding_model_base_url=(
            os.getenv(knowledge_embedding.base_url_env)
            if knowledge_embedding and knowledge_embedding.base_url_env
            else None
        ),
        knowledge_embedding_model_api_key_env=(
            knowledge_embedding.api_key_env
            if knowledge_embedding
            else "KNOWLEDGE_EMBEDDING_MODEL_API_KEY"
        ),
        knowledge_embedding_model_dimensions=(
            knowledge_embedding.dimensions if knowledge_embedding else None
        ),
        **knowledge_env,
        skill_storage=SkillStoragePolicy(**{
            **file_config.workmate_runtime.skills.storage.model_dump(),
            **skill_storage_overrides(os.environ)}),
        skill_s3_endpoint=_nonempty_env('SKILL_S3_ENDPOINT', ''),
        skill_s3_bucket=_nonempty_env('SKILL_S3_BUCKET', 'workmate-skills'),
        skill_s3_access_key=_nonempty_env('SKILL_S3_ACCESS_KEY', ''),
        skill_s3_secret_key=_nonempty_env('SKILL_S3_SECRET_KEY', ''),
        artifact_storage=ArtifactStoragePolicy(**{
            **file_config.product.artifacts.storage.model_dump(),
            **artifact_storage_overrides(os.environ)}),
        artifact_s3_endpoint=_nonempty_env('ARTIFACT_S3_ENDPOINT', ''),
        artifact_s3_bucket=_nonempty_env('ARTIFACT_S3_BUCKET', 'workmate-artifacts'),
        artifact_s3_access_key=_nonempty_env('ARTIFACT_S3_ACCESS_KEY', ''),
        artifact_s3_secret_key=_nonempty_env('ARTIFACT_S3_SECRET_KEY', ''),
        skill_cache_dir=_nonempty_env('WORKMATE_SKILL_CACHE_DIR', './data/skill-cache'),
        as_database_url=_nonempty_env(
            "AS_DATABASE_URL",
            RuntimeSettings.model_fields["as_database_url"].default,
        ),
        command_queue=CommandQueuePolicy(**{**file_config.command_queue.model_dump(), **command_queue_overrides(os.environ)}),
        realtime=RealtimePolicy(**{**file_config.realtime.model_dump(), **realtime_overrides(os.environ)}),
        coordination_namespace=_nonempty_env('WORKMATE_COORDINATION_NAMESPACE','workmate'),
        coordination_redis_url=os.getenv("WORKMATE_COORDINATION_REDIS_URL", ""),
        as_redis_url=_nonempty_env("AS_REDIS_URL", "redis://localhost:6379/0"),
        workspace_basedir=_nonempty_env(
            "WORKMATE_WORKSPACE_BASEDIR",
            "./workspace-data",
        ),
        reme_basedir=_nonempty_env("WORKMATE_REME_BASEDIR", "./reme-data"),
        uploaded_skill_basedir=_nonempty_env(
            "WORKMATE_UPLOADED_SKILL_BASEDIR",
            "./uploaded-skills",
        ),
        reme_enabled=_env_bool("REME_ENABLED", memory.enabled),
        reme_max_instances=_env_int(
            "WORKMATE_REME_MAX_INSTANCES",
            memory.max_instances,
        ),
        worker=WorkerPolicy(**{key:_env_int('WORKMATE_WORKER_'+key.upper(),value)
            for key,value in file_config.workmate_runtime.worker.model_dump().items()}),
        artifact_max_file_bytes=_env_int('ARTIFACT_MAX_FILE_BYTES',file_config.product.artifacts.max_file_bytes or 50*1024*1024),
        artifact_max_files_per_run=_env_int('ARTIFACT_MAX_FILES_PER_RUN',file_config.product.artifacts.max_files_per_run or 20),
        artifact_strict_delivery=_env_bool('ARTIFACT_STRICT_DELIVERY',artifacts.strict_delivery),
        artifact_delivery_mode=_nonempty_env(
            "ARTIFACT_DELIVERY_MODE",
            artifacts.delivery_mode,
        ),
        skillhub_cn_enabled=_env_bool(
            "SKILLHUB_CN_ENABLED",
            skillhub_cn.enabled,
        ),
        skillhub_cn_base_url=_nonempty_env(
            "SKILLHUB_CN_BASE_URL",
            skillhub_cn.base_url,
        ),
        skillhub_cn_request_timeout_secs=float(
            _nonempty_env(
                "SKILLHUB_CN_REQUEST_TIMEOUT_SECS",
                str(skillhub_cn.request_timeout_secs),
            ),
        ),
        skillhub_cn_max_retries=int(
            _nonempty_env(
                "SKILLHUB_CN_MAX_RETRIES",
                str(skillhub_cn.max_retries),
            ),
        ),
        github_mcp_hub_enabled=_env_bool(
            "GITHUB_MCP_HUB_ENABLED",
            github_mcp_hub.enabled,
        ),
        internal_token=_nonempty_env("WORKMATE_INTERNAL_TOKEN", ""),
        product_api_url=_nonempty_env(
            "PRODUCT_API_URL",
            "http://localhost:8080",
        ),
        web_enabled=web.enabled,
        web_tools=resolved_web_tools,
        web_max_response_bytes=web.limits.max_response_bytes,
        web_max_extracted_chars=web.limits.max_extracted_chars,
        chat_model_profiles=profiles,
        default_chat_profile=models.default,
        embedding_model_type=(embedding.model_type.lower() if embedding else ""),
        embedding_model_name=embedding.model if embedding else "",
        embedding_model_base_url=(
            os.getenv(embedding.base_url_env)
            if embedding and embedding.base_url_env
            else None
        ),
        embedding_model_api_key_env=(
            embedding.api_key_env if embedding else "EMBEDDING_MODEL_API_KEY"
        ),
        # Dimensions are YAML-only: identity fields never come from env.
        embedding_model_dimensions=(embedding.dimensions if embedding else None),
    )
