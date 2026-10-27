# -*- coding: utf-8 -*-
"""Runtime YAML configuration and environment override tests.

Model identity (profiles, capabilities) is workmate.yaml-only by design:
the environment carries secrets (each profile's ``api_key_env``) and the
base_url fallbacks, never model identity.
"""

import textwrap

import pytest

from workmate_runtime.config import load_runtime_settings


def test_old_runtime_config_name_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / 'workmate.yaml'
    path.write_text('agentscope_runtime: {}\n', encoding='utf-8')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    with pytest.raises(ValueError, match='agentscope_runtime'):
        load_runtime_settings()

_CHAT_ENV_VARS = (
    "CHAT_MODEL_TYPE",
    "CHAT_MODEL_NAME",
    "CHAT_MODEL_API_KEY",
    "CHAT_MODEL_BASE_URL",
    "CHAT_MODEL_SUPPORTS_MULTIMODAL_INPUT",
    "CHAT_MODEL_SUPPORTS_THINKING",
    "CHAT_MODEL_CONTEXT_SIZE",
    "CHAT_MODEL_MAX_OUTPUT_TOKENS",
)
_GENERIC_ENV_VARS = (
    "EMBEDDING_MODEL_TYPE",
    "EMBEDDING_MODEL_NAME",
    "EMBEDDING_MODEL_API_KEY",
    "EMBEDDING_MODEL_BASE_URL",
    "EMBEDDING_MODEL_DIMENSIONS",
    "REME_ENABLED",
    "WORKMATE_REME_MAX_INSTANCES",
    "SKILLHUB_CN_ENABLED",
    "SKILLHUB_CN_BASE_URL",
    "SKILLHUB_CN_REQUEST_TIMEOUT_SECS",
    "SKILLHUB_CN_MAX_RETRIES",
    "GITHUB_MCP_HUB_ENABLED",
)


def test_public_skill_catalog_disabled_by_default(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv("SKILLHUB_CN_ENABLED", raising=False)
    assert load_runtime_settings().skillhub_cn_enabled is False


def test_public_mcp_catalog_disabled_by_default(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv("GITHUB_MCP_HUB_ENABLED", raising=False)
    assert load_runtime_settings().github_mcp_hub_enabled is False


def test_runtime_loads_memory_and_model_profiles(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            product:
              models:
                default: primary
                media_profiles:
                  vision-documents:
                    model_inputs: [text, image]
                    pdf_strategy: extract_text
                profiles:
                  primary:
                    type: openai
                    model: chat-from-yaml
                    base_url_env: PRIMARY_BASE_URL
                    api_key_env: PRIMARY_API_KEY
                    media_profile: vision-documents
                    context_size: 131072
                    max_output_tokens: 16384
                  backup:
                    type: dashscope
                    model: backup-from-yaml
                    api_key_env: BACKUP_API_KEY
                embedding:
                  type: openai
                  model: embed-from-yaml
                  dimensions: 768
                  base_url_env: EMBED_BASE_URL
                  api_key_env: EMBED_API_KEY
            workmate_runtime:
              memory:
                enabled: false
                max_instances: 12
                fallback: bm25
              skills:
                discovery_mode: explicit
                skillhub_cn:
                  enabled: true
                  base_url: https://skills.example.test
                  request_timeout_secs: 9
                  max_retries: 4
              mcps:
                github:
                  enabled: true
              artifacts:
                delivery_mode: hybrid
                strict_delivery: false
            """,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("PRIMARY_API_KEY", "primary-secret")
    monkeypatch.setenv("PRIMARY_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embed.example.test/v1")
    for name in (*_CHAT_ENV_VARS, *_GENERIC_ENV_VARS):
        monkeypatch.delenv(name, raising=False)

    settings = load_runtime_settings()

    assert settings.reme_enabled is False
    assert settings.reme_max_instances == 12
    assert settings.artifact_delivery_mode == "hybrid"
    assert settings.skillhub_cn_enabled is True
    assert settings.skillhub_cn_request_timeout_secs == 9
    assert settings.skillhub_cn_max_retries == 4
    assert settings.github_mcp_hub_enabled is True

    # Model identity comes from YAML only.
    assert settings.default_chat_profile == "primary"
    assert set(settings.chat_model_profiles) == {"primary", "backup"}
    primary = settings.chat_model_profiles["primary"]
    assert primary.model_type == "openai"
    assert primary.model == "chat-from-yaml"
    assert primary.base_url == "https://models.example.test/v1"
    assert primary.api_key_env == "PRIMARY_API_KEY"
    assert primary.media_profile == "vision-documents"
    assert primary.model_input_types == ["text/plain", "image/*"]
    assert primary.pdf_strategy == "extract_text"
    assert primary.context_size == 131072
    assert primary.max_output_tokens == 16384
    backup = settings.chat_model_profiles["backup"]
    assert backup.model_type == "dashscope"
    assert backup.base_url is None

    # Embedding identity is YAML-only as well.
    assert settings.embedding_model_type == "openai"
    assert settings.embedding_model_name == "embed-from-yaml"
    assert settings.embedding_model_dimensions == 768
    assert settings.embedding_model_base_url == ("https://embed.example.test/v1")
    assert settings.embedding_model_api_key_env == "EMBED_API_KEY"


def test_environment_overrides_yaml(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        "version: 1\nworkmate_runtime:\n  memory:\n"
        "    enabled: true\n    max_instances: 12\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("REME_ENABLED", "false")
    monkeypatch.setenv("WORKMATE_REME_MAX_INSTANCES", "4")
    monkeypatch.setenv("SKILLHUB_CN_ENABLED", "false")
    monkeypatch.setenv("SKILLHUB_CN_BASE_URL", "https://proxy.example.test")
    monkeypatch.setenv("GITHUB_MCP_HUB_ENABLED", "true")
    monkeypatch.delenv("WORKMATE_INTERNAL_TOKEN", raising=False)

    settings = load_runtime_settings()

    assert settings.reme_enabled is False
    assert settings.reme_max_instances == 4
    assert settings.skillhub_cn_enabled is False
    assert settings.skillhub_cn_base_url == "https://proxy.example.test"
    assert settings.github_mcp_hub_enabled is True
    assert settings.internal_token == ""


def test_environment_can_disable_yaml_github_mcp_hub(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        "version: 1\nworkmate_runtime:\n  mcps:\n    github:\n      enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("GITHUB_MCP_HUB_ENABLED", "false")

    assert load_runtime_settings().github_mcp_hub_enabled is False


def test_invalid_github_mcp_hub_environment_value_fails(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("GITHUB_MCP_HUB_ENABLED", "sometimes")

    with pytest.raises(ValueError, match="GITHUB_MCP_HUB_ENABLED"):
        load_runtime_settings()


def test_runtime_internal_token_from_environment(monkeypatch, tmp_path):
    # internal_token has no YAML representation (secrets stay in env); the
    # default is empty and WORKMATE_INTERNAL_TOKEN supplies the value.
    path = tmp_path / "workmate.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("WORKMATE_INTERNAL_TOKEN", "shared-secret")

    settings = load_runtime_settings()

    assert settings.internal_token == "shared-secret"


def test_runtime_rejects_missing_default_profile(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        "version: 1\nproduct:\n  models:\n"
        "    default: missing-profile\n    profiles: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="missing-profile"):
        load_runtime_settings()


def test_model_env_never_overrides_yaml_identity(monkeypatch, tmp_path):
    """CHAT_MODEL_* must not bend model identity: profiles are YAML-only
    by design; the env carries the API key and gateway fallback."""
    path = tmp_path / "workmate.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            product:
              models:
                default: primary
                profiles:
                  primary:
                    type: openai
                    model: chat-from-yaml
                    api_key_env: PRIMARY_API_KEY
            """,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("CHAT_MODEL_NAME", "env-only-model")
    monkeypatch.setenv("CHAT_MODEL_TYPE", "dashscope")
    monkeypatch.setenv("CHAT_MODEL_SUPPORTS_MULTIMODAL_INPUT", "true")
    monkeypatch.setenv("CHAT_MODEL_CONTEXT_SIZE", "4096")

    settings = load_runtime_settings()

    profile = settings.chat_model_profiles["primary"]
    assert settings.default_chat_profile == "primary"
    assert profile.model == "chat-from-yaml"
    assert profile.model_type == "openai"
    assert profile.media_profile == "text"
    assert profile.model_input_types == ["text/plain"]
    assert profile.context_size == 32768


def test_base_url_env_unset_yields_none(monkeypatch, tmp_path):
    """A profile without base_url_env (or with the variable unset) gets
    a None base_url — the provider SDK default applies."""
    path = tmp_path / "workmate.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            product:
              models:
                default: primary
                profiles:
                  primary:
                    type: openai
                    model: chat-from-yaml
                    api_key_env: PRIMARY_API_KEY
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv("CHAT_MODEL_BASE_URL", raising=False)

    settings = load_runtime_settings()

    assert settings.chat_model_profiles["primary"].base_url is None


def test_profiles_absent_yields_empty_map(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    settings = load_runtime_settings()

    assert settings.chat_model_profiles == {}
    assert settings.default_chat_profile is None


def test_invalid_profile_capability_fails(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            product:
              models:
                default: primary
                profiles:
                  primary:
                    type: openai
                    model: chat-from-yaml
                    api_key_env: PRIMARY_API_KEY
                    context_size: 0
            """,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="context_size"):
        load_runtime_settings()


def test_runtime_rejects_unknown_media_profile(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            product:
              models:
                default: primary
                profiles:
                  primary:
                    type: openai
                    model: chat-from-yaml
                    api_key_env: PRIMARY_API_KEY
                    media_profile: missing
            """,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="unknown media profile"):
        load_runtime_settings()


def test_runtime_rejects_adapter_media_mismatch(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            product:
              models:
                default: primary
                media_profiles:
                  native-video:
                    model_inputs: [text, video]
                    pdf_strategy: extract_text
                profiles:
                  primary:
                    type: openai
                    model: chat-from-yaml
                    api_key_env: PRIMARY_API_KEY
                    media_profile: native-video
            """,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="cannot encode: video"):
        load_runtime_settings()


def test_dashscope_native_video_profile_is_supported(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            product:
              models:
                default: primary
                media_profiles:
                  native-video:
                    model_inputs: [text, image, video]
                    pdf_strategy: extract_text
                profiles:
                  primary:
                    type: dashscope
                    model: qwen-omni
                    api_key_env: PRIMARY_API_KEY
                    media_profile: native-video
            """,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    profile = load_runtime_settings().chat_model_profiles["primary"]

    assert profile.model_input_types == [
        "text/plain",
        "image/*",
        "video/*",
    ]


def test_runtime_rejects_unknown_pdf_strategy(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            product:
              models:
                default: primary
                media_profiles:
                  invalid:
                    model_inputs: [text]
                    pdf_strategy: page_images
                profiles:
                  primary:
                    type: openai
                    model: chat-from-yaml
                    api_key_env: PRIMARY_API_KEY
                    media_profile: invalid
            """,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="pdf_strategy"):
        load_runtime_settings()


def test_unsupported_provider_rejected_at_startup(monkeypatch, tmp_path):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            product:
              models:
                default: odd
                profiles:
                  odd:
                    type: anthropic
                    model: some-model
                    api_key_env: ODD_API_KEY
            """,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="anthropic"):
        load_runtime_settings()


def test_knowledge_defaults_and_invalid_overlap(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    path.write_text("version: 1\n")
    settings = load_runtime_settings()
    assert settings.knowledge.enabled is False
    assert settings.knowledge.chunk_size == 512
    assert settings.knowledge.max_upload_bytes == 20 * 1024 * 1024
    path.write_text(
        "product:\n  knowledge:\n    chunk_size: 100\n    chunk_overlap: 100\n"
    )
    with pytest.raises(ValueError, match="chunk_overlap"):
        load_runtime_settings()


def test_knowledge_loads_yaml_and_env_and_rejects_missing_credentials(
    monkeypatch, tmp_path
):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    path.write_text("""product:
  knowledge:
    enabled: true
    chunk_size: 300
    chunk_overlap: 30
    search_timeout_secs: 15
    embedding:
      type: openai
      model: embedding
      dimensions_mode: send
      dimensions: 1024
      api_key_env: KB_TEST_KEY
""")
    monkeypatch.delenv("KB_TEST_KEY", raising=False)
    with pytest.raises(ValueError, match="embedding credentials"):
        load_runtime_settings()
    monkeypatch.setenv("KB_TEST_KEY", "test")
    for name, value in {
        "QDRANT_URL": "http://qdrant:6333",
        "QDRANT_API_KEY": "test-key",
        "S3_ENDPOINT": "http://rustfs:9000",
        "S3_BUCKET": "knowledge",
        "S3_ACCESS_KEY": "access",
        "S3_SECRET_KEY": "secret",
    }.items():
        monkeypatch.setenv("KNOWLEDGE_" + name, value)
    settings = load_runtime_settings()
    assert settings.knowledge.chunk_size == 300
    assert settings.knowledge.search_timeout_secs == 15
    assert settings.knowledge_s3_bucket == "knowledge"
    monkeypatch.delenv("KNOWLEDGE_QDRANT_API_KEY")
    with pytest.raises(ValueError, match="KNOWLEDGE_QDRANT_API_KEY"):
        load_runtime_settings()
    monkeypatch.setenv("KNOWLEDGE_QDRANT_API_KEY", "   ")
    with pytest.raises(ValueError, match="KNOWLEDGE_QDRANT_API_KEY"):
        load_runtime_settings()
    monkeypatch.setenv("KNOWLEDGE_QDRANT_API_KEY", "test-key")
    monkeypatch.setenv("KNOWLEDGE_QDRANT_URL", "http://user:secret@host")
    with pytest.raises(ValueError, match="Invalid knowledge URL"):
        load_runtime_settings()


def test_knowledge_embedding_is_independent_from_reme(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    path.write_text("""product:
  models:
    embedding:
      type: openai
      model: reme-model
      dimensions: 768
      base_url_env: REME_URL
      api_key_env: REME_KEY
  knowledge:
    embedding:
      type: dashscope
      model: knowledge-model
      dimensions: 1024
      base_url_env: RAG_URL
      api_key_env: RAG_KEY
""")
    monkeypatch.setenv("REME_URL", "https://reme.example/v1")
    monkeypatch.setenv("RAG_URL", "https://rag.example/v1")
    settings = load_runtime_settings()
    assert settings.embedding_model_name == "reme-model"
    assert settings.embedding_model_dimensions == 768
    assert settings.embedding_model_api_key_env == "REME_KEY"
    assert settings.embedding_model_base_url == "https://reme.example/v1"
    assert settings.knowledge_embedding_model_type == "dashscope"
    assert settings.knowledge_embedding_model_name == "knowledge-model"
    assert settings.knowledge_embedding_model_dimensions == 1024
    assert settings.knowledge_embedding_model_api_key_env == "RAG_KEY"
    assert settings.knowledge_embedding_model_base_url == "https://rag.example/v1"
    monkeypatch.setenv("RAG_URL", "https://rag-new.example/v1")
    changed = load_runtime_settings()
    assert changed.knowledge_embedding_model_base_url == "https://rag-new.example/v1"
    assert changed.embedding_model_base_url == settings.embedding_model_base_url


def test_knowledge_embedding_never_falls_back_to_reme(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    path.write_text("""product:
  models:
    embedding:
      type: openai
      model: reme-model
      dimensions: 768
      api_key_env: REME_KEY
""")
    monkeypatch.setenv("REME_KEY", "configured-reme-only")
    settings = load_runtime_settings()
    assert settings.knowledge_embedding_model_name == ""
    assert settings.knowledge_embedding_model_dimensions is None
    assert (
        settings.knowledge_embedding_model_api_key_env
        == "KNOWLEDGE_EMBEDDING_MODEL_API_KEY"
    )
    path.write_text(path.read_text() + "  knowledge:\n    enabled: true\n")
    with pytest.raises(ValueError, match="embedding credentials"):
        load_runtime_settings()


@pytest.mark.parametrize(
    "field", ["dimensions: 0", "type: unsupported", "unknown: invalid"]
)
def test_knowledge_embedding_invalid_fields_fail(monkeypatch, tmp_path, field):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    profile = "      type: openai\n      model: knowledge-model\n      dimensions: 1024\n      api_key_env: RAG_KEY\n"
    if field.startswith("type:"):
        profile = profile.replace("type: openai", field)
    elif field.startswith("dimensions:"):
        profile = profile.replace("dimensions: 1024", field)
    else:
        profile += "      " + field + "\n"
    path.write_text("product:\n  knowledge:\n    embedding:\n" + profile)
    with pytest.raises(ValueError):
        load_runtime_settings()


def test_enabled_knowledge_requires_its_own_dimensions_and_nonblank_key(
    monkeypatch, tmp_path
):
    import yaml

    path = tmp_path / "config.yaml"
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    profile = {"type": "openai", "model": "knowledge-model", "api_key_env": "RAG_KEY"}
    config = {"product": {"knowledge": {"enabled": True, "embedding": profile}}}
    monkeypatch.setenv("RAG_KEY", "valid-key")
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="embedding credentials and dimensions"):
        load_runtime_settings()
    profile["dimensions"] = 1024
    monkeypatch.setenv("RAG_KEY", "   ")
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="embedding credentials and dimensions"):
        load_runtime_settings()


@pytest.mark.parametrize("value", [True, False, None, "false", 0])
def test_knowledge_rejects_removed_dimension_alias(monkeypatch, tmp_path, value):
    import yaml

    path = tmp_path / "config.yaml"
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    config = {"product": {"knowledge": {"embedding": {
        "type": "openai", "model": "bge", "api_key_env": "RAG_KEY",
        "pass_dimensions": value,
    }}}}
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="Extra inputs"):
        load_runtime_settings()


def test_enabled_dimensions_auto_resolves_endpoint_and_explicit_modes(
    monkeypatch, tmp_path
):
    import yaml

    path = tmp_path / "config.yaml"
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    for name, value in {
        "KNOWLEDGE_QDRANT_URL": "http://qdrant:6333",
        "KNOWLEDGE_QDRANT_API_KEY": "test",
        "KNOWLEDGE_S3_ENDPOINT": "http://rustfs:9000",
        "KNOWLEDGE_S3_BUCKET": "test",
        "KNOWLEDGE_S3_ACCESS_KEY": "test",
        "KNOWLEDGE_S3_SECRET_KEY": "test",
        "RAG_KEY": "test",
        "RAG_URL": "https://api.siliconflow.cn/v1",
    }.items():
        monkeypatch.setenv(name, value)
    profile = {
        "type": "openai",
        "model": "BAAI/bge-m3",
        "dimensions": 1024,
        "api_key_env": "RAG_KEY",
        "base_url_env": "RAG_URL",
    }
    config = {"product": {"knowledge": {"enabled": True, "embedding": profile}}}
    path.write_text(yaml.safe_dump(config))
    assert load_runtime_settings().knowledge_embedding_model_pass_dimensions is False
    assert load_runtime_settings().knowledge_embedding_model_dimensions_mode == "auto"
    monkeypatch.setenv("RAG_URL", "https://unknown.example/v1")
    with pytest.raises(ValueError, match="explicit"):
        load_runtime_settings()
    profile["dimensions_mode"] = "omit"
    path.write_text(yaml.safe_dump(config))
    assert load_runtime_settings().knowledge_embedding_model_pass_dimensions is False
    profile["dimensions_mode"] = "send"
    path.write_text(yaml.safe_dump(config))
    assert load_runtime_settings().knowledge_embedding_model_pass_dimensions is True
    config["product"]["knowledge"]["enabled"] = False
    profile["dimensions_mode"] = "auto"
    path.write_text(yaml.safe_dump(config))
    assert load_runtime_settings().knowledge.enabled is False


def test_artifact_storage_policy_yaml_and_env(monkeypatch, tmp_path):
    path=tmp_path/'objects.yaml'
    path.write_text('product:\n  artifacts:\n    storage:\n      backend: s3\n      region: test-region\n      timeout_seconds: 11\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    config=load_runtime_settings()
    assert config.artifact_storage.backend=='s3'
    assert config.artifact_storage.region=='test-region'
    assert config.artifact_storage.timeout_seconds==11
    monkeypatch.setenv('ARTIFACT_STORAGE_BACKEND','local')
    monkeypatch.setenv('ARTIFACT_STORAGE_TIMEOUT_SECONDS','9')
    monkeypatch.setenv('ARTIFACT_S3_ENDPOINT','http://objects:9000')
    monkeypatch.setenv('ARTIFACT_S3_BUCKET','test-artifacts')
    config=load_runtime_settings()
    assert config.artifact_storage.backend=='local'
    assert config.artifact_storage.timeout_seconds==9
    assert config.artifact_s3_endpoint=='http://objects:9000'
    monkeypatch.setenv('ARTIFACT_STORAGE_BACKEND','invalid')
    with pytest.raises(ValueError):load_runtime_settings()
