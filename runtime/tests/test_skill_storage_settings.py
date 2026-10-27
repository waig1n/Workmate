"""Shared Skill storage policy and isolated deployment connections."""
import pytest
from workmate_runtime.config import RuntimeSettings, load_runtime_settings


@pytest.fixture
def settings(monkeypatch, tmp_path):
    path = tmp_path/'config.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    for name in ('SKILL_S3_ENDPOINT', 'SKILL_S3_BUCKET', 'SKILL_S3_ACCESS_KEY',
                 'SKILL_S3_SECRET_KEY', 'SKILL_STORAGE_TIMEOUT_SECONDS', 'SKILL_STORAGE_REGION',
                 'WORKMATE_SKILL_CACHE_DIR'):
        monkeypatch.delenv(name, raising=False)
    return load_runtime_settings, path


def test_defaults(settings):
    load, _ = settings
    value = load()
    assert value.skill_storage.timeout_seconds == 30
    assert value.skill_storage.region == 'us-east-1'
    assert value.skill_s3_endpoint == value.skill_s3_access_key == value.skill_s3_secret_key == ''
    assert value.skill_s3_bucket == 'workmate-skills'
    if hasattr(value, 'skill_cache_dir'):
        assert value.skill_cache_dir == './data/skill-cache'


def test_yaml_and_environment_precedence(settings, monkeypatch):
    load, path = settings
    path.write_text('workmate_runtime:\n  skills:\n    storage:\n      timeout_seconds: 12\n      region: eu-west-1\n')
    assert load().skill_storage.timeout_seconds == 12
    assert load().skill_storage.region == 'eu-west-1'
    monkeypatch.setenv('SKILL_STORAGE_TIMEOUT_SECONDS', '8.5')
    monkeypatch.setenv('SKILL_STORAGE_REGION', 'ap-east-1')
    for name, value in {'ENDPOINT': 'https://skills.example', 'BUCKET': 'isolated-skills',
                        'ACCESS_KEY': 'skill-access', 'SECRET_KEY': 'skill-secret'}.items():
        monkeypatch.setenv('SKILL_S3_'+name, value)
    monkeypatch.setenv('WORKMATE_SKILL_CACHE_DIR', '/cache/skills')
    value = load()
    assert value.skill_storage.timeout_seconds == 8.5
    assert value.skill_storage.region == 'ap-east-1'
    assert value.skill_s3_endpoint == 'https://skills.example'
    assert value.skill_s3_bucket == 'isolated-skills'
    assert value.skill_s3_access_key == 'skill-access'
    assert value.skill_s3_secret_key == 'skill-secret'
    assert 'skill-secret' not in repr(value) and 'skill-access' not in repr(value)
    if hasattr(value, 'skill_cache_dir'):
        assert value.skill_cache_dir == '/cache/skills'


@pytest.mark.parametrize('field,value', [('timeout_seconds', 0), ('timeout_seconds', 301),
    ('timeout_seconds', float('inf')), ('timeout_seconds', True), ('region', ' '), ('unknown', 1)])
def test_invalid_yaml(settings, field, value):
    import yaml
    load, path = settings
    path.write_text(yaml.safe_dump({'workmate_runtime': {'skills': {'storage': {field: value}}}}))
    with pytest.raises(ValueError): load()


@pytest.mark.parametrize('name,value', [('SKILL_STORAGE_TIMEOUT_SECONDS', '0'),
    ('SKILL_STORAGE_TIMEOUT_SECONDS', '301'), ('SKILL_STORAGE_TIMEOUT_SECONDS', 'nan'),
    ('SKILL_STORAGE_REGION', ' '), ('SKILL_S3_BUCKET', 'Invalid_Bucket'),
    ('SKILL_S3_BUCKET', 'a..b'), ('SKILL_S3_BUCKET', '127.0.0.1'),
    ('SKILL_S3_BUCKET', 'xn--reserved'), ('SKILL_S3_BUCKET', 'reserved--x-s3'),
    ('SKILL_S3_ENDPOINT', 'ftp://storage'), ('SKILL_S3_ENDPOINT', 'http://user:pass@storage'),
    ('SKILL_S3_ENDPOINT', 'https://storage?token=x'), ('SKILL_S3_ENDPOINT', 'https://storage#fragment')])
def test_invalid_environment(settings, monkeypatch, name, value):
    load, _ = settings
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError): load()


def test_explicit_empty_bucket_is_invalid():
    with pytest.raises(ValueError):
        RuntimeSettings(skill_s3_bucket='')


def test_empty_environment_retains_yaml(settings, monkeypatch):
    load, path = settings
    path.write_text('workmate_runtime:\n  skills:\n    storage:\n      timeout_seconds: 22\n      region: eu-west-1\n')
    monkeypatch.setenv('SKILL_STORAGE_TIMEOUT_SECONDS', '')
    monkeypatch.setenv('SKILL_STORAGE_REGION', '')
    assert load().skill_storage.timeout_seconds == 22
    assert load().skill_storage.region == 'eu-west-1'
