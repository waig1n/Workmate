import json
import pytest
from pydantic import ValidationError
from app.config import Settings
from app.config_file import load_file_config
from workmate_product.config.nodes import WorkerNode


@pytest.mark.parametrize('url', ['http://node:abc', 'http://node:0', 'http://node:65536'])
def test_worker_node_rejects_invalid_port(url):
    with pytest.raises(ValidationError):
        WorkerNode(storage_node_id='node-a', base_url=url)


@pytest.mark.parametrize('url', ['http://node:1', 'https://node:65535', 'http://[::1]:8000/'])
def test_worker_node_accepts_valid_port_boundaries(url):
    assert WorkerNode(storage_node_id='node-a', base_url=url).base_url == url.rstrip('/')


def test_artifact_storage_default_and_yaml(monkeypatch, tmp_path):
    path=tmp_path/'workmate.yaml'
    path.write_text('product:\n  artifacts:\n    storage:\n      backend: s3\n      region: test-region\n      timeout_seconds: 12\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    try:
        s=Settings(_env_file=None)
        assert s.artifact_storage.backend=='s3'
        assert s.artifact_storage.region=='test-region'
        assert s.artifact_storage.timeout_seconds==12
        monkeypatch.setenv('ARTIFACT_STORAGE_BACKEND','local')
        assert Settings(_env_file=None).artifact_storage.backend=='local'
    finally:pass


def test_nodes_require_s3_and_unique_valid_identity(monkeypatch):
    nodes=[{'storage_node_id':'node-a','base_url':'http://runtime-a:8000'}]
    monkeypatch.setenv('WORKMATE_WORKER_NODES',json.dumps(nodes))
    with pytest.raises(ValidationError):Settings(_env_file=None,artifact_storage={'backend':'local'})
    s=Settings(_env_file=None,artifact_storage={'backend':'s3'})
    assert s.worker_nodes[0].enabled is True
    monkeypatch.setenv('WORKMATE_WORKER_NODES',json.dumps(nodes*2))
    with pytest.raises(ValidationError):Settings(_env_file=None,artifact_storage={'backend':'s3'})


@pytest.mark.parametrize('value',['unknown','',None])
def test_invalid_storage_backend(value):
    with pytest.raises(ValidationError):Settings(_env_file=None,artifact_storage={'backend':value})
