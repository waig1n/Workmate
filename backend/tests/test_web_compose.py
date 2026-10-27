"""Static deployment contracts; no Docker/network required."""
from pathlib import Path

import pytest

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_web_services_are_opt_in_and_not_published():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ("searxng", "crawl4ai"):
        assert services[name]["profiles"] == ["web-tool"]
        assert not services[name].get("ports")
    assert "SEARXNG_SECRET:?" in services["searxng"]["command"][0]
    assert services["crawl4ai"]["shm_size"] == "1gb"


@pytest.mark.parametrize('relative', ['config/searxng/settings.yml'])
def test_search_allowlist_cannot_be_extended_by_engine_overrides(relative):
    settings = yaml.safe_load((ROOT / relative).read_text())
    names = settings["use_default_settings"]["engines"]["keep_only"]
    allowed = set(names)
    assert allowed == {"baidu", "baidu images", "360search", "sogou", "sogou images",
                       "bing", "bing images", "bing news"}
    assert len(names) == len(allowed)
    assert len(settings["engines"]) == len({engine["name"] for engine in settings["engines"]})
    assert {engine["name"] for engine in settings["engines"]} <= allowed
    assert settings["search"]["formats"] == ["json", "html"]
    assert "secret_key" not in settings["server"]
