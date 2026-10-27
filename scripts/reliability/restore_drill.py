"""Owned local cold-volume restore drill; deterministic records, no application writers.

Exercises PG product records, Redis Streams, RustFS objects, Qdrant points and
Workspace/ReMe bytes. This is data recovery, not end-to-end approval resumption.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from .cold_backup import backup, docker, inventory, restore, local_daemon
from .cleanup import attempt, restore_targets, remaining_resources, source_hashes

ROOT = Path(__file__).resolve().parents[2]


def compose(path, project, *args):
    return docker('compose', '-p', project, '-f', str(path), *args)


def configuration(images, volume_names=None):
    roles = {'postgres': ('/var/lib/postgresql', 5432), 'redis': ('/data', 6379),
             'rustfs': ('/data', 9000), 'qdrant': ('/qdrant/storage', 6333)}
    services = {}
    for role, (target, port) in roles.items():
        services[role] = {'image': images[role], 'volumes': [f'{role}:{target}'],
                          'ports': [f'127.0.0.1::{port}']}
    services['postgres']['environment'] = {'POSTGRES_HOST_AUTH_METHOD': 'trust', 'POSTGRES_DB': 'drill'}
    services['redis']['command'] = ['redis-server', '--appendonly', 'yes', '--appendfsync', 'always']
    services['rustfs']['environment'] = {'RUSTFS_ACCESS_KEY': 'drill-only-access',
        'RUSTFS_SECRET_KEY': 'drill-only-secret-000000', 'RUSTFS_CONSOLE_ENABLE': 'false'}
    # RustFS's image runs as an unprivileged UID; use root only for these disposable volumes.
    services['rustfs']['user'] = '0:0'
    services['files'] = {'image': images['helper'], 'volumes': ['worker:/worker', 'product:/product'],
                         'command': ['true']}
    volumes = {key: {} for key in [*roles, 'worker', 'product']}
    if volume_names:
        volumes = {key: {'external': True, 'name': volume_names[key]} for key in volumes}
    return {'services': services, 'volumes': volumes}


def port(path, project, role, number):
    return compose(path, project, 'port', role, str(number)).rsplit(':', 1)[1]


def endpoints(path, project):
    return {'pg': 'postgresql://postgres@127.0.0.1:' + port(path, project, 'postgres', 5432) + '/drill',
            'redis': 'redis://127.0.0.1:' + port(path, project, 'redis', 6379) + '/0',
            's3': 'http://127.0.0.1:' + port(path, project, 'rustfs', 9000),
            'qdrant': 'http://127.0.0.1:' + port(path, project, 'qdrant', 6333)}


def wait_ready(ends):
    import asyncpg
    import boto3
    import httpx
    from redis import Redis
    s3 = boto3.client('s3', endpoint_url=ends['s3'], aws_access_key_id='drill-only-access',
                      aws_secret_access_key='drill-only-secret-000000', region_name='us-east-1')
    async def pg():
        connection = await asyncpg.connect(ends['pg'], timeout=2)
        await connection.close()
    deadline = time.monotonic() + 90
    while True:
        try:
            asyncio.run(pg())
            with Redis.from_url(ends['redis']) as client:
                assert client.ping()
            s3.list_buckets()
            assert httpx.get(ends['qdrant'] + '/collections', timeout=2).status_code == 200
            return s3
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(.5)


async def product_records(dsn, *, seed):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy import text
    from workmate_product.persistence.models import Base, User, Scope, Agent, Conversation, Task, TaskMessage, TaskEvent
    from app.auth.security import hash_password, verify_password
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'))
    try:
        if seed:
            async with engine.begin() as db:
                await db.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine).begin() as db:
                db.add(User(id='u', email='restore@example.invalid', password_hash=hash_password('local-drill-password'), display_name='DR'))
                db.add(Scope(id='scope', kind='organization', name='DR', normalized_name='dr'))
                await db.flush()
                db.add(Agent(id='a', slug='dr', display_name='DR', scope_id='scope', created_by='u'))
                await db.flush()
                db.add(Conversation(id='c', user_id='u', agent_id='a'))
                await db.flush()
                db.add(Task(id='t', conversation_id='c', title='Data restore fixture', status='awaiting_permission', run_id='reply'))
                await db.flush()
                db.add(TaskMessage(id='m', task_id='t', conversation_id='c', role='assistant', content={'text': 'Please approve writing the report'}))
                db.add(TaskEvent(task_id='t', conversation_id='c', seq=1, event_type='approval_required', payload={
                    'tool_call_id': 'write-1', 'parameters': {'file_path': 'report.txt', 'content': '精确参数\n'}}))
        async with engine.connect() as db:
            records = {}
            for table in ('users', 'scopes', 'agents', 'conversations', 'tasks', 'task_messages', 'task_events'):
                rows = (await db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY 1'))).scalars().all()
                records[table] = hashlib.sha256('\n'.join(rows).encode()).hexdigest()
            password = (await db.execute(text("SELECT password_hash FROM users WHERE id='u'"))).scalar_one()
            assert verify_password('local-drill-password', password)
            assert (await db.execute(text('SELECT count(*) FROM runtime_commands'))).scalar_one() == 0
            return records
    finally:
        await engine.dispose()


def data(ends, *, seed):
    import httpx
    from redis import Redis
    s3 = wait_ready(ends)
    # Non-ASCII object bytes verify that backup and restore preserve UTF-8 content.
    keys = {'skills/pkg': b'skill-package', 'knowledge/document': '知识原文'.encode(),
            'artifacts/report': b'final-report', 'tool-content/args': '精确参数\n'.encode()}
    if seed:
        s3.create_bucket(Bucket='restore-drill')
        for key, body in keys.items():
            s3.put_object(Bucket='restore-drill', Key=key, Body=body)
        with Redis.from_url(ends['redis']) as client:
            client.xadd('drill:commands', {'id': 'command-1'}, id='1-0')
            client.xgroup_create('drill:commands', 'workers', id='0')
            client.xreadgroup('workers', 'worker-1', {'drill:commands': '>'})
        response = httpx.put(ends['qdrant'] + '/collections/drill', json={'vectors': {'size': 3, 'distance': 'Cosine'}})
        response.raise_for_status()
        httpx.put(ends['qdrant'] + '/collections/drill/points?wait=true', json={'points': [
            {'id': 1, 'vector': [1., 0., 0.], 'payload': {'source': 'knowledge/document'}}]}).raise_for_status()
    objects = {}
    for key, body in keys.items():
        stored = s3.get_object(Bucket='restore-drill', Key=key)['Body'].read()
        assert stored == body
        objects[key] = hashlib.sha256(stored).hexdigest()
    with Redis.from_url(ends['redis'], decode_responses=True) as client:
        assert client.xrange('drill:commands') == [('1-0', {'id': 'command-1'})]
        assert client.xpending('drill:commands', 'workers')['pending'] == 1
    points = httpx.post(ends['qdrant'] + '/collections/drill/points/query',
                       json={'query': [1., 0., 0.], 'with_payload': True}).json()['result']['points']
    assert points[0]['id'] == 1 and points[0]['payload']['source'] == 'knowledge/document'
    return {'product_rows': asyncio.run(product_records(ends['pg'], seed=seed)), 'objects': objects,
            'redis_pending_preserved': True, 'qdrant_query': True, 'password_verification': True}


def files(image, worker, product, *, seed):
    code = """
from pathlib import Path
import hashlib,json
from workmate_runtime.assembly.posix_workspace_ownership import PosixWorkspaceOwnership
from workmate_runtime.assembly.workspace_ownership import WorkspaceOwnershipError
root=Path('/worker/workspaces'); identity={'conversation_id':'c','storage_node_id':'drill'}
seed=SEED
if seed:
    lease=PosixWorkspaceOwnership.acquire(root,'c',identity)
    (lease.path/'report.txt').write_text('workspace-report')
    lease.close(clean=True)
    Path('/worker/reme').mkdir()
    Path('/worker/reme/memory').write_text('remember-me')
    Path('/product/artifact').write_text('local-artifact')
else:
    try: PosixWorkspaceOwnership.acquire(root,'c',identity)
    except WorkspaceOwnershipError: pass
    else: raise AssertionError('New directory must not authorize old execution')
values={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for base in ('/worker','/product') for p in Path(base).rglob('*') if p.is_file() and not p.name.endswith('.lock')}
print(json.dumps(values,sort_keys=True))
""".replace('SEED', repr(seed))
    return json.loads(docker('run', '--rm', '--network', 'none', '--entrypoint', 'python',
        '--mount', f'type=volume,src={worker},dst=/worker', '--mount', f'type=volume,src={product},dst=/product',
        '--mount', f'type=bind,src={ROOT}/runtime,dst=/source,readonly', '-e', 'PYTHONPATH=/source',
        '-e', 'PYTHONDONTWRITEBYTECODE=1', image, '-c', code))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print('Prepared: creates owned local containers/volumes; no existing services are used.')
        return
    local_daemon()
    for relative in ("runtime", "backend", "packages/product/src", "packages/contracts/src"):
        sys.path.insert(0, str(ROOT / relative))
    os.umask(0o077)
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    (root/'source.json').write_text(json.dumps(source_hashes(ROOT),indent=2)+'\n')
    owner = 'drill-' + uuid.uuid4().hex[:12]
    images = {role: docker('image', 'inspect', ref, '--format', '{{.Id}}') for role, ref in {
        'postgres': 'postgres:18-alpine', 'redis': 'redis:7-alpine', 'rustfs': 'rustfs/rustfs:1.0.0-rc.6',
        'qdrant': 'qdrant/qdrant:v1.19.1', 'helper': 'alpine:latest', 'python': 'workmate-runtime:2.0'}.items()}
    source = root / 'source.json'; target = root / 'target.json'
    source.write_text(json.dumps(configuration(images)))
    restored = {}; result = {'passed': False, 'images': images, 'owner': owner}
    started = time.monotonic()
    try:
        compose(source, owner, 'up', '-d')
        primary = endpoints(source, owner)
        before = data(primary, seed=True)
        values = inventory([source], owner)
        names = {key: f'{owner}_{key}' for key in ('postgres','redis','rustfs','qdrant','worker','product')}
        file_before = files(images['python'], names['worker'], names['product'], seed=True)
        # Demonstrate backup fails before any bundle when a data service is alive.
        try: backup(values, root/'must-not-exist', images['helper'])
        except ValueError: pass
        else: raise AssertionError('Live backup accepted')
        assert not (root/'must-not-exist').exists()
        compose(source, owner, 'stop', '-t', '30')
        backup_started = time.monotonic()
        backup(values, root/'bundle', images['helper'])
        restore_started = time.monotonic()
        restored = restore(root/'bundle', images['helper'])
        targets = {key: restored['volumes'][value] for key,value in names.items()}
        target.write_text(json.dumps(configuration(images, targets)))
        compose(target, owner+'-restored', 'up', '-d')
        after = data(endpoints(target, owner+'-restored'), seed=False)
        assert after == before
        file_after = files(images['python'], targets['worker'], targets['product'], seed=False)
        assert file_after == file_before
        result.update(passed=True, checks=after, file_hashes=file_after,
            backup_seconds=restore_started-backup_started, restore_seconds=time.monotonic()-restore_started,
            workspace_rebind='correctly refused; no tools executed',
            scope='seeded product rows and storage bytes; not HTTP login, approval resume, Skill execution or RAG pipeline')
    finally:
        errors = []
        for config, project in ((target,owner+'-restored'), (source,owner)):
            if config.exists():
                attempt(errors, project + ' logs', lambda config=config, project=project:
                    (root/(project+'.log')).write_text(compose(config,project,'logs','--no-color')))
                attempt(errors, project + ' down', lambda config=config, project=project:
                    compose(config,project,'down','--volumes'))
        # restore() can fail after creating some volumes; its persisted ledger is
        # authoritative even when it never returned a result to this caller.
        targets = attempt(errors, 'read restore ledgers', lambda: restore_targets(root/'bundle', docker))
        for volume in targets or []:
            attempt(errors, 'remove ' + volume, lambda volume=volume: docker('volume','rm',volume))
        identities = [p.stem for p in (root/'bundle').glob('workmate-restore-*.json')]
        remaining = attempt(errors, 'verify remaining resources', lambda:
            remaining_resources(docker, [owner, owner+'-restored'], identities))
        result['elapsed_seconds'] = time.monotonic()-started
        result['cleanup_errors'] = errors
        result['cleanup_inventory'] = remaining
        result['cleanup_remaining'] = remaining['containers'] if remaining is not None else None
        result['cleanup_volumes'] = remaining['volumes'] if remaining is not None else None
        result['passed'] = result['passed'] and not errors and remaining is not None and not any(remaining.values())
        (root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    assert result['passed'], result
    print(json.dumps({'passed':True,'evidence':str(root)}))


if __name__ == '__main__':
    main()
