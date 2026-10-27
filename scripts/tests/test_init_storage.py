import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import init_storage as init


def environment():
    return {f'{prefix}_S3_{key}': value
            for prefix in ('SKILL', 'KNOWLEDGE')
            for key, value in dict(ENDPOINT='http://rustfs:9000', BUCKET=prefix.lower(),
                                  ACCESS_KEY=prefix, SECRET_KEY=prefix + '-secret').items()}


@pytest.mark.parametrize('enabled,count', [(False, 1), (True, 2)])
def test_separate_credentials_and_optional_knowledge(monkeypatch, enabled, count):
    env = environment()
    if not enabled:
        env = {k: v for k, v in env.items() if k.startswith('SKILL')}
    clients = []
    def client(settings):
        assert settings.access_key == settings.bucket.upper()
        result = Mock()
        clients.append(result)
        return result
    monkeypatch.setattr(init.bucket, 'make_client', client)
    initialize = Mock()
    monkeypatch.setattr(init.bucket, 'initialize', initialize)
    init.initialize_storage(env, enabled)
    assert initialize.call_count == count
    for item in clients:
        item.close.assert_called_once()


def test_external_buckets_are_never_modified(monkeypatch):
    env = environment()
    for prefix in ('SKILL', 'KNOWLEDGE'):
        env[prefix + '_S3_ENDPOINT'] = 'https://external.example'
    client = Mock()
    monkeypatch.setattr(init.bucket, 'make_client', client)
    init.initialize_storage(env, True)
    client.assert_not_called()


def test_failure_closes_client_and_stops(monkeypatch):
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(init.bucket, 'make_client', factory)
    monkeypatch.setattr(init.bucket, 'initialize', Mock(side_effect=init.bucket.InitializationError('AccessDenied')))
    with pytest.raises(init.bucket.InitializationError):
        init.initialize_storage(environment(), True)
    client.close.assert_called_once()
    factory.assert_called_once()


def test_invalid_or_overlapping_settings_fail_before_changes(monkeypatch):
    env = environment()
    env['KNOWLEDGE_S3_BUCKET'] = env['SKILL_S3_BUCKET']
    factory = Mock()
    monkeypatch.setattr(init.bucket, 'make_client', factory)
    with pytest.raises(init.bucket.InitializationError, match='overlap'):
        init.initialize_storage(env, True)
    factory.assert_not_called()


@pytest.mark.parametrize('value,expected', [('true', True), ('false', False)])
def test_yaml_enablement(tmp_path, value, expected):
    path = tmp_path / 'config.yaml'
    path.write_text('product:\n  knowledge:\n    enabled: ' + value)
    assert init.knowledge_enabled(path) is expected


def test_invalid_configuration_exits_nonzero_without_secrets(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config.yaml'
    path.write_text('product:\n  knowledge:\n    enabled: secret-invalid-value')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    assert init.main() == 1
    assert 'secret-invalid-value' not in capsys.readouterr().err


def test_artifact_bucket_is_optional_and_separate(monkeypatch):
    env=environment()
    env.update({f'ARTIFACT_S3_{k}':v for k,v in dict(ENDPOINT='http://rustfs:9000',BUCKET='artifacts',ACCESS_KEY='ARTIFACTS',SECRET_KEY='secret').items()})
    client=Mock();monkeypatch.setattr(init.bucket,'make_client',Mock(return_value=client))
    initialize=Mock();monkeypatch.setattr(init.bucket,'initialize',initialize)
    init.initialize_storage(env,True,artifacts=True)
    assert initialize.call_count==3
    env['ARTIFACT_S3_BUCKET']=env['SKILL_S3_BUCKET']
    with pytest.raises(init.bucket.InitializationError,match='overlap'):
        init.initialize_storage(env,True,artifacts=True)


@pytest.mark.parametrize('enabled,expected', [('true', True), ('false', False)])
def test_tool_content_enables_private_bucket_with_local_artifacts(tmp_path,monkeypatch,enabled,expected):
    path=tmp_path/'config.yaml'
    path.write_text('product:\n  artifacts:\n    storage:\n      backend: local\n      tool_content_enabled: '+enabled)
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    monkeypatch.delenv('ARTIFACT_STORAGE_BACKEND',raising=False)
    monkeypatch.delenv('ARTIFACT_STORAGE_TOOL_CONTENT_ENABLED',raising=False)
    initialize=Mock()
    monkeypatch.setattr(init,'initialize_storage',initialize)
    assert init.main() == 0
    assert initialize.call_args.kwargs['artifacts'] is expected
