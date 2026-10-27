"""Read-only probes executed in existing containers, with no credentials in argv."""

DATABASE = """
import asyncio, asyncpg
from alembic.config import Config
from alembic.script import ScriptDirectory
async def check():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://'), timeout=10)
    try:
        current = {row['version_num'] for row in await conn.fetch('select version_num from alembic_version')}
    finally:
        await conn.close()
    heads = set(ScriptDirectory.from_config(Config('/app/alembic.ini')).get_heads())
    print('database revisions: ' + ','.join(sorted(current)))
    source = set(EXPECTED_HEADS) if EXPECTED_HEADS else heads
    print('image heads: ' + ','.join(sorted(heads)) + '; source heads: ' + ','.join(sorted(source)))
    if current != heads or heads != source:
        print('migration mismatch; use a fresh database for the initial release; inspect gateway startup logs')
        raise RuntimeError('migration mismatch')
asyncio.run(check())
"""
DEPENDENCIES = """
import asyncio, socket
from urllib.parse import urlsplit
from workmate_runtime.config import load_runtime_settings
settings = load_runtime_settings()
async def check():
    import aioboto3
    from botocore.config import Config
    if not all(getattr(settings, 'skill_s3_'+name, '') for name in ('endpoint', 'bucket', 'access_key', 'secret_key')):
        raise RuntimeError('Skill storage configuration is missing')
    async with aioboto3.Session().client('s3', endpoint_url=settings.skill_s3_endpoint,
            aws_access_key_id=settings.skill_s3_access_key, aws_secret_access_key=settings.skill_s3_secret_key,
            region_name=settings.skill_storage.region,
            config=Config(s3={'addressing_style':'path'},connect_timeout=10,read_timeout=10,retries={'max_attempts':0})) as s3:
        await s3.head_bucket(Bucket=settings.skill_s3_bucket)
    print('skills: S3 bucket access OK (privacy and publication not tested)')
    if not hasattr(settings, 'knowledge'):
        print('Runtime image lacks current knowledge configuration schema; rebuild required')
        raise RuntimeError('outdated runtime image')
    if settings.knowledge.enabled:
        from qdrant_client import QdrantClient
        import aioboto3
        from botocore.config import Config
        client = QdrantClient(url=settings.knowledge_qdrant_url, api_key=settings.knowledge_qdrant_api_key, timeout=10)
        try:
            client.get_collections()
        finally:
            client.close()
        async with aioboto3.Session().client('s3', endpoint_url=settings.knowledge_s3_endpoint, aws_access_key_id=settings.knowledge_s3_access_key, aws_secret_access_key=settings.knowledge_s3_secret_key, config=Config(connect_timeout=10,read_timeout=10,retries={'max_attempts':0})) as s3:
            await s3.head_bucket(Bucket=settings.knowledge_s3_bucket)
        print('knowledge: Qdrant authentication and S3 bucket access OK')
    else:
        print('knowledge: disabled')
    import yaml
    with open(os.environ['WORKMATE_CONFIG_FILE']) as file:
        config = yaml.safe_load(file)
    web = config.get('product',{}).get('web',{})
    if web.get('enabled',False):
        failures = 0
        for tool in web.get('tools',[]):
            label = 'web: ' + tool.get('name', 'provider') + ' (' + tool['base_url_env'] + ')'
            try:
                parsed = urlsplit(os.environ[tool['base_url_env']])
                with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme=='https' else 80)),timeout=10):
                    pass
                print(label + ' TCP OK')
            except Exception as exc:
                failures += 1
                print(label + ' ' + type(exc).__name__)
        if failures:
            raise RuntimeError('Web dependency probes failed')
        print('web: endpoint TCP connectivity OK (provider API and search quality not tested)')
asyncio.run(check())
"""


def guarded(body):
    import textwrap

    return (
        "import os, sys, urllib.request\nurllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))\ntry:\n"
        + textwrap.indent(body.strip(), "    ")
        + "\nexcept Exception as exc:\n    print('probe failed: ' + type(exc).__name__ + (' HTTP ' + str(exc.code) if hasattr(exc, 'code') else ''))\n    sys.exit(1)\nprint('probe OK')\n"
    )
