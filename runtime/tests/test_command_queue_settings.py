import pytest
from workmate_product.config.command_queue import CommandQueuePolicy
from workmate_runtime.config import load_runtime_settings
from app.config import Settings


@pytest.fixture
def config(tmp_path,monkeypatch):
    path=tmp_path/'workmate.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    monkeypatch.delenv('WORKMATE_COORDINATION_NAMESPACE',raising=False)
    for key in CommandQueuePolicy.model_fields:
        monkeypatch.delenv('WORKMATE_COMMAND_QUEUE_'+key.upper(),raising=False)
    return path


def test_namespace_default_and_environment_match_across_roles(config,monkeypatch):
    assert load_runtime_settings().coordination_namespace=='workmate'
    assert Settings(_env_file=None).coordination_namespace=='workmate'
    monkeypatch.setenv('WORKMATE_COORDINATION_NAMESPACE','local_test-2')
    assert load_runtime_settings().coordination_namespace=='local_test-2'
    assert Settings(_env_file=None).coordination_namespace=='local_test-2'


@pytest.mark.parametrize('namespace',['with:colon','with space','x'*65])
def test_invalid_namespace_rejected_by_both_roles(config,monkeypatch,namespace):
    monkeypatch.setenv('WORKMATE_COORDINATION_NAMESPACE',namespace)
    for load in (load_runtime_settings,lambda:Settings(_env_file=None)):
        with pytest.raises(ValueError): load()


def test_defaults_and_shared_yaml(config):
    assert load_runtime_settings().command_queue == CommandQueuePolicy()
    assert Settings(_env_file=None).command_queue == CommandQueuePolicy()
    config.write_text('version: 1\ncommand_queue:\n  batch_size: 12\n')
    from app.config_file import _load_file_config_path
    _load_file_config_path.cache_clear()
    assert load_runtime_settings().command_queue.batch_size == 12
    assert Settings(_env_file=None).command_queue.batch_size == 12


@pytest.mark.parametrize('key',CommandQueuePolicy.model_fields)
def test_shared_environment_override(config,monkeypatch,key):
    value=.5 if key=='poll_interval_seconds' else 60
    monkeypatch.setenv('WORKMATE_COMMAND_QUEUE_'+key.upper(),str(value))
    assert getattr(load_runtime_settings().command_queue,key)==value
    assert getattr(Settings(_env_file=None).command_queue,key)==value


@pytest.mark.parametrize('key',CommandQueuePolicy.model_fields)
def test_invalid_yaml_and_environment_fail(config,monkeypatch,key):
    config.write_text(f'version: 1\ncommand_queue:\n  {key}: -1\n')
    for load in (load_runtime_settings,lambda:Settings(_env_file=None)):
        with pytest.raises(ValueError): load()
    config.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_COMMAND_QUEUE_'+key.upper(),'-1')
    for load in (load_runtime_settings,lambda:Settings(_env_file=None)):
        with pytest.raises(ValueError): load()
