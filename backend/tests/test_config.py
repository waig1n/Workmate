# -*- coding: utf-8 -*-
"""Configuration source precedence and validation tests."""

import textwrap

import pytest

from app.config import Settings


def test_old_runtime_config_name_is_rejected():
    from app.config_file import WorkmateFileConfig

    with pytest.raises(ValueError, match='agentscope_runtime'):
        WorkmateFileConfig.model_validate({'agentscope_runtime': {}})


def _write_config(tmp_path, content: str):
    path = tmp_path / "workmate.yaml"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


@pytest.mark.parametrize("name,default", [
    ("event_queue_capacity", 1024),
    ("task_max_outstanding", 64),
    ("task_max_outstanding_per_user", 8),
    ("task_max_pending_approval", 256),
    ("task_max_pending_approval_per_user", 32),
    ("approval_timeout_secs", 86400),
])
def test_performance_defaults_and_env(monkeypatch, tmp_path, name, default):
    path = _write_config(tmp_path, "version: 1")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv(name.upper(), raising=False)
    assert getattr(Settings(_env_file=None), name) == default
    monkeypatch.setenv(name.upper(), "12")
    assert getattr(Settings(_env_file=None), name) == 12
    monkeypatch.setenv(name.upper(), "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.mark.parametrize("name", [
    "event_queue_capacity", "task_max_outstanding", "task_max_outstanding_per_user",
    "task_max_pending_approval", "task_max_pending_approval_per_user", "approval_timeout_secs",
])
@pytest.mark.parametrize("value", [7, 0, -1])
def test_performance_yaml(monkeypatch, tmp_path, name, value):
    path = _write_config(tmp_path, f"version: 1\nproduct:\n  performance:\n    {name}: {value}\n")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv(name.upper(), raising=False)
    if value <= 0:
        with pytest.raises(ValueError):
            Settings(_env_file=None)
    else:
        assert getattr(Settings(_env_file=None), name) == value
        monkeypatch.setenv(name.upper(), "9")
        assert getattr(Settings(_env_file=None), name) == 9


def test_yaml_supplies_non_secret_product_settings(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          auth:
            algorithm: HS512
            expire_hours: 12
          runtime:
            request_timeout_secs: 45
            session_lock_ttl_secs: 900
          artifacts:
            max_file_bytes: 1024
            max_files_per_run: 3
            preview_extensions: [md, json]
            force_download_extensions: [html, svg]
          models:
            default: primary
            media_profiles:
              vision-documents:
                model_inputs: [text, image]
                pdf_strategy: extract_text
            profiles:
              primary:
                type: openai
                model: configured-model
                base_url_env: MODEL_BASE_URL
                api_key_env: MODEL_PRIMARY_API_KEY
        workmate_runtime:
          memory:
            enabled: false
            max_instances: 8
          artifacts:
            delivery_mode: hybrid
            strict_delivery: true
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("MODEL_PRIMARY_API_KEY", "from-profile-secret")
    for name in (
        "JWT_ALGORITHM",
        "JWT_EXPIRE_HOURS",
        "RUNTIME_TIMEOUT_SECS",
        "SESSION_LOCK_TTL_SECS",
        "ARTIFACT_MAX_FILE_BYTES",
        "ARTIFACT_MAX_FILES_PER_RUN",
        "ARTIFACT_PREVIEW_EXTENSIONS",
        "ARTIFACT_FORCE_DOWNLOAD_EXTENSIONS",
        "CHAT_MODEL_TYPE",
        "CHAT_MODEL_NAME",
        "CHAT_MODEL_API_KEY",
        "CHAT_MODEL_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.jwt_algorithm == "HS512"
    assert settings.jwt_expire_hours == 12
    assert settings.runtime_timeout_secs == 45
    assert settings.session_lock_ttl_secs == 900
    assert settings.artifact_max_file_bytes == 1024
    assert settings.artifact_max_files_per_run == 3
    assert settings.artifact_preview_extensions == "md,json"
    assert settings.artifact_force_download_extensions == "html,svg"
    assert settings.artifact_delivery_mode == "hybrid"
    assert settings.artifact_strict_delivery is True


def test_environment_overrides_yaml(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          artifacts:
            max_files_per_run: 20
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("ARTIFACT_MAX_FILES_PER_RUN", "7")

    assert Settings(_env_file=None).artifact_max_files_per_run == 7


def test_runtime_internal_token_from_environment(monkeypatch, tmp_path):
    # runtime_internal_token has no YAML representation (secrets stay in env);
    # product-api reads RUNTIME_INTERNAL_TOKEN, not Runtime's
    # WORKMATE_INTERNAL_TOKEN; the default is empty.
    path = _write_config(tmp_path, "version: 1")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv("RUNTIME_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("WORKMATE_INTERNAL_TOKEN", "runtime-side-token")
    assert Settings(_env_file=None).runtime_internal_token == ""

    monkeypatch.setenv("RUNTIME_INTERNAL_TOKEN", "shared-secret")
    assert Settings(_env_file=None).runtime_internal_token == "shared-secret"


def test_unknown_yaml_keys_fail_fast(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          artifacts:
            max_files_per_run: 20
            misspelled_limit: 5
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="misspelled_limit"):
        Settings(_env_file=None)


def test_missing_explicit_config_file_fails(monkeypatch, tmp_path):
    missing = tmp_path / "missing.yaml"
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(missing))

    with pytest.raises(Exception, match="missing.yaml"):
        Settings(_env_file=None)


def test_config_is_an_immutable_process_snapshot(monkeypatch, tmp_path):
    """A live bind-mount edit cannot change a running process's schema."""
    path = _write_config(tmp_path, "version: 1")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    from app.config_file import load_file_config

    first = load_file_config()
    path.write_text(
        "version: 1\nproduct:\n  unknown_live_field: true\n",
        encoding="utf-8",
    )

    assert load_file_config() is first


def test_missing_default_model_profile_fails(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          models:
            default: missing-profile
            profiles: {}
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="missing-profile"):
        Settings(_env_file=None)


def test_model_capability_fields_load_from_profile(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
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
                model: configured-model
                api_key_env: MODEL_PRIMARY_API_KEY
                media_profile: vision-documents
                supports_thinking: true
                context_size: 131072
                max_output_tokens: 16384
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("MODEL_PRIMARY_API_KEY", "secret")
    for name in (
        "CHAT_MODEL_TYPE", "CHAT_MODEL_NAME",
        "CHAT_MODEL_API_KEY", "CHAT_MODEL_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    from app.config_file import load_file_config

    models = load_file_config().product.models
    profile = models.profiles["primary"]

    assert profile.media_profile == "vision-documents"
    media = models.media_profiles[profile.media_profile]
    assert media.model_inputs == ["text", "image"]
    assert media.pdf_strategy == "extract_text"
    assert profile.supports_thinking is True
    assert profile.context_size == 131072
    assert profile.max_output_tokens == 16384


def test_model_capability_fields_have_conservative_defaults(
    monkeypatch, tmp_path,
):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          models:
            default: primary
            profiles:
              primary:
                type: openai
                model: configured-model
                api_key_env: MODEL_PRIMARY_API_KEY
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    from app.config_file import load_file_config

    profile = load_file_config().product.models.profiles["primary"]

    # The product layer parses (and must accept) the capability fields
    # the Runtime consumes; defaults match the Runtime schema so one
    # workmate.yaml means the same thing on both planes.
    assert profile.media_profile == "text"
    assert profile.supports_thinking is False
    assert profile.context_size == 32768
    assert profile.max_output_tokens == 8192


def test_model_capability_fields_reject_invalid_values(
    monkeypatch, tmp_path,
):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          models:
            default: primary
            profiles:
              primary:
                type: openai
                model: configured-model
                api_key_env: MODEL_PRIMARY_API_KEY
                context_size: 0
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="context_size"):
        Settings(_env_file=None)


def test_repo_workmate_yaml_capabilities_parse_on_backend(monkeypatch):
    """Contract: the checked-in workmate.yaml must parse on the product
    plane (Runtime has a mirror test) with the declared capability
    values — schema drift on either side fails one of the two tests."""
    from pathlib import Path

    import yaml

    from app.config_file import load_file_config

    repo_config = (
        Path(__file__).resolve().parents[2] / "config" / "workmate.yaml"
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(repo_config))

    models = yaml.safe_load(repo_config.read_text(encoding="utf-8"))[
        "product"
    ]["models"]
    declared = models["profiles"][models["default"]]

    parsed_config = load_file_config()
    parsed_models = parsed_config.product.models
    profile = parsed_models.profiles[models["default"]]

    assert parsed_config.workmate_runtime.mcps.github.enabled is False

    assert profile.media_profile == declared["media_profile"]
    declared_media = models["media_profiles"][profile.media_profile]
    parsed_media = parsed_models.media_profiles[profile.media_profile]
    assert parsed_media.model_inputs == declared_media["model_inputs"]
    assert parsed_media.pdf_strategy == declared_media["pdf_strategy"]
    assert profile.supports_thinking == declared["supports_thinking"]
    assert profile.context_size == declared["context_size"]
    assert profile.max_output_tokens == declared["max_output_tokens"]


def test_unknown_media_profile_fails(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          models:
            default: primary
            profiles:
              primary:
                type: openai
                model: configured-model
                media_profile: missing
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="unknown media profile"):
        Settings(_env_file=None)


def test_openai_video_media_profile_fails(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          models:
            default: primary
            media_profiles:
              video:
                model_inputs: [text, video]
                pdf_strategy: extract_text
            profiles:
              primary:
                type: openai
                model: configured-model
                media_profile: video
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="cannot encode: video"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("media_fields", "error"),
    [
        ("model_inputs: [image]\npdf_strategy: extract_text", "include text"),
        ("model_inputs: [text, text]\npdf_strategy: extract_text", "unique"),
        ("model_inputs: [text]\npdf_strategy: page_images", "pdf_strategy"),
    ],
)
def test_invalid_media_profile_values_fail(
    monkeypatch, tmp_path, media_fields, error,
):
    indented = textwrap.indent(media_fields, " " * 16)
    path = _write_config(
        tmp_path,
        f"""
        version: 1
        product:
          models:
            default: primary
            media_profiles:
              invalid:
{indented}
            profiles:
              primary:
                type: openai
                model: configured-model
                media_profile: invalid
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match=error):
        Settings(_env_file=None)


def test_yaml_supplies_registration_mode(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          auth:
            registration_mode: closed
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv("REGISTRATION_MODE", raising=False)

    assert Settings(_env_file=None).registration_mode == "closed"


def test_registration_mode_defaults_to_bootstrap_only(monkeypatch, tmp_path):
    path = _write_config(tmp_path, "version: 1")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv("REGISTRATION_MODE", raising=False)

    assert Settings(_env_file=None).registration_mode == "bootstrap_only"


def test_registration_mode_env_overrides_yaml(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          auth:
            registration_mode: closed
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("REGISTRATION_MODE", "open")

    assert Settings(_env_file=None).registration_mode == "open"


def test_registration_mode_invalid_value_fails(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        version: 1
        product:
          auth:
            registration_mode: sideways
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv("REGISTRATION_MODE", raising=False)

    with pytest.raises(Exception, match="registration_mode"):
        Settings(_env_file=None)


def test_unsupported_provider_rejected_at_startup(monkeypatch, tmp_path):
    """P1-5: an unsupported provider fails during config parsing on the
    product plane too (the Runtime has a mirror test)."""
    path = _write_config(
        tmp_path,
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
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))

    with pytest.raises(Exception, match="anthropic"):
        Settings(_env_file=None)


def test_knowledge_embedding_is_independent_and_strict():
    from app.config_file import WorkmateFileConfig, KnowledgeFileConfig

    assert KnowledgeFileConfig().embedding is None
    config = WorkmateFileConfig.model_validate({
        'version': 1,
        'product': {
            'models': {'embedding': {'type': 'openai', 'model': 'memory-model', 'api_key_env': 'MEMORY_KEY', 'dimensions': 1024}},
            'knowledge': {'embedding': {'type': 'dashscope', 'model': 'rag-model', 'api_key_env': 'RAG_KEY', 'dimensions': 768}},
        },
    })
    assert config.product.models.embedding.model == 'memory-model'
    assert config.product.knowledge.embedding.model == 'rag-model'
    with pytest.raises(ValueError):
        KnowledgeFileConfig(embedding={'type': 'openai', 'model': 'rag'})
    for bad in ({'dimensions': 0}, {'api_key_env': 'not-a-variable'}, {'type': 'unknown'}, {'unexpected': True}):
        profile = {'type': 'openai', 'model': 'rag', 'api_key_env': 'RAG_KEY', **bad}
        with pytest.raises(ValueError):
            KnowledgeFileConfig(embedding=profile)


def test_knowledge_embedding_request_dimensions_are_separate_from_vector_size():
    from app.config_file import KnowledgeEmbeddingFileConfig
    args = {'type': 'openai', 'model': 'BAAI/bge-m3', 'api_key_env': 'RAG_KEY', 'dimensions': 1024}
    assert KnowledgeEmbeddingFileConfig(**args).dimensions_mode == "auto"
    profile = KnowledgeEmbeddingFileConfig(**args, dimensions_mode="omit")
    assert profile.dimensions == 1024
    assert profile.dimensions_mode == "omit"
    for value in (True, False, None, "false"):
        with pytest.raises(ValueError, match="Extra inputs"):
            KnowledgeEmbeddingFileConfig(**args, pass_dimensions=value)


def test_knowledge_embedding_dimensions_mode_is_validated():
    from app.config_file import KnowledgeEmbeddingFileConfig
    args = {'type': 'openai', 'model': 'custom', 'api_key_env': 'RAG_KEY', 'dimensions': 1024}
    for mode in ('auto', 'send', 'omit'):
        assert KnowledgeEmbeddingFileConfig(**args, dimensions_mode=mode).dimensions_mode == mode
    for value in ('guess', None, 1):
        with pytest.raises(ValueError):
            KnowledgeEmbeddingFileConfig(**args, dimensions_mode=value)


def test_native_github_mcp_catalog_config_defaults_yaml_env_and_invalid(monkeypatch,tmp_path):
    from app.config_file import _load_file_config_path

    path=_write_config(tmp_path,'version: 1')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    monkeypatch.delenv('GITHUB_MCP_HUB_ENABLED',raising=False)
    assert Settings(_env_file=None).github_mcp_hub_enabled is False
    path.write_text('version: 1\nworkmate_runtime:\n  mcps:\n    github:\n      enabled: true\n')
    _load_file_config_path.cache_clear()
    assert Settings(_env_file=None).github_mcp_hub_enabled is True
    monkeypatch.setenv('GITHUB_MCP_HUB_ENABLED','false')
    assert Settings(_env_file=None).github_mcp_hub_enabled is False
    monkeypatch.setenv('GITHUB_MCP_HUB_ENABLED','invalid')
    with pytest.raises(ValueError): Settings(_env_file=None)
    monkeypatch.delenv('GITHUB_MCP_HUB_ENABLED')
    path.write_text('version: 1\nworkmate_runtime:\n  mcps:\n    github:\n      enabled: invalid\n')
    _load_file_config_path.cache_clear()
    with pytest.raises(ValueError): Settings(_env_file=None)


def test_builtin_default_use_defaults_yaml_and_env(monkeypatch,tmp_path):
    from app.config_file import DEFAULT_ORGANIZATION_SKILL_IDS, _load_file_config_path
    path=_write_config(tmp_path,'version: 1')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    monkeypatch.delenv('BUILTIN_SKILL_ORGANIZATION_USE_IDS',raising=False)
    assert Settings(_env_file=None).builtin_skill_organization_use_ids==list(DEFAULT_ORGANIZATION_SKILL_IDS)
    path.write_text('version: 1\nproduct:\n  builtin_skills:\n    organization_use_ids: [data-analysis]\n')
    _load_file_config_path.cache_clear()
    assert Settings(_env_file=None).builtin_skill_organization_use_ids==['data-analysis']
    monkeypatch.setenv('BUILTIN_SKILL_ORGANIZATION_USE_IDS','["chart-visualization"]')
    assert Settings(_env_file=None).builtin_skill_organization_use_ids==['chart-visualization']
    monkeypatch.setenv('BUILTIN_SKILL_ORGANIZATION_USE_IDS','[]')
    assert Settings(_env_file=None).builtin_skill_organization_use_ids==[]


@pytest.mark.parametrize('value',['null','true','[123]','["../secret"]','[""]'])
def test_builtin_default_use_rejects_invalid_yaml_and_env(monkeypatch,tmp_path,value):
    path=_write_config(tmp_path,'version: 1\nproduct:\n  builtin_skills:\n    organization_use_ids: '+value+'\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    monkeypatch.delenv('BUILTIN_SKILL_ORGANIZATION_USE_IDS',raising=False)
    with pytest.raises(ValueError): Settings(_env_file=None)
    path.write_text('version: 1')
    monkeypatch.setenv('BUILTIN_SKILL_ORGANIZATION_USE_IDS',value)
    with pytest.raises(ValueError): Settings(_env_file=None)


def test_builtin_skill_defaults_match_shipped_catalog():
    from pathlib import Path
    import yaml
    from app.config_file import DEFAULT_ORGANIZATION_SKILL_IDS
    from app.services.workspace_assistant import INITIAL_SKILL_IDS
    from workmate_product.skills.catalog import builtin_root

    shipped = {p.parent.name for p in builtin_root().glob('*/SKILL.md')}
    defaults = list(DEFAULT_ORGANIZATION_SKILL_IDS)
    config = yaml.safe_load((Path(__file__).parents[2] / 'config/workmate.yaml').read_text())
    configured = config['product']['builtin_skills']['organization_use_ids']
    assert set(defaults) == shipped
    assert len(defaults) == len(shipped)
    assert configured == defaults
    assert set(INITIAL_SKILL_IDS) <= shipped
