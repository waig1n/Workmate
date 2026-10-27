"""Product admission uses the same deployment storage identity as its Worker."""
from pathlib import Path
import pytest
from pydantic import ValidationError
import yaml
from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_config(tmp_path,monkeypatch):
    config=tmp_path/'workmate.yaml';config.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(config))
    monkeypatch.delenv('WORKMATE_WORKER_STORAGE_NODE_ID',raising=False)
    monkeypatch.delenv('WORKER_STORAGE_NODE_ID',raising=False)


def test_storage_identity_defaults_match_compose_worker():
    assert Settings(_env_file=None).worker_storage_node_id=='local-worker-storage'


def test_storage_identity_environment_and_explicit_override(monkeypatch):
    monkeypatch.setenv('WORKMATE_WORKER_STORAGE_NODE_ID','node-A.volume_1')
    assert Settings(_env_file=None).worker_storage_node_id=='node-A.volume_1'
    assert Settings(_env_file=None,worker_storage_node_id='explicit-node').worker_storage_node_id=='explicit-node'


@pytest.mark.parametrize('value',[' ','../node','-node','_node','.node','node/child','node\n','a'*65,'节点'])
def test_invalid_environment_storage_identity_is_rejected(monkeypatch,value):
    monkeypatch.setenv('WORKMATE_WORKER_STORAGE_NODE_ID',value)
    with pytest.raises(ValidationError): Settings(_env_file=None)


@pytest.mark.parametrize('value',['',True,123,None])
def test_invalid_explicit_storage_identity_is_rejected(value):
    with pytest.raises(ValidationError): Settings(_env_file=None,worker_storage_node_id=value)


def test_storage_identity_boundaries_and_empty_environment_follow_existing_settings_policy(monkeypatch):
    assert Settings(_env_file=None,worker_storage_node_id='a').worker_storage_node_id=='a'
    assert Settings(_env_file=None,worker_storage_node_id='a'*64).worker_storage_node_id=='a'*64
    monkeypatch.setenv('WORKMATE_WORKER_STORAGE_NODE_ID','')
    assert Settings(_env_file=None).worker_storage_node_id=='local-worker-storage'


def test_product_and_worker_compose_share_exact_deployment_identity_variable():
    config=yaml.safe_load((Path(__file__).parents[2]/'docker-compose.yml').read_text())
    product=config['services']['gateway']['environment']['WORKMATE_WORKER_STORAGE_NODE_ID']
    worker=config['services']['runtime']['environment']['WORKMATE_WORKER_STORAGE_NODE_ID']
    assert product==worker=='${WORKMATE_WORKER_STORAGE_NODE_ID:-local-worker-storage}'
