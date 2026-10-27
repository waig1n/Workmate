"""Sandbox selection is validated before any workspace or SDK is opened."""
import pytest

from workmate_runtime.config import load_runtime_settings


def config(monkeypatch, tmp_path, body='version: 1\n'):
    path = tmp_path / 'workmate.yaml'
    path.write_text(body)
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    monkeypatch.delenv('WORKMATE_SANDBOX_PROVIDER', raising=False)


def test_default_and_explicit_bubblewrap(monkeypatch, tmp_path):
    config(monkeypatch, tmp_path)
    assert load_runtime_settings().sandbox.provider == 'bubblewrap'
    config(monkeypatch, tmp_path,
        'workmate_runtime:\n  sandbox:\n    provider: bubblewrap\n')
    assert load_runtime_settings().sandbox.provider == 'bubblewrap'


def test_environment_selection_is_validated(monkeypatch, tmp_path):
    config(monkeypatch, tmp_path)
    monkeypatch.setenv('WORKMATE_SANDBOX_PROVIDER', 'bubblewrap')
    assert load_runtime_settings().sandbox.provider == 'bubblewrap'
    monkeypatch.setenv('WORKMATE_SANDBOX_PROVIDER', 'local')
    assert load_runtime_settings().sandbox.provider == 'local'
    monkeypatch.setenv('WORKMATE_SANDBOX_PROVIDER', 'unknown')
    with pytest.raises(ValueError):
        load_runtime_settings()


@pytest.mark.parametrize('provider', ['applecontainer', 'unknown', '', None, 1])
def test_unimplemented_or_invalid_provider_fails_closed(monkeypatch, tmp_path, provider):
    import yaml
    config(monkeypatch, tmp_path, yaml.safe_dump(
        {'workmate_runtime': {'sandbox': {'provider': provider}}}))
    with pytest.raises(ValueError):
        load_runtime_settings()


def test_unused_provider_options_are_rejected(monkeypatch, tmp_path):
    config(monkeypatch, tmp_path,
        'workmate_runtime:\n  sandbox:\n    provider: bubblewrap\n    image: ignored\n')
    with pytest.raises(ValueError):
        load_runtime_settings()


def test_factory_uses_explicit_policy_and_rejects_fallback():
    from workmate_runtime.sandbox.factory import create_sandbox_provider
    from workmate_product.config.sandbox import BubblewrapSettings
    assert create_sandbox_provider(BubblewrapSettings()).name == 'bubblewrap'
    assert create_sandbox_provider({'provider': 'local'}).name == 'local'
    with pytest.raises(ValueError):
        create_sandbox_provider({'provider': 'unknown'})


def test_empty_environment_keeps_yaml(monkeypatch, tmp_path):
    config(monkeypatch, tmp_path)
    monkeypatch.setenv('WORKMATE_SANDBOX_PROVIDER', ' ')
    assert load_runtime_settings().sandbox.provider == 'bubblewrap'


def test_compose_passes_selection_to_worker():
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / 'docker-compose.yml').read_text())
    assert 'WORKMATE_SANDBOX_PROVIDER' not in compose['services']['runtime']['environment']


@pytest.mark.parametrize('provider', ['local','bubblewrap','docker','kubernetes','e2b','daytona','opensandbox'])
def test_all_provider_defaults_load(monkeypatch,tmp_path,provider):
    config(monkeypatch,tmp_path,f'workmate_runtime:\n  sandbox:\n    provider: {provider}\n')
    assert load_runtime_settings().sandbox.provider == provider


def test_environment_provider_switch_uses_selected_defaults(monkeypatch,tmp_path):
    config(monkeypatch,tmp_path,'workmate_runtime:\n  sandbox:\n    provider: docker\n    base_image: custom:1\n')
    monkeypatch.setenv('WORKMATE_SANDBOX_PROVIDER','e2b')
    monkeypatch.setenv('E2B_API_KEY','not-a-real-secret')
    monkeypatch.setenv('E2B_DOMAIN','sandbox.example.com')
    settings=load_runtime_settings().sandbox
    assert settings.provider=='e2b' and settings.template=='base'
    assert settings.api_key.get_secret_value()=='not-a-real-secret'
    assert settings.domain=='sandbox.example.com'
    assert 'not-a-real-secret' not in repr(settings)


@pytest.mark.parametrize('body',[
    'provider: local\n    image: unused',
    'provider: e2b\n    timeout_seconds: 0',
    'provider: docker\n    base_image: ""',
    'provider: opensandbox\n    protocol: ftp',
    'provider: e2b\n    api_key: forbidden-in-yaml',
    'provider: kubernetes\n    kubeconfig: /secret/config',
])
def test_provider_options_validate_and_yaml_rejects_connections(monkeypatch,tmp_path,body):
    config(monkeypatch,tmp_path,'workmate_runtime:\n  sandbox:\n    '+body+'\n')
    with pytest.raises(ValueError):
        load_runtime_settings()


@pytest.mark.parametrize('provider,fields',[
    ('docker',{'base_image':'custom:1','node_version':'22','extra_pip':['numpy']}),
    ('kubernetes',{'image':'custom:2','namespace':'workmate','storage_class':'ssd','storage_size':'5Gi'}),
    ('e2b',{'template':'custom','timeout_seconds':600}),
    ('daytona',{'target':'eu','timeout_seconds':600}),
    ('opensandbox',{'image':'custom:3','protocol':'https','timeout_seconds':600}),
])
def test_provider_specific_yaml_retained(monkeypatch,tmp_path,provider,fields):
    import yaml
    config(monkeypatch,tmp_path,yaml.safe_dump({'workmate_runtime':{'sandbox':{'provider':provider,**fields}}}))
    monkeypatch.setenv('WORKMATE_SANDBOX_PROVIDER',provider)
    policy=load_runtime_settings().sandbox
    for name,value in fields.items():
        assert getattr(policy,name)==value


@pytest.mark.parametrize('provider,environment,field,value',[
    ('kubernetes','WORKMATE_SANDBOX_KUBECONFIG','kubeconfig','/mounted/kubeconfig'),
    ('daytona','DAYTONA_API_URL','api_url','https://sandbox.example.com'),
    ('daytona','DAYTONA_API_KEY','api_key','test-key'),
    ('opensandbox','OPEN_SANDBOX_DOMAIN','domain','sandbox.example.com:443'),
    ('opensandbox','OPEN_SANDBOX_API_KEY','api_key','test-key'),
])
def test_environment_connections_are_kept_out_of_serialized_policy(monkeypatch,tmp_path,provider,environment,field,value):
    config(monkeypatch,tmp_path,f'workmate_runtime:\n  sandbox:\n    provider: {provider}\n')
    monkeypatch.setenv(environment,value)
    policy=load_runtime_settings().sandbox
    actual=getattr(policy,field)
    assert (actual.get_secret_value() if field=='api_key' else actual)==value
    assert field not in policy.model_dump()


def test_workspace_lifecycle_defaults_yaml_and_environment(monkeypatch, tmp_path):
    config(monkeypatch, tmp_path)
    assert load_runtime_settings().workspace_lifecycle.warm_ttl_seconds == 600
    config(monkeypatch, tmp_path, 'workmate_runtime:\n  workspace_lifecycle:\n    warm_ttl_seconds: 12\n    max_warm_per_worker: 2\n    sweep_interval_seconds: 3\n')
    policy = load_runtime_settings().workspace_lifecycle
    assert (policy.warm_ttl_seconds, policy.max_warm_per_worker, policy.sweep_interval_seconds) == (12, 2, 3)
    for key, value in [('WARM_TTL_SECONDS', '0'), ('MAX_WARM_PER_WORKER', '0'), ('SWEEP_INTERVAL_SECONDS', '1')]:
        monkeypatch.setenv('WORKMATE_WORKSPACE_' + key, value)
    policy = load_runtime_settings().workspace_lifecycle
    assert (policy.warm_ttl_seconds, policy.max_warm_per_worker, policy.sweep_interval_seconds) == (0, 0, 1)
    monkeypatch.setenv('WORKMATE_WORKSPACE_WARM_TTL_SECONDS', '-1')
    with pytest.raises(ValueError):
        load_runtime_settings()


@pytest.mark.parametrize('field,value', [('warm_ttl_seconds', -1), ('max_warm_per_worker', True), ('sweep_interval_seconds', 0)])
def test_workspace_lifecycle_invalid_yaml(monkeypatch, tmp_path, field, value):
    import yaml
    config(monkeypatch, tmp_path, yaml.safe_dump({'workmate_runtime': {'workspace_lifecycle': {field: value}}}))
    with pytest.raises(ValueError):
        load_runtime_settings()
