# -*- coding: utf-8 -*-
"""Shared model construction and private namespace helpers for Worker memory."""

import hashlib
import os
from typing import TYPE_CHECKING

from agentscope.credential import (
    DashScopeCredential,
    OpenAICredential,
)

from .config import RuntimeSettings, load_runtime_settings

if TYPE_CHECKING:
    from agentscope.embedding import EmbeddingModelBase
    from agentscope.model import ChatModelBase


def build_chat_model_from_profile(
    settings: "RuntimeSettings | None" = None,
) -> "ChatModelBase | None":
    """Build the default-profile chat model for ReMe memory.

    Model identity comes from workmate.yaml (``models.default``); the
    environment only supplies the API key (the profile's ``api_key_env``)
    and the ``CHAT_MODEL_BASE_URL`` gateway fallback. Returns ``None``
    when no default profile or API key is configured, degrading memory.
    """
    settings = settings or load_runtime_settings()
    profile = settings.chat_model_profiles.get(
        settings.default_chat_profile or "",
    )
    if profile is None:
        return None
    api_key = os.getenv(profile.api_key_env, "")
    if not api_key:
        return None

    base_url = profile.base_url or os.getenv("CHAT_MODEL_BASE_URL") or None
    if profile.model_type == "dashscope":
        from agentscope.model import DashScopeChatModel

        return DashScopeChatModel(
            credential=DashScopeCredential(api_key=api_key),
            model=profile.model,
        )
    if profile.model_type == "openai":
        from agentscope.model import OpenAIChatModel

        return OpenAIChatModel(
            credential=OpenAICredential(api_key=api_key, base_url=base_url),
            model=profile.model,
        )
    raise ValueError(f"Unsupported chat model type: {profile.model_type!r}")


def build_embedding_model_from_settings(
    settings: "RuntimeSettings | None" = None,
) -> "EmbeddingModelBase | None":
    """Build the ReMe embedding model from the YAML embedding profile.

    The environment only supplies the API key (the profile's
    ``api_key_env``) and the ``EMBEDDING_MODEL_BASE_URL`` fallback.
    Returns ``None`` when unconfigured, degrading memory to BM25.
    """
    settings = settings or load_runtime_settings()
    if not settings.embedding_model_type or not settings.embedding_model_name:
        return None
    api_key = os.getenv(settings.embedding_model_api_key_env, "")
    if not api_key:
        return None

    model_name = settings.embedding_model_name
    dimensions = settings.embedding_model_dimensions or _default_dimensions(
        model_name,
    )
    base_url = (
        settings.embedding_model_base_url
        or os.getenv("EMBEDDING_MODEL_BASE_URL")
        or None
    )

    if settings.embedding_model_type == "dashscope":
        from agentscope.embedding import DashScopeEmbeddingModel

        return DashScopeEmbeddingModel(
            credential=DashScopeCredential(api_key=api_key),
            model=model_name,
            dimensions=dimensions,
        )
    if settings.embedding_model_type == "openai":
        from agentscope.embedding import OpenAIEmbeddingModel

        return OpenAIEmbeddingModel(
            credential=OpenAICredential(api_key=api_key, base_url=base_url),
            model=model_name,
            dimensions=dimensions,
            # Dimensions are declared in YAML, so always forward them
            # unless the profile leaves them unset for a provider that
            # rejects the parameter.
            pass_dimensions=settings.embedding_model_dimensions is not None,
        )
    raise ValueError(
        f"Unsupported embedding model type: {settings.embedding_model_type!r}",
    )


def _default_dimensions(model_name: str) -> int:
    """Return safe dimensions for common embedding models.

    AgentScope made ``dimensions`` a required constructor argument.
    Providers such as SiliconFlow do not accept the OpenAI ``dimensions``
    request parameter, so it is used for the local contract but only sent to
    the API when the profile explicitly declares dimensions.
    """
    normalized = model_name.lower()
    if "text-embedding-3-large" in normalized:
        return 3072
    if "text-embedding-3-small" in normalized or "ada-002" in normalized:
        return 1536
    # BAAI/bge-m3 and DashScope text-embedding-v4 both default to 1024.
    return 1024


def _namespace_segment(kind: str, value: str) -> str:
    """Return a stable, path-safe namespace segment for an identity."""
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=16).hexdigest()
    return f"{kind}-{digest}"


def reme_workspace_dir(reme_basedir: str, user_id: str, agent_id: str) -> str:
    """Resolve a ReMe directory isolated by product user and agent.

    Agent definitions are org-wide in workmate, so using only ``agent_id``
    would let different users search the same memory collection. Hashing
    the identities also prevents an externally supplied id from escaping
    ``reme_basedir`` through path separators.
    """
    return os.path.join(
        reme_basedir,
        _namespace_segment("user", user_id),
        _namespace_segment("agent", agent_id),
    )
