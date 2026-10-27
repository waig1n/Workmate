"""Repository model capability configuration contracts."""
import pytest


@pytest.fixture(autouse=True)
def repository_environment(monkeypatch):
    # Configuration parsing needs explicit provider identity, never developer secrets.
    values = {
        'WEB_FETCH_BASE_URL': 'http://fetch.test',
        'WEB_SEARCH_BASE_URL': 'http://search.test',
        'KNOWLEDGE_EMBEDDING_MODEL_API_KEY': 'test',
        'KNOWLEDGE_EMBEDDING_MODEL_BASE_URL': 'https://api.siliconflow.cn/v1',
        'KNOWLEDGE_QDRANT_URL': 'http://qdrant.test',
        'KNOWLEDGE_QDRANT_API_KEY': 'test',
        'KNOWLEDGE_S3_ENDPOINT': 'http://s3.test',
        'KNOWLEDGE_S3_BUCKET': 'test',
        'KNOWLEDGE_S3_ACCESS_KEY': 'test',
        'KNOWLEDGE_S3_SECRET_KEY': 'test',
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_repo_workmate_yaml_capabilities_parse_on_runtime():
    """Contract: the checked-in workmate.yaml must parse on the Runtime
    plane (backend has a mirror test) and yield the declared capability
    values — schema drift on either side fails one of the two tests."""
    import os
    from pathlib import Path

    import yaml as yaml_lib

    from workmate_runtime.config import load_runtime_settings

    repo_config = (
        Path(__file__).resolve().parents[2] / "config" / "workmate.yaml"
    )
    profile = yaml_lib.safe_load(repo_config.read_text(encoding="utf-8"))[
        "product"
    ]["models"]

    old = os.environ.get("WORKMATE_CONFIG_FILE")
    os.environ["WORKMATE_CONFIG_FILE"] = str(repo_config)
    try:
        settings = load_runtime_settings()
    finally:
        if old is None:
            os.environ.pop("WORKMATE_CONFIG_FILE", None)
        else:
            os.environ["WORKMATE_CONFIG_FILE"] = old

    declared = profile["profiles"][profile["default"]]
    assert settings.default_chat_profile == profile["default"]
    resolved = settings.chat_model_profiles[profile["default"]]
    assert resolved.model == declared["model"]
    assert resolved.media_profile == declared["media_profile"]
    media = profile["media_profiles"][resolved.media_profile]
    expected = [
        {"text": "text/plain", "image": "image/*",
         "audio": "audio/*", "video": "video/*"}[value]
        for value in media["model_inputs"]
    ]
    if media["pdf_strategy"] == "native":
        expected.append("application/pdf")
    assert resolved.model_input_types == expected
    assert resolved.supports_thinking == declared["supports_thinking"]
    assert resolved.context_size == declared["context_size"]
    assert resolved.max_output_tokens == declared["max_output_tokens"]


def test_repo_kimi_profile_does_not_forward_pdf_as_native_file():
    """Regression: SiliconFlow Kimi accepts text/image_url, not file."""
    import os
    from pathlib import Path

    from workmate_runtime.config import load_runtime_settings

    repo_config = (
        Path(__file__).resolve().parents[2] / "config" / "workmate.yaml"
    )
    old = os.environ.get("WORKMATE_CONFIG_FILE")
    os.environ["WORKMATE_CONFIG_FILE"] = str(repo_config)
    try:
        profile = load_runtime_settings().chat_model_profiles["kimi-2.6"]
    finally:
        if old is None:
            os.environ.pop("WORKMATE_CONFIG_FILE", None)
        else:
            os.environ["WORKMATE_CONFIG_FILE"] = old

    assert profile.model_input_types == ["text/plain", "image/*"]
    assert "application/pdf" not in profile.model_input_types
