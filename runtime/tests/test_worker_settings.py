"""Worker policy defaults, YAML, env overrides and invalid values fail closed."""
import pytest
from workmate_product.config.worker import WorkerPolicy
from workmate_runtime.config import load_runtime_settings


@pytest.fixture
def config(tmp_path,monkeypatch):
    path=tmp_path/'workmate.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    for key in WorkerPolicy.model_fields:
        monkeypatch.delenv('WORKMATE_WORKER_'+key.upper(),raising=False)
    return path


def test_worker_policy_defaults_and_yaml(config):
    assert load_runtime_settings().worker==WorkerPolicy()
    config.write_text('version: 1\nworkmate_runtime:\n  worker:\n    capacity: 8\n    lease_seconds: 45\n')
    settings=load_runtime_settings()
    assert settings.worker.capacity==8 and settings.worker.lease_seconds==45


def test_worker_env_overrides_yaml_and_artifact_policy_is_shared(config,monkeypatch):
    config.write_text('version: 1\nproduct:\n  artifacts:\n    max_file_bytes: 1234\n    max_files_per_run: 3\nworkmate_runtime:\n  artifacts:\n    strict_delivery: true\n  worker:\n    capacity: 8\n')
    for key,value in {'capacity':'9','lease_seconds':'50','drain_seconds':'0','startup_timeout_seconds':'80'}.items():
        monkeypatch.setenv('WORKMATE_WORKER_'+key.upper(),value)
    settings=load_runtime_settings()
    assert settings.worker==WorkerPolicy(capacity=9,lease_seconds=50,drain_seconds=0,startup_timeout_seconds=80)
    assert (settings.artifact_max_file_bytes,settings.artifact_max_files_per_run,settings.artifact_strict_delivery)==(1234,3,True)
    monkeypatch.setenv('ARTIFACT_MAX_FILE_BYTES','4321')
    monkeypatch.setenv('ARTIFACT_MAX_FILES_PER_RUN','5')
    monkeypatch.setenv('ARTIFACT_STRICT_DELIVERY','false')
    settings=load_runtime_settings()
    assert (settings.artifact_max_file_bytes,settings.artifact_max_files_per_run,settings.artifact_strict_delivery)==(4321,5,False)


@pytest.mark.parametrize('key,value',[('capacity',0),('capacity',True),('lease_seconds',2),
    ('lease_seconds',301),('drain_seconds',-1),('startup_timeout_seconds',0)])
def test_invalid_yaml_policy_fails(config,key,value):
    import yaml
    config.write_text(yaml.safe_dump({'version':1,'workmate_runtime':{'worker':{key:value}}}))
    with pytest.raises(ValueError):
        load_runtime_settings()


@pytest.mark.parametrize('key,value',[('capacity','0'),('lease_seconds','nan'),('drain_seconds','-1'),('startup_timeout_seconds','0')])
def test_invalid_env_policy_fails(config,monkeypatch,key,value):
    monkeypatch.setenv('WORKMATE_WORKER_'+key.upper(),value)
    with pytest.raises(ValueError):
        load_runtime_settings()


def test_worker_role_identity_and_roots_are_required_without_secret_errors(config,monkeypatch,tmp_path):
    from workmate_runtime.roles.settings import load_worker_role_settings
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('WORKMATE_WORKER_STORAGE_NODE_ID',raising=False)
    with pytest.raises(ValueError):
        load_worker_role_settings()
    monkeypatch.setenv('DATABASE_URL','postgresql+asyncpg://test:test-only@localhost/test')
    monkeypatch.setenv('WORKMATE_WORKER_STORAGE_NODE_ID','node-a')
    monkeypatch.setenv('WORKMATE_WORKSPACE_BASEDIR',str(tmp_path/'workspaces'))
    monkeypatch.setenv('ARTIFACT_STORAGE_DIR',str(tmp_path/'artifacts'))
    settings=load_worker_role_settings()
    assert settings.storage_node_id=='node-a' and 'test-only' not in repr(settings)
    assert not settings.workspace_dir.exists() and not settings.artifact_dir.exists()
    monkeypatch.setenv('ARTIFACT_STORAGE_DIR',str(tmp_path/'workspaces'/'output'))
    with pytest.raises(ValueError,match='separate'):
        load_worker_role_settings()
    monkeypatch.setenv('ARTIFACT_STORAGE_DIR',str(tmp_path/'unused'/'..'/'workspaces'))
    with pytest.raises(ValueError,match='separate'):
        load_worker_role_settings()
    alias=tmp_path/'workspace-alias'
    alias.symlink_to(tmp_path/'workspaces',target_is_directory=True)
    monkeypatch.setenv('ARTIFACT_STORAGE_DIR',str(alias))
    with pytest.raises(ValueError,match='separate'):
        load_worker_role_settings()
