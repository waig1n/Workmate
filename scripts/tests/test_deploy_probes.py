"""Execute diagnostic scripts with inert settings, files and socket adapters."""

import os
import socket
import sys
from contextlib import nullcontext, asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
from deploy_probes import DEPENDENCIES


@pytest.fixture
def web_probe(monkeypatch, tmp_path):
    module = ModuleType("workmate_runtime.config")
    module.load_runtime_settings = lambda: SimpleNamespace(
        knowledge=SimpleNamespace(enabled=False),
        skill_s3_endpoint='https://skill-storage.example', skill_s3_bucket='workmate-skills',
        skill_s3_access_key='private-skill-access', skill_s3_secret_key='private-skill-secret',
        skill_storage=SimpleNamespace(region='us-east-1'),
    )
    async def head_bucket(**kwargs):
        assert kwargs == {'Bucket': 'workmate-skills'}
    @asynccontextmanager
    async def client(*args, **kwargs):
        assert args == ('s3',)
        assert kwargs['aws_secret_access_key'] == 'private-skill-secret'
        # No write method exists: any accidental mutation fails the test.
        yield SimpleNamespace(head_bucket=head_bucket)
    sdk = ModuleType('aioboto3')
    sdk.Session = lambda: SimpleNamespace(client=client)
    monkeypatch.setitem(sys.modules, 'aioboto3', sdk)
    monkeypatch.setitem(sys.modules, "workmate_runtime", ModuleType("workmate_runtime"))
    monkeypatch.setitem(sys.modules, "workmate_runtime.config", module)
    path = tmp_path / "config.yaml"
    config = {
        "product": {
            "web": {
                "enabled": True,
                "tools": [
                    {"name": "web_search", "base_url_env": "SEARCH_URL"},
                    {"name": "web_fetch", "base_url_env": "FETCH_URL"},
                ],
            }
        }
    }
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("SEARCH_URL", "http://private-search.example:8080")
    monkeypatch.setenv("FETCH_URL", "https://private-fetch.example")
    return path, config


def test_skill_probe_requires_explicit_credentials_before_sdk_access(web_probe, monkeypatch):
    module = sys.modules['workmate_runtime.config']
    value = module.load_runtime_settings()
    value.skill_s3_secret_key = ''
    monkeypatch.setattr(module, 'load_runtime_settings', lambda: value)
    with pytest.raises(RuntimeError, match='Skill storage configuration is missing'):
        execute()


def execute():
    exec(DEPENDENCIES, {"os": os})


def test_web_checks_all_tools_after_first_socket_failure(
    web_probe, monkeypatch, capsys
):
    calls = []

    def connect(address, **kwargs):
        calls.append(address)
        if len(calls) == 1:
            raise ConnectionRefusedError(
                "private-secret http://private-search.example:8080"
            )
        return nullcontext()

    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(RuntimeError):
        execute()
    assert calls == [("private-search.example", 8080), ("private-fetch.example", 443)]
    output = capsys.readouterr().out
    assert "web: web_search (SEARCH_URL) ConnectionRefusedError" in output
    assert "web: web_fetch (FETCH_URL)" in output
    assert "private-secret" not in output
    assert "private-search.example" not in output
    assert "private-fetch.example" not in output


def test_multiple_failures_report_each_tool_then_fail(web_probe, monkeypatch, capsys):
    errors = iter(
        [socket.gaierror("private-dns-detail"), TimeoutError("private-timeout-detail")]
    )

    def connect(*args, **kwargs):
        raise next(errors)

    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(RuntimeError) as failure:
        execute()
    output = capsys.readouterr().out
    assert "web: web_search (SEARCH_URL) gaierror" in output
    assert "web: web_fetch (FETCH_URL) TimeoutError" in output
    assert "private-" not in output
    assert "private-" not in str(failure.value)


def test_missing_environment_does_not_skip_other_tool(web_probe, monkeypatch, capsys):
    monkeypatch.delenv("SEARCH_URL")
    calls = []
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, **kwargs: calls.append(address) or nullcontext(),
    )
    with pytest.raises(RuntimeError):
        execute()
    assert calls == [("private-fetch.example", 443)]
    assert "web: web_search (SEARCH_URL) KeyError" in capsys.readouterr().out


def test_successful_web_probe_and_disabled_mode(web_probe, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, **kwargs: calls.append(address) or nullcontext(),
    )
    execute()
    assert len(calls) == 2
    output = capsys.readouterr().out
    assert "private-search.example" not in output
    path, config = web_probe
    config["product"]["web"]["enabled"] = False
    path.write_text(yaml.safe_dump(config))
    calls.clear()
    execute()
    assert not calls
