"""Actual Gateway role over a caller-owned test schema and loopback socket."""
import argparse
import os
import re
import socket

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--schema', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--socket-fd', required=True, type=int)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'live_projection_[a-f0-9]{32}', args.schema):
        parser.error('An explicitly owned projection test schema is required')
    dsn = os.environ['WORKMATE_TEST_POSTGRES_DSN']
    redis = os.environ['WORKMATE_TEST_REDIS_URL']
    os.environ.pop('GITHUB_TOKEN', None)
    os.environ.update(
        WORKMATE_CONFIG_FILE=args.config,
        WORKMATE_COORDINATION_REDIS_URL=redis,
        WORKMATE_COORDINATION_NAMESPACE=args.namespace,
        JWT_SECRET='local-dual-gateway-signing-key-0123456789',
        WORKSPACE_ASSISTANT_ENABLED='false',
        RUNTIME_INTERNAL_TOKEN='local-gateway-fixture',
        WORKMATE_WORKER_STORAGE_NODE_ID='node',
    )
    from app.config import Settings
    Settings.model_config['env_file'] = None
    import app.db.engine as database
    from product_catalog_fakes import MemoryBlobStore
    # Only Skill object storage is a fixture. HTTP/auth, both publishers,
    # projection, Redis reader and role shutdown are the production lifespan.
    database._engine = create_async_engine(
        dsn.replace('postgresql://', 'postgresql+asyncpg://'),
        connect_args={'server_settings': {'search_path': args.schema,
            'application_name': f'q4_gateway_{os.getpid()}'}},
    )
    database._sessionmaker = async_sessionmaker(database._engine,
        expire_on_commit=False, info={'skill_blob_store': MemoryBlobStore()})
    from app.main import build_app
    import uvicorn
    endpoint = socket.socket(fileno=args.socket_fd)
    if endpoint.getsockname()[0] != '127.0.0.1':
        parser.error('Only a prebound loopback socket is allowed')
    server = uvicorn.Server(uvicorn.Config(build_app(), log_level='warning',
        timeout_graceful_shutdown=2))
    import asyncio
    asyncio.run(server.serve(sockets=[endpoint]))


if __name__ == '__main__':
    main()
