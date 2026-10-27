"""Native API consumes the existing SkillHub policy without Runtime imports."""
import pytest
from app.config import Settings


def test_native_catalog_defaults(monkeypatch, tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    settings = Settings(_env_file=None)
    assert settings.skillhub_cn_enabled is False
    assert settings.skillhub_cn_base_url == 'https://api.skillhub.cn'
    assert settings.skillhub_cn_request_timeout_secs == 30
    assert settings.skillhub_cn_max_retries == 2


@pytest.mark.parametrize('field,yaml_value,env_value,expected', [
    ('enabled', 'true', 'false', False),
    ('base_url', 'https://catalog.example', 'https://override.example', 'https://override.example'),
    ('request_timeout_secs', '12', '8', 8),
    ('max_retries', '3', '1', 1),
])
def test_native_catalog_yaml_and_env(monkeypatch, tmp_path, field, yaml_value, env_value, expected):
    import yaml
    path = tmp_path / 'config.yaml'
    path.write_text(f'version: 1\nworkmate_runtime:\n  skills:\n    skillhub_cn:\n      {field}: {yaml_value}\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    name = 'skillhub_cn_' + field
    monkeypatch.delenv(name.upper(), raising=False)
    assert getattr(Settings(_env_file=None), name) == yaml.safe_load(yaml_value)
    monkeypatch.setenv(name.upper(), env_value)
    assert getattr(Settings(_env_file=None), name) == expected


@pytest.mark.parametrize('field,value', [('enabled','invalid'), ('base_url',' '), ('request_timeout_secs','0'), ('max_retries','11')])
def test_native_catalog_invalid_env(monkeypatch, tmp_path, field, value):
    path = tmp_path / 'config.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    monkeypatch.setenv('SKILLHUB_CN_' + field.upper(), value)
    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.mark.parametrize('field,value', [('base_url', 'invalid'), ('request_timeout_secs', '0'), ('max_retries', '-1')])
def test_native_catalog_invalid_yaml(monkeypatch, tmp_path, field, value):
    path = tmp_path / 'config.yaml'
    path.write_text(f'version: 1\nworkmate_runtime:\n  skills:\n    skillhub_cn:\n      {field}: {value}\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_native_catalog_empty_env_retains_yaml(monkeypatch, tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('version: 1\nworkmate_runtime:\n  skills:\n    skillhub_cn:\n      enabled: true\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    monkeypatch.setenv('SKILLHUB_CN_ENABLED', '')
    assert Settings(_env_file=None).skillhub_cn_enabled is True
