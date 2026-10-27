import textwrap

import pytest

from workmate_runtime.config import load_runtime_settings


def _write_config(tmp_path, web: str):
    path = tmp_path / "workmate.yaml"
    path.write_text(
        "version: 1\nproduct:\n  web:\n" + textwrap.indent(
            textwrap.dedent(web).strip() + "\n",
            "    ",
        ),
        encoding="utf-8",
    )
    return path


def test_disabled_web_does_not_require_provider_environment(monkeypatch, tmp_path):
    path = _write_config(tmp_path, "enabled: false")
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv("WEB_SEARCH_BASE_URL", raising=False)

    settings = load_runtime_settings()

    assert settings.web_enabled is False
    assert settings.web_tools == []


def test_enabled_web_resolves_provider_environment(monkeypatch, tmp_path):
    path = _write_config(
        tmp_path,
        """
        enabled: true
        tools:
          - name: web_search
            group: web_access
            use: workmate_runtime.web_providers:SearxngSearchProvider
            base_url_env: WEB_SEARCH_BASE_URL
            max_results: 4
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("WEB_SEARCH_BASE_URL", "https://search.example.test")

    settings = load_runtime_settings()

    assert settings.web_enabled is True
    assert settings.web_tools[0]["base_url"] == "https://search.example.test"
    assert settings.web_tools[0]["max_results"] == 4


@pytest.mark.parametrize(
    ("web", "message"),
    [
        ("enabled: true\ntools: []", "at least one tool"),
        (
            """
            enabled: true
            tools:
              - name: web_search
                group: web_access
                use: workmate_runtime.web_providers:SearxngSearchProvider
                base_url_env: WEB_SEARCH_BASE_URL
              - name: web_search
                group: web_access
                use: workmate_runtime.web_providers:SearxngSearchProvider
                base_url_env: WEB_SEARCH_BASE_URL
            """,
            "unique",
        ),
    ],
)
def test_invalid_web_declarations_fail(monkeypatch, tmp_path, web, message):
    path = _write_config(tmp_path, web)
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("WEB_SEARCH_BASE_URL", "https://search.example.test")

    with pytest.raises(Exception, match=message):
        load_runtime_settings()


def test_enabled_web_rejects_missing_or_invalid_provider_url(
    monkeypatch,
    tmp_path,
):
    path = _write_config(
        tmp_path,
        """
        enabled: true
        tools:
          - name: web_search
            group: web_access
            use: workmate_runtime.web_providers:SearxngSearchProvider
            base_url_env: WEB_SEARCH_BASE_URL
        """,
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.delenv("WEB_SEARCH_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="WEB_SEARCH_BASE_URL"):
        load_runtime_settings()

    monkeypatch.setenv("WEB_SEARCH_BASE_URL", "file:///etc/passwd")
    with pytest.raises(ValueError, match="Invalid Web provider URL"):
        load_runtime_settings()
