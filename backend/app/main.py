# -*- coding: utf-8 -*-
"""workmate product-api entrypoint (Enterprise Control Plane).

Production entry for the Product API.
Lifespan runs durable Product admission/projection. Agent execution belongs to
the independent Worker; knowledge administration uses its protected HTTP API.
"""

from contextlib import AsyncExitStack, asynccontextmanager
from workmate_product.skills.lifecycle import skill_catalogs
import asyncio
import logging
import os
from workmate_product.mcp.github_catalog import GitHubMCPCatalog

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .adapter.knowledge_client import KnowledgeServiceClient
from .api.routers import (
    sharing,
    knowledge,
    admin_users,
    admin_roles,
    admin_scopes,
    admin_organization,
    admin_audit,
    agents,
    artifacts,
    auth,
    conversations,
    mcps,
    mcp_connections,
    models,
    scopes,
    skills,
    tasks,
    usage,
)
from .config import get_settings
from .db import dispose_engine, get_sessionmaker
from .services.bootstrap import ensure_bootstrap
from .services.workspace_assistant import ensure_workspace_assistant, ensure_builtin_skill_library

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Known placeholder values from .env.example / docker-compose.yml /
# code defaults. A deployment running on one of these issues
# forgeable tokens.
_PLACEHOLDER_SECRETS = {
    "dev-secret-change-me",
    "change-me-in-production",
    "replace-with-at-least-32-random-bytes",
}


def _warn_insecure_settings() -> None:
    settings = get_settings()
    if settings.jwt_secret in _PLACEHOLDER_SECRETS or len(
        settings.jwt_secret
    ) < 32:
        logger.warning(
            "JWT_SECRET is a known placeholder or shorter than 32 bytes "
            "- issued tokens are forgeable. Set a strong JWT_SECRET "
            "before any real deployment.",
        )
    if not settings.runtime_internal_token:
        logger.warning(
            "RUNTIME_INTERNAL_TOKEN is not set - all protected Runtime "
            "API calls will fail. Set "
            "RUNTIME_INTERNAL_TOKEN on gateway and the matching "
            "WORKMATE_INTERNAL_TOKEN on Runtime.",
        )


def build_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            stack.push_async_callback(dispose_engine)
            app.state.product_ready = False
            _warn_insecure_settings()
            # Seed root scope and roles/permissions. User scopes are provisioned
            # explicitly during registration.
            async with get_sessionmaker()() as db:
                await ensure_bootstrap(db)

            settings = get_settings()
            client = KnowledgeServiceClient(settings.runtime_base_url, settings.runtime_internal_token,
                timeout=settings.runtime_timeout_secs)
            # Upload and verify shipped packages before taking Product bootstrap locks.
            from workmate_product.skill_storage import PreparedSkillBlobStore, get_skill_blob_store
            from workmate_product.skills.catalog import BuiltinCatalog
            async with get_sessionmaker()() as db:
                prepared_skills = PreparedSkillBlobStore(get_skill_blob_store(db))
            catalog = BuiltinCatalog()
            cursor = None
            while True:
                page = await catalog.list_skills('workmate-bootstrap', None, cursor, 100)
                for card in page.cards:
                    await prepared_skills.prepare(await catalog.archive('workmate-bootstrap', card.id, card.version))
                cursor = page.next_cursor
                if not cursor:
                    break
            async with get_sessionmaker()() as db:
                db.info['skill_blob_store'] = prepared_skills
                await ensure_builtin_skill_library(db)
                if get_settings().workspace_assistant_enabled:
                    await ensure_workspace_assistant(db)
                await db.commit()
            # The Worker exposes only auxiliary knowledge management HTTP.
            # Task execution and platform tools use durable/shared Product services.
            app.state.runtime_client = client
            from .services.durable_runtime import compose_product_services
            settings = get_settings()
            if not settings.coordination_redis_url:
                raise RuntimeError('WORKMATE_COORDINATION_REDIS_URL is required for command delivery')
            from redis.asyncio import Redis
            from workmate_product.coordination.command_queue import RedisCommandQueue
            from workmate_product.coordination.command_publisher import CommandPublisher
            redis_client=await stack.enter_async_context(Redis.from_url(settings.coordination_redis_url,
                decode_responses=True,socket_connect_timeout=5,socket_timeout=5))
            queue=RedisCommandQueue(redis_client,namespace=settings.coordination_namespace)
            publisher=CommandPublisher(get_sessionmaker(),queue,settings.command_queue)
            from redis.asyncio.retry import Retry
            from redis.backoff import NoBackoff
            from workmate_product.realtime.composition import create_realtime_log
            from workmate_product.realtime.business_publisher import BusinessPublisher
            realtime_client = await stack.enter_async_context(Redis.from_url(
                settings.coordination_redis_url, decode_responses=True,
                retry=Retry(NoBackoff(), 0), retry_on_timeout=False,
                socket_connect_timeout=5,
                socket_timeout=max(5, settings.realtime.reader_block_ms / 1000 + 1)))
            realtime_log = create_realtime_log(realtime_client,
                namespace=settings.coordination_namespace, policy=settings.realtime)
            wakeup_bus = None
            if settings.coordination_redis_url:
                from workmate_product.coordination import WakeupBus
                wakeup_bus = await stack.enter_async_context(WakeupBus(settings.coordination_redis_url))
                get_sessionmaker().configure(info={
                    **get_sessionmaker().kw.get('info', {}), 'wakeup_bus': wakeup_bus})
            node_directory = None
            if settings.worker_nodes:
                from .services.worker_nodes import WorkerNodeDirectory
                node_directory = await stack.enter_async_context(WorkerNodeDirectory(
                    settings.worker_nodes, settings.runtime_internal_token))
            runtime,projection,stream=compose_product_services(get_sessionmaker(),settings,
                node_directory=node_directory,wakeup_bus=wakeup_bus,realtime_log=realtime_log)
            stack.push_async_callback(stream.reader.close)
            stack.push_async_callback(stream.close)
            # Both outboxes share bounded scheduling limits; command routing and
            # retention settings do not apply to the business event publisher.
            business_publisher = BusinessPublisher(get_sessionmaker(), stream.manager,
                realtime_log, settings.command_queue)
            app.state.product_runtime=runtime
            app.state.product_stream=stream
            app.state.product_sessions=get_sessionmaker()
            app.state.product_projection=projection
            from .services.schedules import ScheduleService
            from workmate_product.config.schedules import SchedulePolicy
            schedule_service = ScheduleService(get_sessionmaker(), runtime.admission,
                                               policy=SchedulePolicy.from_settings(settings))
            app.state.schedule_service = schedule_service
            from .channels.processing import ChannelService
            from .channels.supervisor import ChannelSupervisor
            from workmate_product.config.channels import ChannelPolicy
            channel_service = ChannelService(get_sessionmaker(), runtime.admission,
                ChannelPolicy.from_settings(settings), settings.workmate_public_url)
            app.state.channel_service = channel_service
            from .channels.realtime import ChannelTextFeed
            channel_supervisor = ChannelSupervisor(channel_service,
                text_feed=ChannelTextFeed(stream, get_sessionmaker()))
            app.state.skill_catalogs = await stack.enter_async_context(skill_catalogs(
                skillhub_cn_enabled=settings.skillhub_cn_enabled,
                skillhub_cn_base_url=settings.skillhub_cn_base_url,
                skillhub_cn_request_timeout_secs=settings.skillhub_cn_request_timeout_secs,
                skillhub_cn_max_retries=settings.skillhub_cn_max_retries,
            ))
            app.state.mcp_catalogs = {}
            if settings.github_mcp_hub_enabled:
                mcp_catalog = await stack.enter_async_context(GitHubMCPCatalog(api_token=os.getenv('GITHUB_TOKEN')))
                app.state.mcp_catalogs = {mcp_catalog.hub_id: mcp_catalog}
            shutdown = asyncio.Event()
            worker = asyncio.create_task(projection.serve(shutdown))
            publishing = asyncio.create_task(publisher.serve(shutdown))
            business_publishing = asyncio.create_task(business_publisher.serve(shutdown))
            scheduling = asyncio.create_task(schedule_service.serve(shutdown))
            app.state.projection_task = worker
            app.state.command_publisher_task = publishing
            app.state.business_publisher_task = business_publishing
            app.state.schedule_task = scheduling
            channel_task = asyncio.create_task(channel_supervisor.serve(shutdown))
            app.state.channel_task = channel_task
            app.state.product_ready = True
            try:
                yield
            finally:
                app.state.product_ready = False
                try:
                    await stream.close()
                    await stream.reader.close()
                finally:
                    shutdown.set()
                    results = await asyncio.gather(worker,publishing,business_publishing,scheduling,channel_task,
                        return_exceptions=True)
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result

    app = FastAPI(title="Workmate Enterprise Control Plane", lifespan=lifespan)
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path == '/api/channels' or request.url.path.startswith('/api/channels/'):
            return JSONResponse({'detail': '渠道请求参数无效'}, status_code=422)
        if request.url.path == '/api/mcps' or request.url.path.startswith('/api/mcps/'):
            return JSONResponse({'detail': 'Invalid MCP request'}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    from .api.routers import recovery, channels
    from .api import schedules
    for router in (channels.router, schedules.router, recovery.router, usage.router, auth.router, admin_users.router, admin_roles.router,
                   admin_scopes.router, admin_organization.router, admin_audit.router,
                   agents.router, sharing.router,
                   scopes.router, skills.router, mcp_connections.router, mcps.router, models.router,
                   conversations.router, tasks.router, artifacts.router):
        app.include_router(router, prefix="/api")
    app.include_router(knowledge.router, prefix="/api")

    @app.get('/health', include_in_schema=False)
    async def health():
        projection_task = getattr(app.state, 'projection_task', None)
        publisher_task = getattr(app.state, 'command_publisher_task', None)
        business_task = getattr(app.state, 'business_publisher_task', None)
        schedule_task = getattr(app.state, 'schedule_task', None)
        channel_task = getattr(app.state, 'channel_task', None)
        channel_ready = (not get_settings().channel_enabled
                         or channel_task is not None and not channel_task.done())
        schedule_ready = (not get_settings().schedule_enabled
                          or schedule_task is not None and not schedule_task.done())
        ready = bool(getattr(app.state, 'product_ready', False)
                     and projection_task is not None and not projection_task.done()
                     and publisher_task is not None and not publisher_task.done()
                     and business_task is not None and not business_task.done()
                     and schedule_ready and channel_ready)
        return JSONResponse({'role': 'product-api', 'ready': ready},
                            status_code=200 if ready else 503)

    return app


app = build_app()
