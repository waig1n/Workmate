"""Native Worker role: durable commands, Core assembly and auxiliary resources."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import logging
import os
import threading
import traceback
import uuid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from workmate_runtime.execution.composition import compose_worker
from workmate_runtime.rag.lifecycle import configured_knowledge_resources
from workmate_runtime.rag.resolver import WorkerKnowledgeResolver
from workmate_runtime.roles.settings import load_worker_role_settings
from workmate_runtime.sandbox.factory import create_sandbox_provider

logger=logging.getLogger(__name__)


def shutdown_budget_seconds(settings):
    # Existing execution drain plus a bounded allowance for resource cleanup.
    return settings.runtime.worker.drain_seconds + 30


def log_worker_failure(stage, error):
    """Log frame identities, never exception text, source lines, or locals."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        frames = ' -> '.join(
            f'{os.path.basename(frame.f_code.co_filename)}:{line}:{frame.f_code.co_name}'
            for frame, line in traceback.walk_tb(error.__traceback__))
        logger.error('Worker %s failed (%s) frames=%s', stage,
                     type(error).__name__, frames or '-')
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)


async def verify_product_schema(engine):
    # The Worker cannot migrate the Product database. Missing tables fail before
    # command admission rather than after claiming a real execution.
    required=('runtime_execution_specs','runtime_events','runtime_snapshots',
        'runtime_commands','runtime_delivery_captures','runtime_sandbox_attestations',
        'skill_artifacts','skill_upload_sources','runtime_permission_upgrades',
        'mcp_secret_revisions','capability_mcp_revisions','runtime_external_receipts','runtime_memory_namespaces',
        'runtime_delivery_seals','conversation_retirements','runtime_suspensions','command_deliveries','command_delivery_quarantines',
        'workspace_residencies','workspace_release_requests','conversation_event_logs','realtime_business_outbox')
    async with engine.connect() as db:
        for name in required:
            if await db.scalar(text('SELECT to_regclass(:name)'),{'name':name}) is None:
                raise RuntimeError('Product schema is not ready for the native Worker')
        # Verify required storage fields in addition to table availability.
        await db.execute(text('SELECT storage_key,size_bytes FROM skill_artifacts LIMIT 0'))
        await db.execute(text('SELECT instance_id,evidence FROM runtime_sandbox_attestations LIMIT 0'))
        await db.execute(text('SELECT purged_at,purged_by FROM conversation_retirements LIMIT 0'))
        await db.execute(text('SELECT resume_worker_id,resume_epoch,resume_command_id FROM runtime_suspensions LIMIT 0'))
        await db.execute(text('SELECT holder_worker_id,workspace_generation,hold_version,status FROM workspace_residencies LIMIT 0'))
        await db.execute(text('SELECT expected_worker_id,expected_hold_version,cleanup_receipt,status FROM workspace_release_requests LIMIT 0'))
        await db.execute(text('SELECT snapshot_version,snapshot_reply_id,snapshot_execution_epoch,covered_cursor,'
            'snapshot_business_seq,snapshot_phase,snapshot_incomplete FROM task_messages LIMIT 0'))
        await db.execute(text('SELECT business_seq FROM conversations LIMIT 0'))
        await db.execute(text('SELECT business_seq FROM product_events LIMIT 0'))
        from sqlalchemy import inspect
        attestation_key = await db.run_sync(lambda connection:
            inspect(connection).get_pk_constraint('runtime_sandbox_attestations')['constrained_columns'])
        if set(attestation_key) != {'task_id','execution_epoch'}:
            raise RuntimeError('Product schema lacks per-epoch sandbox attestations')


async def serve_worker(settings,shutdown,*,on_ready=None):
    policy=settings.runtime.worker
    engine=create_async_engine(settings.database_url,pool_pre_ping=True)
    try:
        async with AsyncExitStack() as resources:
            async with asyncio.timeout(policy.startup_timeout_seconds):
                await verify_product_schema(engine)
                sandbox_provider = create_sandbox_provider(settings.runtime.sandbox)
                await sandbox_provider.probe()
                manager=await resources.enter_async_context(configured_knowledge_resources(
                    # Index lease renew/release is fenced by processing_node, so
                    # replicas sharing storage must never share this owner token.
                    settings.runtime,node_id='index-worker-' + uuid.uuid4().hex))
                resolver=(WorkerKnowledgeResolver(async_sessionmaker(engine,expire_on_commit=False),manager,
                    timeout=settings.runtime.knowledge.search_timeout_secs) if manager is not None else None)
                memory=None
                if settings.runtime.reme_enabled:
                    from workmate_runtime.assembly.memory import WorkerMemoryManager,bind_memory_node
                    from workmate_runtime.middlewares import build_chat_model_from_profile,build_embedding_model_from_settings
                    sessions=async_sessionmaker(engine,expire_on_commit=False)
                    async def placement(user_id,agent_id):
                        await bind_memory_node(sessions,user_id,agent_id,settings.storage_node_id)
                    memory=WorkerMemoryManager(settings.runtime.reme_basedir,
                        build_chat_model_from_profile(settings.runtime),build_embedding_model_from_settings(settings.runtime),
                        bind_node=placement,max_instances=settings.runtime.reme_max_instances)
                    resources.push_async_callback(memory.close)
                if not settings.runtime.coordination_redis_url:
                    raise RuntimeError('WORKMATE_COORDINATION_REDIS_URL is required for command delivery')
                from redis.asyncio import Redis
                from workmate_product.coordination.command_queue import RedisCommandQueue
                redis_client=await resources.enter_async_context(Redis.from_url(
                    settings.runtime.coordination_redis_url,decode_responses=True,
                    socket_connect_timeout=5,socket_timeout=5))
                command_queue=RedisCommandQueue(redis_client,namespace=settings.runtime.coordination_namespace)
                from redis.asyncio.retry import Retry
                from redis.backoff import NoBackoff
                from workmate_product.realtime.composition import create_realtime_log
                realtime_client = await resources.enter_async_context(Redis.from_url(
                    settings.runtime.coordination_redis_url, decode_responses=True,
                    retry=Retry(NoBackoff(), 0), retry_on_timeout=False,
                    socket_connect_timeout=5, socket_timeout=5))
                realtime_log = create_realtime_log(realtime_client,
                    namespace=settings.runtime.coordination_namespace,
                    policy=settings.runtime.realtime)
                wakeup_bus = None
                if settings.runtime.coordination_redis_url:
                    from workmate_product.coordination import WakeupBus
                    wakeup_bus = await resources.enter_async_context(
                        WakeupBus(settings.runtime.coordination_redis_url))
                app=compose_worker(engine,settings.runtime,storage_node_id=settings.storage_node_id,
                    workspace_dir=settings.workspace_dir,artifact_dir=settings.artifact_dir,
                    max_file_bytes=settings.runtime.artifact_max_file_bytes,
                    max_files=settings.runtime.artifact_max_files_per_run,
                    strict_delivery=settings.runtime.artifact_strict_delivery,
                    capacity=policy.capacity,lease_seconds=policy.lease_seconds,
                    drain_seconds=policy.drain_seconds,knowledge_resolver=resolver,memory_manager=memory,
                    sandbox_provider=sandbox_provider,wakeup_bus=wakeup_bus,command_queue=command_queue,
                    realtime_log=realtime_log)
            app.knowledge_management = getattr(manager, 'workmate_knowledge_management', None)
            running=asyncio.create_task(app.serve(shutdown))
            ready=asyncio.create_task(app.service.ready.wait())
            indexing_failure=getattr(manager,'workmate_indexing_failure',None)
            try:
                watched=[running,ready]
                if indexing_failure is not None:
                    watched.append(indexing_failure)
                done,_=await asyncio.wait(watched,return_when=asyncio.FIRST_COMPLETED)
                if indexing_failure in done:
                    raise indexing_failure.result()
                if running in done:
                    await running
                    return
                if on_ready is not None:
                    on_ready(app)
                logger.info('Native Worker ready for durable commands')
                if indexing_failure is not None:
                    done,_=await asyncio.wait((running,indexing_failure),return_when=asyncio.FIRST_COMPLETED)
                    if indexing_failure in done:
                        raise indexing_failure.result()
                await running
            finally:
                # Close HTTP admission before cancelling execution or closing resources.
                app.knowledge_management = None
                ready.cancel()
                if not running.done():
                    running.cancel()
                await asyncio.gather(ready,running,return_exceptions=True)
    finally:
        await engine.dispose()


def create_worker_app(settings, *, on_fatal=None):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    @asynccontextmanager
    async def lifespan(web):
        shutdown,ready=asyncio.Event(),asyncio.Event()
        web.state.worker_failed = False
        web.state.accepting_knowledge = False
        monitoring = None
        failure_logged = False

        def fatal():
            web.state.accepting_knowledge = False
            if not web.state.worker_failed:
                web.state.worker_failed = True
                if on_fatal is not None:
                    on_fatal()

        def report_finished():
            nonlocal failure_logged
            if failure_logged or not running.done():
                return
            failure_logged = True
            if running.cancelled():
                logger.error('Worker execution cancelled unexpectedly')
            elif running.exception() is not None:
                log_worker_failure('execution', running.exception())
            elif not shutdown.is_set():
                logger.error('Worker execution returned unexpectedly')

        def initialized(application):
            web.state.worker=application
            web.state.accepting_knowledge=True
            ready.set()

        async def monitor():
            while not shutdown.is_set():
                # Also observe readiness withdrawal while resource cleanup is
                # still pending; waiting only for running.done() can hang forever.
                if running.done() or not web.state.worker.service.ready.is_set():
                    fatal()
                    if running.done():
                        report_finished()
                    else:
                        logger.error('Worker execution readiness withdrawn unexpectedly')
                    return
                await asyncio.wait({running}, timeout=0.1)

        running=asyncio.create_task(serve_worker(settings,shutdown,on_ready=initialized))
        web.state.running=running
        waiting=asyncio.create_task(ready.wait())
        try:
            async with asyncio.timeout(settings.runtime.worker.startup_timeout_seconds):
                done,_=await asyncio.wait((running,waiting),return_when=asyncio.FIRST_COMPLETED)
                if running in done:
                    if running.cancelled():
                        raise RuntimeError('Worker cancelled before readiness')
                    await running
                    raise RuntimeError('Worker exited before readiness')
            monitoring=asyncio.create_task(monitor(), name='worker-role-supervisor')
            yield
        except Exception as error:
            fatal()
            log_worker_failure('lifecycle', error)
            raise RuntimeError('Worker role lifecycle failed; dependencies or state are unavailable') from None
        finally:
            # Mark normal shutdown before readiness is cleared by the draining
            # worker, so ordinary SIGTERM is not classified as execution failure.
            web.state.accepting_knowledge=False
            shutdown.set()
            waiting.cancel()
            if monitoring is not None:
                monitoring.cancel()
            await asyncio.gather(waiting, *([monitoring] if monitoring else []),
                                 return_exceptions=True)
            if not ready.is_set() and not running.done():
                running.cancel()
            done, _ = await asyncio.wait({running}, timeout=shutdown_budget_seconds(settings))
            if not done:
                fatal()
                running.cancel()
                logger.error('Worker resource cleanup exceeded shutdown budget')
            elif not running.cancelled() and running.exception() is not None:
                fatal()
                report_finished()

    web=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    from fastapi import HTTPException
    from workmate_runtime.service_auth import RuntimeServiceAuthMiddleware
    from workmate_runtime.rag.management_router import create_management_router
    web.add_middleware(RuntimeServiceAuthMiddleware, token=settings.runtime.internal_token)
    async def management():
        worker = getattr(web.state,'worker',None)
        if (not getattr(web.state,'accepting_knowledge',False) or worker is None
                or not worker.service.ready.is_set() or web.state.running.done() or web.state.worker_failed
                or getattr(worker,'knowledge_management',None) is None):
            raise HTTPException(503,'Knowledge management is unavailable')
        return worker.knowledge_management
    web.include_router(create_management_router(management))
    from workmate_runtime.mcp.probe_router import create_probe_router
    def probe_ready():
        worker = getattr(web.state, 'worker', None)
        return (worker is not None and worker.service.ready.is_set()
                and not web.state.running.done() and not web.state.worker_failed)
    web.include_router(create_probe_router(settings, probe_ready))
    @web.get('/health')
    async def health():
        worker=getattr(web.state,'worker',None)
        ready=worker is not None and worker.service.ready.is_set() and not web.state.running.done() and not web.state.worker_failed
        return JSONResponse({'role':'worker','ready':ready,
            'active_sessions':len(worker.service.active_sessions) if worker else 0,
            'blocked_sessions':len(worker.service.failures) if worker else 0},status_code=200 if ready else 503)
    @web.get('/workmate/node')
    async def node_status():
        # Protected by RuntimeServiceAuthMiddleware, unlike public liveness.
        worker=getattr(web.state,'worker',None)
        ready=worker is not None and worker.service.ready.is_set() and not web.state.running.done() and not web.state.worker_failed
        return JSONResponse({'role':'worker','ready':ready,'storage_node_id':settings.storage_node_id,
            'artifact_storage_backend':settings.runtime.artifact_storage.backend},
            status_code=200 if ready else 503)
    return web


async def run_worker_server(settings, *, on_fatal=None):
    import uvicorn

    def failed():
        server.should_exit = True
        if on_fatal is not None:
            on_fatal()

    web = create_worker_app(settings, on_fatal=failed)
    server = uvicorn.Server(uvicorn.Config(web, host='0.0.0.0',
        port=int(os.getenv('PORT', '8000')),
        timeout_graceful_shutdown=shutdown_budget_seconds(settings)))
    await server.serve()
    if not server.started or web.state.worker_failed:
        raise RuntimeError('Worker server stopped after a role failure')


def main():
    parser=argparse.ArgumentParser(description='workmate native durable Worker')
    parser.add_argument('--check-config',action='store_true',help='Validate configuration without opening services or files')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    watchdog = None
    def force_failed_exit():
        logger.error('Worker fatal shutdown exceeded budget; exiting process')
        os._exit(1)

    def arm_watchdog():
        nonlocal watchdog
        if watchdog is None:
            watchdog = threading.Timer(shutdown_budget_seconds(settings), force_failed_exit)
            watchdog.daemon = True
            watchdog.start()

    try:
        settings=load_worker_role_settings()
        if args.check_config:
            print('Worker configuration valid')
            return
        asyncio.run(run_worker_server(settings, on_fatal=arm_watchdog))
    except Exception as error:
        # Validation errors can embed DB URLs/credentials; never log raw input.
        logger.error('Worker startup or execution failed (%s)',type(error).__name__)
        raise SystemExit(1) from None
    finally:
        # Keep the fuse armed through asyncio.run's final task cancellation,
        # which can itself hang when a cleanup coroutine ignores cancellation.
        if watchdog is not None:
            watchdog.cancel()


if __name__=='__main__':
    main()
