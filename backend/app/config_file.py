# -*- coding: utf-8 -*-
"""Validated non-secret configuration loaded from ``workmate.yaml``."""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from workmate_product.config.command_queue import CommandQueuePolicy
from workmate_product.config.schedules import SchedulePolicy
from workmate_product.config.channels import ChannelPolicy
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.config.worker import WorkerPolicy, WorkspaceLifecyclePolicy
from workmate_product.config.sandbox import SandboxSettings, BubblewrapSettings
from workmate_product.config.skill_storage import SkillStoragePolicy
from workmate_product.config.artifact_storage import ArtifactStoragePolicy
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import PydanticBaseSettingsSource


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AuthFileConfig(_StrictModel):
    algorithm: str | None = None
    expire_hours: int | None = Field(default=None, gt=0)
    registration_mode: Literal["bootstrap_only", "closed", "open"] | None = None


class RuntimeFileConfig(_StrictModel):
    request_timeout_secs: float | None = Field(default=None, gt=0)
    session_lock_ttl_secs: int | None = Field(default=None, gt=0)


class ArtifactFileConfig(_StrictModel):
    storage: ArtifactStoragePolicy = Field(default_factory=ArtifactStoragePolicy)
    max_file_bytes: int | None = Field(default=None, gt=0)
    max_files_per_run: int | None = Field(default=None, gt=0)
    preview_extensions: list[str] | None = None
    force_download_extensions: list[str] | None = None


ModelInput = Literal["text", "image", "audio", "video"]
PdfStrategy = Literal["extract_text", "native"]


class MediaProfileFileConfig(_StrictModel):
    """A reusable, operator-facing media capability preset."""

    model_inputs: list[ModelInput] = Field(default_factory=lambda: ["text"])
    pdf_strategy: PdfStrategy = "extract_text"

    @model_validator(mode="after")
    def validate_model_inputs(self):
        if "text" not in self.model_inputs:
            raise ValueError("media profile model_inputs must include text")
        if len(self.model_inputs) != len(set(self.model_inputs)):
            raise ValueError("media profile model_inputs must be unique")
        return self


def _default_media_profiles() -> dict[str, MediaProfileFileConfig]:
    return {"text": MediaProfileFileConfig()}


class ModelProfileFileConfig(_StrictModel):
    model_type: Literal["openai", "dashscope"] = Field(alias="type")
    model: str
    # Name of the env var holding the gateway URL — the URL itself never
    # lives in this file. Null/empty means the provider SDK default.
    base_url_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z_][A-Z0-9_]*$",
    )
    api_key_env: str = Field(
        default="CHAT_MODEL_API_KEY",
        pattern=r"^[A-Z_][A-Z0-9_]*$",
    )
    dimensions: int | None = Field(default=None, gt=0)
    # Reusable capability preset declared under models.media_profiles.
    media_profile: str = Field(
        default="text",
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    supports_thinking: bool = False
    context_size: int = Field(default=32768, gt=0)
    max_output_tokens: int = Field(default=8192, gt=0)


class ModelsFileConfig(_StrictModel):
    default: str | None = None
    media_profiles: dict[str, MediaProfileFileConfig] = Field(
        default_factory=_default_media_profiles,
    )
    profiles: dict[str, ModelProfileFileConfig] = Field(default_factory=dict)
    embedding: ModelProfileFileConfig | None = None

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


class WebSearchFileConfig(_StrictModel):
    name: Literal["web_search"]
    group: Literal["web_access"]
    use: Literal[
        "workmate_runtime.web_providers:SearxngSearchProvider",
    ]
    base_url_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    max_results: int = Field(default=8, ge=1, le=20)


class WebFetchFileConfig(_StrictModel):
    name: Literal["web_fetch"]
    group: Literal["web_access"]
    use: Literal[
        "workmate_runtime.web_providers:Crawl4AIFetchProvider",
    ]
    base_url_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    token_env: str | None = Field(
        default=None, pattern=r"^[A-Z_][A-Z0-9_]*$",
    )
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    extract_mode: Literal["fit", "raw"] = "fit"


WebToolFileConfig = Annotated[
    Union[WebSearchFileConfig, WebFetchFileConfig],
    Field(discriminator="name"),
]


class WebLimitsFileConfig(_StrictModel):
    max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    max_extracted_chars: int = Field(default=50_000, gt=0)


class WebFileConfig(_StrictModel):
    enabled: bool = False
    tools: list[WebToolFileConfig] = Field(default_factory=list)
    limits: WebLimitsFileConfig = Field(default_factory=WebLimitsFileConfig)

    @model_validator(mode="after")
    def validate_unique_tools(self):
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("product.web tools must have unique names")
        if self.enabled and not self.tools:
            raise ValueError("product.web requires at least one tool when enabled")
        return self


class WorkspaceAssistantFileConfig(_StrictModel):
    enabled: bool = True


class PerformanceFileConfig(_StrictModel):
    event_queue_capacity: int = Field(default=1024, gt=0)
    task_max_outstanding: int = Field(default=64, gt=0)
    task_max_outstanding_per_user: int = Field(default=8, gt=0)
    task_max_pending_approval: int = Field(default=256, gt=0)
    task_max_pending_approval_per_user: int = Field(default=32, gt=0)
    approval_timeout_secs: int = Field(default=86400, gt=0)


class TokenPriceFileConfig(_StrictModel):
    input_per_k: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=18, decimal_places=10)
    output_per_k: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=18, decimal_places=10)


class UsageFileConfig(_StrictModel):
    currency: Literal["CNY", "USD", "EUR"] = "CNY"
    prices: dict[str, TokenPriceFileConfig] = Field(default_factory=dict)


class KnowledgeEmbeddingFileConfig(ModelProfileFileConfig):
    dimensions_mode: Literal["auto", "send", "omit"] = "auto"

    # Required reference: never inherit the chat model's credential default.
    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")


class KnowledgeFileConfig(_StrictModel):
    enabled: bool = False
    embedding: KnowledgeEmbeddingFileConfig | None = None
    chunk_size: int = Field(default=512, ge=32, le=8192)
    chunk_overlap: int = Field(default=64, ge=0)
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    search_timeout_secs: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("knowledge chunk_overlap must be less than chunk_size")
        return self


DEFAULT_ORGANIZATION_SKILL_IDS = (
    "academic-paper-review", "agent-bootstrap", "chart-visualization", "code-documentation",
    "consulting-analysis", "data-analysis", "deep-research", "find-skills",
    "frontend-design", "github-deep-research", "newsletter-generation", "ppt-generation",
    "skill-creator", "skill-reviewer", "surprise-me", "systematic-literature-review",
)
SkillIdentifier = Annotated[str, Field(strict=True, pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")]


class BuiltinSkillsFileConfig(_StrictModel):
    organization_use_ids: list[SkillIdentifier] = Field(default_factory=lambda: list(DEFAULT_ORGANIZATION_SKILL_IDS))


class ProductFileConfig(_StrictModel):
    channels: ChannelPolicy = Field(default_factory=ChannelPolicy)
    schedules: SchedulePolicy = Field(default_factory=SchedulePolicy)
    builtin_skills: BuiltinSkillsFileConfig = Field(default_factory=BuiltinSkillsFileConfig)
    knowledge: KnowledgeFileConfig = Field(default_factory=KnowledgeFileConfig)
    usage: UsageFileConfig = Field(default_factory=UsageFileConfig)
    performance: PerformanceFileConfig = Field(default_factory=PerformanceFileConfig)
    auth: AuthFileConfig = Field(default_factory=AuthFileConfig)
    runtime: RuntimeFileConfig = Field(default_factory=RuntimeFileConfig)
    artifacts: ArtifactFileConfig = Field(default_factory=ArtifactFileConfig)
    models: ModelsFileConfig = Field(default_factory=ModelsFileConfig)
    web: WebFileConfig = Field(default_factory=WebFileConfig)
    workspace_assistant: WorkspaceAssistantFileConfig = Field(
        default_factory=WorkspaceAssistantFileConfig,
    )

    @model_validator(mode="after")
    def validate_usage_profiles(self):
        unknown = self.usage.prices.keys() - self.models.profiles.keys()
        if unknown:
            raise ValueError(f"usage prices reference unknown model profiles: {sorted(unknown)}")
        return self


class MemoryFileConfig(_StrictModel):
    enabled: bool = True
    max_instances: int = Field(default=256, gt=0)
    fallback: Literal["bm25", "disabled"] = "bm25"


class SkillHubCNFileConfig(_StrictModel):
    enabled: bool = False
    base_url: str = Field(default="https://api.skillhub.cn", pattern=r"^https?://\S+$")
    request_timeout_secs: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)


class GitHubMCPHubFileConfig(_StrictModel):
    enabled: bool = False


class MCPsFileConfig(_StrictModel):
    github: GitHubMCPHubFileConfig = Field(
        default_factory=GitHubMCPHubFileConfig,
    )


class SkillsFileConfig(_StrictModel):
    storage: SkillStoragePolicy = Field(default_factory=SkillStoragePolicy)
    discovery_mode: Literal["explicit", "discover", "none"] = "explicit"
    skillhub_cn: SkillHubCNFileConfig = Field(
        default_factory=SkillHubCNFileConfig,
    )


class RuntimeArtifactFileConfig(_StrictModel):
    delivery_mode: Literal["snapshot_fallback", "hybrid"] = "hybrid"
    strict_delivery: bool = False


class WorkmateRuntimeFileConfig(_StrictModel):
    workspace_lifecycle: WorkspaceLifecyclePolicy = Field(default_factory=WorkspaceLifecyclePolicy)
    sandbox: SandboxSettings = Field(default_factory=BubblewrapSettings)
    worker: WorkerPolicy = Field(default_factory=WorkerPolicy)
    memory: MemoryFileConfig = Field(default_factory=MemoryFileConfig)
    skills: SkillsFileConfig = Field(default_factory=SkillsFileConfig)
    mcps: MCPsFileConfig = Field(default_factory=MCPsFileConfig)
    artifacts: RuntimeArtifactFileConfig = Field(
        default_factory=RuntimeArtifactFileConfig,
    )


class WorkmateFileConfig(_StrictModel):
    command_queue: CommandQueuePolicy = Field(default_factory=CommandQueuePolicy)
    realtime: RealtimePolicy = Field(default_factory=RealtimePolicy)
    version: Literal[1] = 1
    product: ProductFileConfig = Field(default_factory=ProductFileConfig)
    workmate_runtime: WorkmateRuntimeFileConfig = Field(
        default_factory=WorkmateRuntimeFileConfig,
    )


def resolve_config_file() -> Path | None:
    """Resolve an explicit config path or the repository/container default."""
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


@lru_cache(maxsize=8)
def _load_file_config_path(path_value: str) -> WorkmateFileConfig:
    """Parse one immutable per-process snapshot for a resolved path."""
    path = Path(path_value)
    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"WORKMATE config root must be a mapping: {path}")
    return WorkmateFileConfig.model_validate(raw)


def load_file_config() -> WorkmateFileConfig | None:
    """Load configuration once per path; changes require process restart."""
    path = resolve_config_file()
    if path is None:
        return None
    return _load_file_config_path(str(path.resolve()))


def _extensions_csv(values: list[str] | None) -> str | None:
    if values is None:
        return None
    normalized = [value.strip().lower().lstrip(".") for value in values]
    return ",".join(value for value in normalized if value)


def model_profiles() -> dict[str, ModelProfileFileConfig]:
    """All declared chat model profiles (not only the default).

    Conversations select a model per session; the default profile is the
    fallback for conversations without an explicit selection.
    """
    config = load_file_config()
    if config is None:
        return {}
    return dict(config.product.models.profiles)


def models_file_config() -> ModelsFileConfig:
    """Return the validated model catalog, including media presets."""
    config = load_file_config()
    if config is None:
        return ModelsFileConfig()
    return config.product.models


def default_model_profile_name() -> str | None:
    """The profile name conversations fall back to when unset."""
    config = load_file_config()
    if config is None:
        return None
    return config.product.models.default


def product_settings_values() -> dict[str, object]:
    """Flatten the product YAML section into existing Settings field names."""
    config = load_file_config()
    if config is None:
        return {}
    product = config.product
    values: dict[str, object | None] = {
        **{f'channel_{key}': value for key, value in product.channels.model_dump().items()},
        **{f'schedule_{key}': value for key, value in product.schedules.model_dump().items()},
        'command_queue': config.command_queue.model_dump(),
        'realtime': config.realtime.model_dump(),
        'skill_storage': config.workmate_runtime.skills.storage.model_dump(),
        'artifact_storage': product.artifacts.storage.model_dump(),
        **product.performance.model_dump(),
        "github_mcp_hub_enabled": config.workmate_runtime.mcps.github.enabled,
        **{f"skillhub_cn_{key}": value for key, value in
           config.workmate_runtime.skills.skillhub_cn.model_dump().items()},
        "jwt_algorithm": product.auth.algorithm,
        "jwt_expire_hours": product.auth.expire_hours,
        "registration_mode": product.auth.registration_mode,
        "runtime_timeout_secs": product.runtime.request_timeout_secs,
        "session_lock_ttl_secs": product.runtime.session_lock_ttl_secs,
        "artifact_max_file_bytes": product.artifacts.max_file_bytes,
        "artifact_max_files_per_run": product.artifacts.max_files_per_run,
        "artifact_preview_extensions": _extensions_csv(
            product.artifacts.preview_extensions,
        ),
        "artifact_force_download_extensions": _extensions_csv(
            product.artifacts.force_download_extensions,
        ),
        "artifact_delivery_mode": (
            config.workmate_runtime.artifacts.delivery_mode
        ),
        "artifact_strict_delivery": (
            config.workmate_runtime.artifacts.strict_delivery
        ),
        "workspace_assistant_enabled": product.workspace_assistant.enabled,
        "builtin_skill_organization_use_ids": product.builtin_skills.organization_use_ids,
    }

    # Chat model profiles are read through model_profiles() /
    # default_model_profile_name() — they are not flattened into Settings
    # so environment variables can never override YAML model identity.
    return {key: value for key, value in values.items() if value is not None}


class ConfigFileSettingsSource(PydanticBaseSettingsSource):
    """Pydantic settings source below env/.env and above code defaults."""

    def get_field_value(self, field, field_name):  # pragma: no cover - unused
        value = product_settings_values().get(field_name)
        return value, field_name, False

    def __call__(self) -> dict[str, object]:
        return product_settings_values()
