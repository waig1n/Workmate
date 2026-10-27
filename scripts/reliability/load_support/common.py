import importlib, json, os
from pathlib import Path

def guard(label):
    origins={}
    expected = {
        'app': '/source/backend/',
        'workmate_runtime': '/source/runtime/',
        'workmate_product': '/source/packages/product/src/',
        'workmate_runtime.execution.composition': '/source/runtime/',
        'workmate_runtime.roles.worker': '/source/runtime/',
        'workmate_product.application.control_commands': '/source/packages/product/src/',
        'workmate_contracts': '/source/packages/contracts/src/',
        'app.services.durable_runtime': '/source/backend/',
    }
    for name, prefix in expected.items():
        path = Path(importlib.import_module(name).__file__).resolve()
        assert str(path).startswith(prefix) and path.suffix == '.py', (name, str(path))
        origins[name] = str(path)
    Path('/evidence/'+label+'-origins.json').write_text(json.dumps(origins,indent=2))

def settings():
    from workmate_runtime.config import RuntimeSettings
    from workmate_product.config.worker import WorkerPolicy
    from workmate_runtime.roles.settings import WorkerRoleSettings
    return WorkerRoleSettings(runtime=RuntimeSettings(reme_enabled=False,
        workspace_lifecycle={'max_warm_per_worker':2,'warm_ttl_seconds':30,'sweep_interval_seconds':2},
        knowledge={'enabled':os.environ.get('WORKMATE_LOAD_WORKLOAD') in ('rag','mixed')},worker=WorkerPolicy(capacity=int(os.environ.get('WORKMATE_LOAD_WORKER_CAPACITY','2'))),
        coordination_redis_url=os.environ['WORKMATE_COORDINATION_REDIS_URL'],
        coordination_namespace=os.environ['WORKMATE_LOAD_NAMESPACE']),
        database_url=os.environ['DATABASE_URL'], storage_node_id='node',
        workspace_dir='/shared/workspaces',artifact_dir='/shared/artifacts')

def fixture_blobs():
    # Only a private deterministic fixture blob directory is shared by roles.
    import workmate_product.skill_storage as module
    from skill_fixture import FixtureStore
    module.store_from_settings=lambda _:FixtureStore()


def chunk(key,index):
    return f"{key}:{index:06d}:" + "x"*24

def parameters():
    return (10, .02) if os.environ.get('WORKMATE_LOAD_MODE')=='smoke' else (600,.02)

JWT_SECRET='fixture-load-only-key-not-a-real-secret-000000'


def configure_database(database, store):
    """Preserve production pool/session policy; replace only fixture blob storage."""
    engine = database.get_engine()
    factory = database.get_sessionmaker()
    factory.configure(info={**factory.kw.get('info', {}), 'skill_blob_store': store})
    return engine


async def wait_handshakes(handshakes, consumers):
    """Surface SSE failure immediately; the caller retains its original deadline."""
    import asyncio
    waiters = [asyncio.create_task(event.wait()) for event in handshakes]
    try:
        while True:
            for consumer in consumers:
                if consumer.done():
                    consumer.result()  # Preserve the original HTTP/stream exception.
                    raise RuntimeError('SSE consumer ended before submission')
            if all(event.is_set() for event in handshakes):
                return
            await asyncio.wait([task for task in waiters if not task.done()] + consumers,
                               return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
