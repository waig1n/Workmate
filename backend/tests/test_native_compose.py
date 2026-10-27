"""Native default role graph is isolated and has no API/Worker dependency cycle."""
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]


def test_default_compose_uses_native_roles_and_separate_image_names():
    config=yaml.safe_load((ROOT/'docker-compose.yml').read_text())
    assert config['name']=='workmate'
    services=config['services']
    assert not {'product-api', 'agent-worker'} & services.keys()
    assert 'agentscope-runtime' not in services
    worker,api=services['runtime'],services['gateway']
    assert not worker.get('profiles')
    assert worker['build']['target']=='worker'
    assert worker['depends_on']['gateway']['condition']=='service_healthy'
    assert worker['restart']=='unless-stopped'
    assert set(api['depends_on'])=={'postgres', 'storage-init'}
    assert api['depends_on']['storage-init']['condition']=='service_completed_successfully'
    assert not services['rustfs'].get('profiles')
    initializer=services['storage-init']
    assert not initializer.get('profiles')
    assert initializer['image']==worker['image']
    assert initializer['environment']['SKILL_S3_BUCKET']=='${SKILL_S3_BUCKET:-workmate-skills}'
    assert not {'rustfs-init', 'skill-bucket-init'} & services.keys()
    assert 'WORKMATE_SKILL_CACHE_DIR' in worker['environment']
    for key in ('ENDPOINT','BUCKET','ACCESS_KEY','SECRET_KEY'):
        assert api['environment']['SKILL_S3_'+key]==worker['environment']['SKILL_S3_'+key]
    assert api['environment']['RUNTIME_BASE_URL']=='${RUNTIME_BASE_URL:-http://runtime:8000}'
    assert services['storage-init']['image']==worker['image']
    for name in ('runtime','gateway','frontend'):
        assert services[name]['image'] == f'workmate-{name}:2.0'
    assert not worker.get('ports')
    assert 'native-product-data:/data' in worker['volumes']
    assert 'native-product-data:/data' in api['volumes']
    assert 'runtime-data' not in config['volumes']
    assert 'native-pg-data-v18:/var/lib/postgresql' in services['postgres']['volumes']
    assert 'product-data' not in config['volumes'] and 'pg-data-v18' not in config['volumes']
    assert '/health' in ' '.join(api['healthcheck']['test'])


def test_runtime_build_only_exposes_worker_execution_target():
    dockerfile=(ROOT/'runtime/Dockerfile').read_text()
    assert 'AS legacy' not in dockerfile
    assert 'workmate_runtime.main:app' not in dockerfile
    assert 'FROM runtime-base AS worker' in dockerfile
    assert dockerfile.rstrip().endswith('CMD ["python", "-m", "workmate_runtime.roles.worker"]')


def test_command_redis_is_durable_bounded_and_never_evicts_pending_work():
    config=yaml.safe_load((ROOT/'docker-compose.yml').read_text())
    redis=config['services']['redis']
    command=redis['command']
    assert command[command.index('--appendonly')+1]=='yes'
    assert command[command.index('--appendfsync')+1]=='everysec'
    assert command[command.index('--maxmemory')+1]=='512mb'
    assert command[command.index('--maxmemory-policy')+1]=='noeviction'
    assert 'command-redis-data:/data' in redis['volumes']
    assert 'command-redis-data' in config['volumes']


def test_compose_keeps_deployment_environment_without_policy_overrides():
    from dotenv import dotenv_values

    policies = set('''SKILL_STORAGE_TIMEOUT_SECONDS SKILL_STORAGE_REGION
        WORKMATE_REME_MAX_INSTANCES REME_ENABLED ARTIFACT_DELIVERY_MODE
        SKILLHUB_CN_ENABLED SKILLHUB_CN_BASE_URL SKILLHUB_CN_REQUEST_TIMEOUT_SECS
        SKILLHUB_CN_MAX_RETRIES GITHUB_MCP_HUB_ENABLED WORKMATE_SANDBOX_PROVIDER
        WORKMATE_WORKSPACE_WARM_TTL_SECONDS WORKMATE_WORKSPACE_SWEEP_INTERVAL_SECONDS
        WORKMATE_WORKSPACE_MAX_WARM_PER_WORKER ARTIFACT_MAX_FILE_BYTES
        ARTIFACT_MAX_FILES_PER_RUN ARTIFACT_STRICT_DELIVERY WORKMATE_WORKER_CAPACITY
        WORKMATE_WORKER_LEASE_SECONDS WORKMATE_WORKER_DRAIN_SECONDS WORKMATE_WORKER_STARTUP_TIMEOUT_SECONDS
        JWT_ALGORITHM JWT_EXPIRE_HOURS REGISTRATION_MODE WORKSPACE_ASSISTANT_ENABLED
        EVENT_QUEUE_CAPACITY TASK_MAX_OUTSTANDING TASK_MAX_OUTSTANDING_PER_USER
        RUNTIME_TIMEOUT_SECS SESSION_LOCK_TTL_SECS ARTIFACT_PREVIEW_EXTENSIONS
        ARTIFACT_FORCE_DOWNLOAD_EXTENSIONS'''.split())
    config = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    for role in ('gateway', 'runtime'):
        service = config['services'][role]
        assert not policies & service['environment'].keys()
        assert not service.get('env_file')
        assert {'DATABASE_URL', 'SKILL_S3_SECRET_KEY', 'ARTIFACT_STORAGE_DIR'} <= service['environment'].keys()
        assert './config/workmate.yaml:/app/config/workmate.yaml:ro' in service['volumes']
    assert not policies & dotenv_values(ROOT / '.env.example').keys()
    assert 'JWT_SECRET' not in config['services']['runtime']['environment']
    assert 'E2B_API_KEY' not in config['services']['gateway']['environment']
