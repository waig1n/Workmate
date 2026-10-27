"""Authenticated, read-only startup checks; never provision a probe sandbox."""
import asyncio
from datetime import timedelta

from .base import WorkspaceBindingError


async def probe_provider(settings):
    try:
        async with asyncio.timeout(15):
            await _probe(settings)
    except asyncio.CancelledError:
        raise
    except Exception:
        # SDK exceptions can include credentials or connection strings.
        raise WorkspaceBindingError('Configured sandbox service connection could not be verified') from None


async def _probe(settings):
    if settings.provider=='docker':
        from aiodocker import Docker
        client= Docker()
        try:
            await client.version()
        finally:
            await client.close()
    elif settings.provider=='kubernetes':
        from kubernetes_asyncio import client, config
        configuration=client.Configuration()
        if settings.kubeconfig:
            await config.load_kube_config(config_file=settings.kubeconfig,
                client_configuration=configuration,persist_config=False)
        else:
            try:
                config.load_incluster_config(client_configuration=configuration)
            except config.ConfigException:
                await config.load_kube_config(client_configuration=configuration,persist_config=False)
        api=client.ApiClient(configuration=configuration)
        try:
            await client.CoreV1Api(api).get_api_resources(_request_timeout=10)
        finally:
            await api.close()
    elif settings.provider=='e2b':
        import httpx
        from e2b.connection_config import ConnectionConfig
        from e2b.api.client_async import AsyncApiClient
        from e2b.api.client.api.sandboxes import get_v2_sandboxes
        configuration=ConnectionConfig(api_key=settings.api_key.get_secret_value(),
            domain=settings.domain or None,request_timeout=10)
        # The public paginator owns no closable handle and uses a shared SDK
        # transport. This one-request SDK client owns its transport explicitly.
        async with httpx.AsyncHTTPTransport() as transport:
            async with AsyncApiClient(configuration,transport=transport) as api:
                response=await get_v2_sandboxes.asyncio_detailed(client=api,limit=1)
                if not 200<=response.status_code<300:
                    raise WorkspaceBindingError('Sandbox authentication failed')
    elif settings.provider=='daytona':
        from daytona import AsyncDaytona, DaytonaConfig, ListSandboxesQuery
        client=AsyncDaytona(DaytonaConfig(api_key=settings.api_key.get_secret_value(),
            api_url=settings.api_url or None,target=settings.target or None))
        try:
            iterator=client.list(ListSandboxesQuery(limit=1),request_timeout=10)
            try:
                await anext(iterator,None)
            finally:
                await iterator.aclose()
        finally:
            await client.close()
    elif settings.provider=='opensandbox':
        from opensandbox import SandboxManager
        from opensandbox.config import ConnectionConfig
        from opensandbox.models.sandboxes import SandboxFilter
        manager=await SandboxManager.create(ConnectionConfig(
            api_key=settings.api_key.get_secret_value() or None,domain=settings.domain or None,
            protocol=settings.protocol,request_timeout=timedelta(seconds=10)))
        try:
            await manager.list_sandbox_infos(SandboxFilter(page_size=1,page=1))
        finally:
            await manager.close()
    else:
        raise WorkspaceBindingError('Provider has no remote connection probe')
