"""Pure deployment planning: no Docker, filesystem or network effects."""

import importlib.util
import sys
import pytest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "deploy_config", Path(__file__).parents[1] / "deploy_config.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
evaluate = module.evaluate


def configs():
    model = {
        "type": "openai",
        "model": "chat",
        "api_key_env": "CHAT_KEY",
        "base_url_env": "CHAT_URL",
    }
    app = {
        "product": {
            "models": {"default": "main", "profiles": {"main": model}},
            "web": {"enabled": False},
            "knowledge": {"enabled": False},
        }
    }
    shared = {"CHAT_KEY": "chat-secret", "CHAT_URL": "https://model.example/v1",
        'SKILL_S3_ENDPOINT': 'https://skills.example', 'SKILL_S3_BUCKET': 'workmate-skills',
        'SKILL_S3_ACCESS_KEY': 'skill-access', 'SKILL_S3_SECRET_KEY': 'skill-secret'}
    compose = {
        "services": {
            "gateway": {
                "environment": {
                    **shared,
                    "JWT_SECRET": "j" * 40,
                    "RUNTIME_INTERNAL_TOKEN": "internal-secret",
                    "RUNTIME_BASE_URL": "http://runtime:8000",
                    "WORKMATE_WORKER_STORAGE_NODE_ID": "node",
                    "ARTIFACT_STORAGE_DIR": "/data/artifacts",
                    "DATABASE_URL": "postgresql+asyncpg://workmate:db-secret@postgres:5432/workmate",
                    "WORKMATE_COORDINATION_REDIS_URL": "redis://redis:6379/0",
                    "WORKMATE_COORDINATION_NAMESPACE": "test-deploy",
                }
            },
            "runtime": {
                "environment": {
                    **shared,
                    "WORKMATE_INTERNAL_TOKEN": "internal-secret",
                    "DATABASE_URL": "postgresql+asyncpg://workmate:db-secret@postgres:5432/workmate",
                    "WORKMATE_WORKER_STORAGE_NODE_ID": "node",
                    "ARTIFACT_STORAGE_DIR": "/data/artifacts",
                    "AS_DATABASE_URL": "postgresql+asyncpg://workmate:db-secret@postgres:5432/workmate_agentscope",
                    "AS_REDIS_URL": "redis://redis:6379/0",
                    "WORKMATE_COORDINATION_REDIS_URL": "redis://redis:6379/0",
                    "WORKMATE_COORDINATION_NAMESPACE": "test-deploy",
                }
            },
            "frontend": {},
            "postgres": {
                "environment": {
                    "POSTGRES_USER": "workmate",
                    "POSTGRES_PASSWORD": "db-secret",
                    "POSTGRES_DB": "workmate",
                }
            },
            "redis": {},
            "qdrant": {
                "profiles": ["knowledge"],
                "environment": {"QDRANT__SERVICE__API_KEY": "qdrant-secret"},
            },
            "rustfs": {
                "profiles": ["knowledge"],
                "environment": {
                    "RUSTFS_ACCESS_KEY": "access",
                    "RUSTFS_SECRET_KEY": "rustfs-secret",
                },
            },
            "storage-init": {
                "environment": {
                    "KNOWLEDGE_S3_ENDPOINT": "http://rustfs:9000",
                    "KNOWLEDGE_S3_ACCESS_KEY": "access",
                    "KNOWLEDGE_S3_SECRET_KEY": "rustfs-secret",
                    "KNOWLEDGE_S3_BUCKET": "knowledge",
                    'SKILL_S3_ENDPOINT': 'http://rustfs:9000',
                    'SKILL_S3_ACCESS_KEY': 'access',
                    'SKILL_S3_SECRET_KEY': 'rustfs-secret',
                    'SKILL_S3_BUCKET': 'workmate-skills',
                },
            },
            "searxng": {
                "profiles": ["web-tool"],
                "environment": {"SEARXNG_SECRET": "search-secret"},
            },
            "crawl4ai": {
                "profiles": ["web-tool"],
                "environment": {"CRAWL4AI_API_TOKEN": "crawl-secret"},
            },
        }
    }
    return app, compose


def errors(plan):
    return [f.code for f in plan.findings if f.level == "error"]


def test_command_coordination_required_shared_and_isolated():
    app,compose=configs()
    for role in ('gateway','runtime'):
        compose['services'][role]['environment']['AS_REDIS_URL']='redis://external:6379/0'
    assert 'redis' in evaluate(app,compose).dependencies
    compose['services']['gateway']['environment']['WORKMATE_COORDINATION_REDIS_URL']=''
    assert 'environment_missing' in errors(evaluate(app,compose))
    compose['services']['gateway']['environment']['WORKMATE_COORDINATION_REDIS_URL']='redis://other:6379/0'
    assert 'command_coordination_mismatch' in errors(evaluate(app,compose))
    compose['services']['gateway']['environment']['WORKMATE_COORDINATION_REDIS_URL']='redis://redis:6379/0'
    compose['services']['runtime']['environment']['WORKMATE_COORDINATION_NAMESPACE']='another'
    assert 'command_namespace_mismatch' in errors(evaluate(app,compose))
    compose['services']['runtime']['environment']['WORKMATE_COORDINATION_NAMESPACE']='invalid:name'
    assert 'command_namespace_invalid' in errors(evaluate(app,compose))


def test_skill_storage_is_required_without_knowledge():
    app, compose = configs()
    compose['services']['runtime']['environment']['SKILL_S3_ENDPOINT'] = ''
    assert 'environment_missing' in errors(evaluate(app, compose))


def test_local_skill_bucket_starts_before_api_without_knowledge_profile():
    app, compose = configs()
    compose['services']['rustfs'].pop('profiles')
    for role in ('gateway', 'runtime'):
        compose['services'][role]['environment'].update(SKILL_S3_ENDPOINT='http://rustfs:9000',
            SKILL_S3_ACCESS_KEY='access', SKILL_S3_SECRET_KEY='rustfs-secret')
    plan = evaluate(app, compose)
    assert not errors(plan)
    assert 'rustfs' in plan.dependencies
    assert plan.init_services == ('storage-init',)
    assert 'knowledge' not in plan.profiles
    assert plan.apps[0] == 'gateway'


@pytest.mark.parametrize('field', ['SKILL_S3_ENDPOINT', 'SKILL_S3_BUCKET', 'SKILL_S3_ACCESS_KEY', 'SKILL_S3_SECRET_KEY'])
def test_skill_connections_must_match_between_api_and_worker(field):
    app, compose = configs()
    compose['services']['gateway']['environment'][field] += '-other'
    assert 'skill_storage_mismatch' in errors(evaluate(app, compose))


@pytest.mark.parametrize('field,code', [
    ('DATABASE_URL', 'worker_database_mismatch'),
    ('WORKMATE_WORKER_STORAGE_NODE_ID', 'worker_storage_mismatch'),
    ('ARTIFACT_STORAGE_DIR', 'worker_storage_mismatch'),
])
def test_native_worker_cannot_use_another_product_database_or_storage(field, code):
    app, compose = configs()
    compose['services']['runtime']['environment'][field] = 'different-private-value'
    plan = evaluate(app, compose)
    assert code in errors(plan)
    assert all('different-private-value' not in f.message for f in plan.findings)


def test_native_plan_rejects_legacy_app_instead_of_starting_it():
    app, compose = configs()
    compose['services']['agentscope-runtime'] = compose['services'].pop('runtime')
    plan = evaluate(app, compose)
    assert 'service_missing' in errors(plan)
    assert 'agentscope-runtime' not in plan.apps


def test_native_product_must_not_route_knowledge_back_to_legacy_app():
    app, compose = configs()
    compose['services']['gateway']['environment']['RUNTIME_BASE_URL'] = 'http://agentscope-runtime:8000'
    assert 'legacy_runtime_endpoint' in errors(evaluate(app, compose))


def test_disabled_optional_services_never_started():
    app, compose = configs()
    plan = evaluate(app, compose)
    assert not errors(plan)
    assert plan.profiles == ()
    assert plan.dependencies == ("postgres", "redis")
    assert plan.apps == ("gateway", "runtime", "frontend")
    assert plan.init_services == ()


def test_local_optional_dependencies_and_profiles():
    app, compose = configs()
    app["product"]["web"] = {
        "enabled": True,
        "tools": [
            {"base_url_env": "SEARCH_URL"},
            {
                "name": "web_fetch",
                "base_url_env": "FETCH_URL",
                "token_env": "FETCH_TOKEN",
            },
        ],
    }
    app["product"]["knowledge"] = {"enabled": True}
    app["product"]["knowledge"]["embedding"] = {
        "type": "openai",
        "model": "embedding",
        "dimensions": 1024,
        "api_key_env": "EMBED_KEY",
    }
    compose["services"]["runtime"]["environment"].update(
        SEARCH_URL="http://searxng:8080",
        FETCH_URL="http://crawl4ai:11235",
        FETCH_TOKEN="crawl-secret",
        EMBED_KEY="embedding-secret",
        KNOWLEDGE_QDRANT_URL="http://qdrant:6333",
        KNOWLEDGE_QDRANT_API_KEY="qdrant-secret",
        KNOWLEDGE_S3_ENDPOINT="http://rustfs:9000",
        KNOWLEDGE_S3_BUCKET="knowledge",
        KNOWLEDGE_S3_ACCESS_KEY="access",
        KNOWLEDGE_S3_SECRET_KEY="rustfs-secret",
    )
    plan = evaluate(app, compose)
    assert not errors(plan)
    assert plan.dependencies == (
        "postgres",
        "redis",
        "searxng",
        "crawl4ai",
        "qdrant",
        "rustfs",
    )
    assert plan.profiles == ("web-tool", "knowledge")
    assert plan.init_services == ("storage-init",)


def test_external_databases_and_providers_do_not_start_local_services():
    app, compose = configs()
    product = compose["services"]["gateway"]["environment"]
    runtime = compose["services"]["runtime"]["environment"]
    product["DATABASE_URL"] = "postgresql+asyncpg://p:secret@db.example/product"
    runtime["DATABASE_URL"] = product["DATABASE_URL"]
    runtime["AS_DATABASE_URL"] = "postgresql+asyncpg://p:secret@db.example/runtime"
    runtime["AS_REDIS_URL"] = "rediss://cache.example/0"
    runtime['WORKMATE_COORDINATION_REDIS_URL']=product['WORKMATE_COORDINATION_REDIS_URL']='rediss://cache.example/0'
    app["product"]["web"] = {"enabled": True, "tools": [{"base_url_env": "SEARCH_URL"}]}
    runtime["SEARCH_URL"] = "https://search.example"
    plan = evaluate(app, compose)
    assert not errors(plan)
    assert plan.dependencies == ()
    assert plan.profiles == ()


def test_errors_do_not_echo_secret_values_and_repr_hides_them():
    app, compose = configs()
    runtime = compose["services"]["runtime"]["environment"]
    runtime["WORKMATE_INTERNAL_TOKEN"] = "different-secret"
    runtime["AS_DATABASE_URL"] = "postgresql+asyncpg://bad:wrong-secret@postgres/workmate"
    compose["services"]["gateway"]["environment"]["JWT_SECRET"] = (
        "change-me-in-production"
    )
    plan = evaluate(app, compose)
    assert {
        "internal_token_mismatch",
        "database_identity_mismatch",
        "database_not_isolated",
    } <= set(errors(plan))
    for secret in (
        "different-secret",
        "wrong-secret",
        "db-secret",
        "chat-secret",
        "change-me-in-production",
    ):
        assert secret in plan.secrets
        assert secret not in repr(plan)
        assert all(secret not in f.message for f in plan.findings)


def test_default_model_missing_key_is_error_optional_profile_is_warning():
    app, compose = configs()
    app["product"]["models"]["profiles"]["extra"] = {"api_key_env": "EXTRA_KEY"}
    plan = evaluate(app, compose)
    assert not errors(plan)
    assert any(
        f.level == "warning" and f.code == "model_environment_missing"
        for f in plan.findings
    )
    del compose["services"]["gateway"]["environment"]["CHAT_KEY"]
    assert "model_environment_missing" in errors(evaluate(app, compose))


def test_malformed_inputs_are_friendly_findings():
    plan = evaluate(
        {"product": {"models": None}},
        {"services": {"gateway": {"environment": None}}},
    )
    assert errors(plan)
    assert all(f.message for f in plan.findings)


def enable_knowledge(app, compose, local=False):
    app["product"]["knowledge"] = {"enabled": True}
    app["product"]["knowledge"]["embedding"] = {
        "type": "openai",
        "model": "embedding",
        "dimensions": 1024,
        "api_key_env": "EMBED_KEY",
    }
    runtime = compose["services"]["runtime"]["environment"]
    runtime.update(
        EMBED_KEY="embedding-secret",
        KNOWLEDGE_QDRANT_URL="http://qdrant:6333"
        if local
        else "https://vectors.example",
        KNOWLEDGE_QDRANT_API_KEY="qdrant-secret",
        KNOWLEDGE_S3_ENDPOINT="http://rustfs:9000"
        if local
        else "https://objects.example",
        KNOWLEDGE_S3_BUCKET="knowledge",
        KNOWLEDGE_S3_ACCESS_KEY="access",
        KNOWLEDGE_S3_SECRET_KEY="rustfs-secret",
    )
    return runtime


def test_external_knowledge_never_runs_rustfs_init_or_local_vector_store():
    app, compose = configs()
    runtime = enable_knowledge(app, compose)
    plan = evaluate(app, compose)
    assert not errors(plan)
    assert plan.dependencies == ("postgres", "redis")
    assert plan.init_services == ()
    assert plan.profiles == ()
    runtime["KNOWLEDGE_QDRANT_API_KEY"] = ""
    assert "environment_missing" in errors(evaluate(app, compose))


def test_local_service_credentials_and_initialization_must_match():
    app, compose = configs()
    enable_knowledge(app, compose, local=True)
    compose["services"]["qdrant"]["environment"]["QDRANT__SERVICE__API_KEY"] = "wrong"
    compose["services"]["rustfs"]["environment"]["RUSTFS_SECRET_KEY"] = "short"
    compose["services"]["storage-init"]["environment"]["KNOWLEDGE_S3_BUCKET"] = "other"
    assert {
        "qdrant_key_mismatch",
        "rustfs_root_invalid",
        "rustfs_credentials_mismatch",
        "rustfs_init_mismatch",
    } <= set(errors(evaluate(app, compose)))


def test_invalid_endpoint_and_database_port_never_leak_url():
    app, compose = configs()
    runtime = compose["services"]["runtime"]["environment"]
    runtime["AS_DATABASE_URL"] = (
        "postgresql://owner:database-secret@postgres:invalid/runtime"
    )
    plan = evaluate(app, compose)
    assert "url_invalid" in errors(plan)
    assert "database-secret" in plan.secrets
    assert "database-secret" not in repr(plan)


def test_mapping_and_model_mismatch_checks():
    app, compose = configs()
    runtime = compose["services"]["runtime"]["environment"]
    runtime["CHAT_KEY"] = "different"
    del runtime["CHAT_URL"]
    app["product"]["models"]["default"] = "absent"
    codes = set(f.code for f in evaluate(app, compose).findings)
    assert {
        "default_model_missing",
        "model_environment_mismatch",
        "model_environment_missing",
    } <= codes


def test_wrong_switch_types_are_not_treated_as_disabled():
    app, compose = configs()
    app["product"]["knowledge"]["enabled"] = "true"
    assert "feature_enabled_invalid" in errors(evaluate(app, compose))


def test_weak_secrets_warn_but_missing_or_mismatched_credentials_fail():
    app, compose = configs()
    product = compose["services"]["gateway"]["environment"]
    product["JWT_SECRET"] = "change-me-in-production"
    plan = evaluate(app, compose)
    assert not errors(plan)
    assert any(
        f.code == "jwt_secret_weak" and f.level == "warning" and f.security
        for f in plan.findings
    )
    assert any(f.code == "internal_token_weak" and f.security for f in plan.findings)
    product["JWT_SECRET"] = ""
    assert "environment_missing" in errors(evaluate(app, compose))
    product["RUNTIME_INTERNAL_TOKEN"] = "mismatch"
    assert "internal_token_mismatch" in errors(evaluate(app, compose))


def test_knowledge_cannot_fall_back_to_memory_embedding():
    app, compose = configs()
    runtime = enable_knowledge(app, compose)
    app["product"]["models"]["embedding"] = app["product"]["knowledge"].pop("embedding")
    assert "embedding_model_invalid" in errors(evaluate(app, compose))
    app["product"]["knowledge"]["embedding"] = {
        **app["product"]["models"]["embedding"],
        "api_key_env": "RAG_ONLY_KEY",
    }
    assert "model_environment_missing" in errors(evaluate(app, compose))
    runtime["RAG_ONLY_KEY"] = "rag-secret"
    assert not errors(evaluate(app, compose))


@pytest.mark.parametrize(
    "server_token,client_token",
    [("", ""), ("server", "wrong"), ("server", ""), ("", "client")],
)
def test_local_crawl4ai_requires_matching_nonempty_token(server_token, client_token):
    app, compose = configs()
    app["product"]["web"] = {
        "enabled": True,
        "tools": [
            {
                "name": "web_fetch",
                "base_url_env": "FETCH_URL",
                "token_env": "FETCH_TOKEN",
            }
        ],
    }
    compose["services"]["runtime"]["environment"].update(
        FETCH_URL="http://crawl4ai:11235", FETCH_TOKEN=client_token
    )
    compose["services"]["crawl4ai"]["environment"]["CRAWL4AI_API_TOKEN"] = server_token
    assert "web_fetch_token_mismatch" in errors(evaluate(app, compose))


def test_external_fetch_does_not_require_local_crawl4ai_token():
    app, compose = configs()
    app["product"]["web"] = {
        "enabled": True,
        "tools": [{"name": "web_fetch", "base_url_env": "FETCH_URL"}],
    }
    compose["services"]["runtime"]["environment"]["FETCH_URL"] = (
        "https://external-fetch.example"
    )
    compose["services"]["crawl4ai"]["environment"]["CRAWL4AI_API_TOKEN"] = ""
    assert "web_fetch_token_mismatch" not in errors(evaluate(app, compose))


def test_embedding_dimensions_mode_validation():
    app, compose = configs()
    enable_knowledge(app, compose)
    profile = app["product"]["knowledge"]["embedding"]
    profile["dimensions_mode"] = "unknown"
    assert "embedding_dimensions_mode_invalid" in errors(evaluate(app, compose))
    for mode in ("auto", "send", "omit"):
        profile["dimensions_mode"] = mode
        assert not errors(evaluate(app, compose))


def test_rustfs_initializer_cannot_target_another_endpoint():
    app, compose = configs()
    enable_knowledge(app, compose, local=True)
    compose["services"]["storage-init"]["environment"]["KNOWLEDGE_S3_ENDPOINT"] = (
        "https://external.example"
    )
    assert "rustfs_init_mismatch" in errors(evaluate(app, compose))


def multinode_configs():
    app, compose = configs()
    app['product']['artifacts'] = {'storage': {'backend': 's3'}}
    for role in ('gateway', 'runtime'):
        compose['services'][role]['environment'].update(
            ARTIFACT_S3_ENDPOINT='https://objects.example', ARTIFACT_S3_BUCKET='artifacts',
            ARTIFACT_S3_ACCESS_KEY='object-access', ARTIFACT_S3_SECRET_KEY='object-secret')
    compose['services']['gateway']['environment']['WORKMATE_WORKER_NODES'] = (
        '[{"storage_node_id":"node-a","base_url":"http://node-a:8000"}]')
    compose['services']['runtime']['environment']['WORKMATE_WORKER_STORAGE_NODE_ID'] = 'node-a'
    compose['services']['runtime']['environment']['ARTIFACT_STORAGE_DIR'] = '/different/local/cache'
    return app, compose


def test_multinode_s3_allows_node_local_identity_and_paths():
    app, compose = multinode_configs()
    assert not errors(evaluate(app, compose))


def test_multinode_requires_object_storage():
    app, compose = multinode_configs()
    app['product']['artifacts']['storage']['backend'] = 'local'
    assert 'worker_nodes_require_s3' in errors(evaluate(app, compose))


@pytest.mark.parametrize('nodes', ['{}', 'bad-json', '[{}]',
    '[{"storage_node_id":"a","base_url":"http://secret:password@host"}]',
    '[{"storage_node_id":"a","base_url":"http://host","enabled":"false"}]',
    '[{"storage_node_id":"a","base_url":"http://host"},{"storage_node_id":"a","base_url":"http://other"}]'])
def test_invalid_node_directory_is_rejected_without_disclosing_values(nodes):
    app, compose = multinode_configs()
    compose['services']['gateway']['environment']['WORKMATE_WORKER_NODES'] = nodes
    plan = evaluate(app, compose)
    assert 'worker_nodes_invalid' in errors(plan)
    assert all(nodes not in finding.message for finding in plan.findings)


@pytest.mark.parametrize('field,value', [
    ('storage_node_id', 'bad space'), ('storage_node_id', '-node'),
    ('storage_node_id', 'n' * 65), ('storage_node_id', 'node\n'),
    ('base_url', 'http://node:8000/api'), ('base_url', 'http://node?'),
    ('base_url', 'http://node#'), ('base_url', 'http://@node'),
    ('base_url', 'http://node\t'),
    ('base_url', 'http://node:abc'), ('base_url', 'http://node:0'),
    ('base_url', 'http://node:65536'),
])
def test_node_directory_matches_application_identity_and_origin_rules(field, value):
    import json
    app, compose = multinode_configs()
    node = {'storage_node_id': 'node-a', 'base_url': 'http://node-a:8000'}
    node[field] = value
    compose['services']['gateway']['environment']['WORKMATE_WORKER_NODES'] = json.dumps([node])
    assert 'worker_nodes_invalid' in errors(evaluate(app, compose))


@pytest.mark.parametrize('identity,url', [
    ('n' * 64, 'http://node-a:8000/'),
    ('node_A.1-2', 'https://[::1]:8443'),
])
def test_node_directory_accepts_application_identity_and_origin_boundaries(identity, url):
    import json
    app, compose = multinode_configs()
    compose['services']['gateway']['environment']['WORKMATE_WORKER_NODES'] = json.dumps([
        {'storage_node_id': identity, 'base_url': url}])
    assert not errors(evaluate(app, compose))


@pytest.mark.parametrize('prefix', ['SKILL', 'KNOWLEDGE'])
def test_artifact_bucket_is_separate_from_other_objects(prefix):
    app, compose = multinode_configs()
    runtime = compose['services']['runtime']['environment']
    runtime[prefix + '_S3_ENDPOINT'] = runtime['ARTIFACT_S3_ENDPOINT']
    runtime[prefix + '_S3_BUCKET'] = runtime['ARTIFACT_S3_BUCKET']
    assert 'artifact_bucket_overlap' in errors(evaluate(app, compose))


def test_artifact_storage_must_match_across_roles():
    app, compose = multinode_configs()
    compose['services']['gateway']['environment']['ARTIFACT_S3_BUCKET'] = 'other'
    assert 'artifact_storage_mismatch' in errors(evaluate(app, compose))


def test_local_artifact_initializer_matches_credentials_and_location():
    app, compose = multinode_configs()
    values = dict(ARTIFACT_S3_ENDPOINT='http://rustfs:9000', ARTIFACT_S3_BUCKET='artifacts',
                  ARTIFACT_S3_ACCESS_KEY='access', ARTIFACT_S3_SECRET_KEY='rustfs-secret')
    for role in ('gateway', 'runtime', 'storage-init'):
        compose['services'][role]['environment'].update(values)
    plan = evaluate(app, compose)
    assert not errors(plan)
    assert 'rustfs' in plan.dependencies
    assert plan.init_services == ('storage-init',)
    compose['services']['storage-init']['environment']['ARTIFACT_S3_BUCKET'] = 'wrong'
    assert 'artifact_bucket_init_mismatch' in errors(evaluate(app, compose))
