"""Explicit auxiliary indexing lifecycle inside the Worker process.

Only knowledge storage/manager/indexing components are reused. This context never
constructs App, ChatService, a Session manager or an HTTP router. Index leases and
document status remain independent of Product Task and Reply execution.
"""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager


@asynccontextmanager
async def knowledge_resources(*,storage,message_bus,manager,blob_store,parsers,node_id,settings=None):
    from agentscope.app._service import IndexWorker,IndexTaskConsumer,IndexSweeper
    async with AsyncExitStack() as stack:
        for component in (storage,message_bus,blob_store,manager):
            # Some pinned components allocate resources before completing enter.
            # Register first so startup failure/cancellation also disposes them.
            stack.push_async_callback(component.__aexit__,None,None,None)
            await component.__aenter__()
        manager.workmate_knowledge_management = None
        if settings is not None:
            from agentscope.app._service import KnowledgeBaseService, ResourceAccessService
            from agentscope.app.access import DenyAllResourceAccessPolicy
            from .management import KnowledgeManagement
            service = KnowledgeBaseService(storage, manager, blob_store, message_bus,
                ResourceAccessService(storage, DenyAllResourceAccessPolicy()))
            manager.workmate_knowledge_management = KnowledgeManagement(settings, storage,
                message_bus, blob_store, service)
        worker=IndexWorker(storage=storage,blob_store=blob_store,
            knowledge_base_manager=manager,parsers=parsers,node_id=node_id)
        consumer=IndexTaskConsumer(message_bus=message_bus,worker=worker)
        # Register cleanup before startup, including partial subscription failure
        # or cancellation while waiting for initial drain.
        stack.push_async_callback(consumer.__aexit__,None,None,None)
        await consumer.__aenter__()
        if consumer._task is None or consumer._task.done():
            raise RuntimeError('Knowledge index subscription did not start')
        sweeper=IndexSweeper(storage=storage,message_bus=message_bus)
        stack.push_async_callback(sweeper.stop)
        await sweeper.start()
        if sweeper._task is None or sweeper._task.done():
            raise RuntimeError('Knowledge index sweeper did not start')
        if consumer._task.done():
            raise RuntimeError('Knowledge index subscription stopped during startup')
        # Consumer intentionally swallows Redis errors in the pinned library.
        # A completed task is therefore failure even when it has no exception.
        # The role observes this Future alongside Worker execution and shuts down
        # before reporting itself available with a dead indexing subscription.
        failure=asyncio.get_running_loop().create_future()
        manager.workmate_indexing_failure=failure
        async def supervise():
            done,_=await asyncio.wait((consumer._task,sweeper._task),return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():
                    task.exception()  # Consume exceptions without leaking details.
            if not failure.done():
                failure.set_result(RuntimeError('Knowledge indexing background stopped'))
        supervisor=asyncio.create_task(supervise(),name='workmate-knowledge-supervisor')
        async def stop_supervisor():
            supervisor.cancel()
            await asyncio.gather(supervisor,return_exceptions=True)
            if not failure.done():
                failure.cancel()
        stack.push_async_callback(stop_supervisor)
        try:
            yield manager
        finally:
            manager.workmate_knowledge_management = None


async def initialize_knowledge_schema(engine):
    """Serialize pinned auxiliary schema bootstrap in the target database.

    This is create-if-absent, not an upgrade or Product migration. The lock and
    DDL share a transaction, so failure/cancellation releases both together.
    """
    from sqlalchemy import text
    from agentscope.app.storage._sql._tables import _Base
    async with engine.begin() as connection:
        await connection.execute(text(
            "SELECT pg_advisory_xact_lock(hashtextextended('workmate:knowledge-schema', 0))"))
        await connection.run_sync(_Base.metadata.create_all)


@asynccontextmanager
async def configured_knowledge_resources(settings,*,node_id):
    if not settings.knowledge.enabled:
        yield None
        return
    from agentscope.app.storage import AsyncSQLAlchemyStorage
    from agentscope.app.message_bus import RedisMessageBus
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from .components import build_knowledge_components
    from redis.asyncio import ConnectionPool
    async with AsyncExitStack() as stack:
        if make_url(settings.as_database_url).get_backend_name() == 'postgresql':
            engine = create_async_engine(settings.as_database_url)
            stack.push_async_callback(engine.dispose)
            await initialize_knowledge_schema(engine)
            storage = AsyncSQLAlchemyStorage(settings.as_database_url,
                engine=engine, create_tables=False)
        else:
            # Preserve the upstream SQLite foreign-key connection setup used by
            # local tests; multi-replica deployments require PostgreSQL.
            storage = AsyncSQLAlchemyStorage(settings.as_database_url)
        components=build_knowledge_components(settings,storage)
        # RedisMessageBus takes host, not URL. Keep auth, DB and TLS via its pool.
        pool=ConnectionPool.from_url(settings.as_redis_url,decode_responses=True)
        pool.connection_kwargs["decode_responses"]=True
        stack.push_async_callback(pool.aclose)
        async with knowledge_resources(storage=storage,message_bus=RedisMessageBus(connection_pool=pool),
                manager=components['knowledge_base_manager'],blob_store=components['blob_store'],
                parsers=components['knowledge_parsers'],node_id=node_id,settings=settings) as manager:
            yield manager
