"""Coordination connections are deployment inputs, never YAML policy."""
import pytest
from pydantic import ValidationError
from workmate_runtime.config import RuntimeSettings, load_runtime_settings

def settings(**kwargs):
    return RuntimeSettings(**kwargs) if kwargs else load_runtime_settings()

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    path = tmp_path / 'workmate.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    monkeypatch.delenv('WORKMATE_COORDINATION_REDIS_URL', raising=False)
    return path


def test_default_disabled_and_unrelated_yaml_preserved(isolated):
    assert settings().coordination_redis_url == ''
    isolated.write_text('version: 1\nworkmate_runtime:\n  worker:\n    capacity: 8\n')
    assert settings().coordination_redis_url == ''


@pytest.mark.parametrize('url', ['', 'redis://redis:6379/0', 'rediss://user:secret@example.com:6380/1', 'redis://[::1]:6379/0'])
def test_environment_connection(monkeypatch, url):
    monkeypatch.setenv('WORKMATE_COORDINATION_REDIS_URL', url)
    assert settings().coordination_redis_url == url
    assert 'secret@' not in repr(settings())


@pytest.mark.parametrize('url', ['http://redis:6379', 'redis://', 'redis:///0', 'redis://host:0/0', 'redis://host:65536/0', 'redis://host:abc/0', 'redis://host/-1', 'redis://host/notdb', 'redis://host/0#fragment', 'redis://bad host/0', ' redis://host/0'])
def test_invalid_environment_fails(monkeypatch, url):
    monkeypatch.setenv('WORKMATE_COORDINATION_REDIS_URL', url)
    with pytest.raises(ValidationError):
        settings()


@pytest.mark.parametrize('value', [None, True, 12])
def test_invalid_explicit_type(value):
    with pytest.raises(ValidationError):
        settings(coordination_redis_url=value)
