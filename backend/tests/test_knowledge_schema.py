"""Current model and deployment contracts."""
from pathlib import Path
from sqlalchemy.orm import configure_mappers


def test_knowledge_deployment_is_private_and_opt_in():
    import yaml

    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    for name in ("qdrant",):
        assert compose["services"][name]["profiles"] == ["knowledge"]
        assert not compose["services"][name].get("ports")
    # Object storage is shared with the default Skill package service. RAG
    # initialization and vector storage remain opt-in and all stores private.
    assert not compose['services']['rustfs'].get('profiles')
    assert not compose['services']['rustfs'].get('ports')
    qdrant = compose["services"]["qdrant"]
    assert "QDRANT__SERVICE__API_KEY:?" in qdrant["command"][0]
    assert compose["services"]["storage-init"]["entrypoint"] == ["python", "/app/init_storage.py"]
    assert not compose['services']['storage-init'].get('profiles')
    assert all('minio/' not in str(service.get('image', '')) for service in compose['services'].values())
    assert 'knowledge-rustfs-data:/data' in compose['services']['rustfs']['volumes']
    product_env = compose["services"]["gateway"]["environment"]
    assert "KNOWLEDGE_S3_SECRET_KEY" not in product_env
    settings = yaml.safe_load((root / "config/workmate.yaml").read_text())
    from app.config_file import KnowledgeFileConfig
    assert KnowledgeFileConfig().enabled is False
    assert isinstance(settings["product"]["knowledge"]["enabled"], bool)
