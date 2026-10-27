"""Delete only explicitly identified, stopped Docker/K8s storage; never provision."""
import asyncio
import re
from .base import WorkspaceBindingError


async def purge_provider_storage(provider, resource_id):
    if re.fullmatch('[0-9a-f]{64}', resource_id) is None:
        raise WorkspaceBindingError('Invalid persistent sandbox identity')
    if provider.name in {'bubblewrap', 'local'}:
        return
    if provider.name == 'docker':
        import aiodocker
        from aiodocker.exceptions import DockerError
        async with aiodocker.Docker() as client:
            name = 'workmate-' + resource_id
            try:
                await client.containers.get(name)
            except DockerError as error:
                if error.status != 404:
                    raise
            else:
                raise WorkspaceBindingError('Container still exists; storage deletion refused')
            try:
                volume = await client.volumes.get(name)
                await volume.delete()  # No force: an attached volume must remain blocked.
            except DockerError as error:
                if error.status != 404:
                    raise
        return
    if provider.name == 'kubernetes':
        from kubernetes_asyncio import client, config
        from kubernetes_asyncio.client.rest import ApiException
        from agentscope.workspace._k8s._constants import _k8s_safe_name
        if provider.settings.kubeconfig:
            await config.load_kube_config(config_file=provider.settings.kubeconfig)
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                await config.load_kube_config()
        name, namespace = _k8s_safe_name(resource_id), provider.settings.namespace
        async with client.ApiClient() as api_client:
            api = client.CoreV1Api(api_client)
            try:
                await api.read_namespaced_pod(name, namespace)
            except ApiException as error:
                if error.status != 404:
                    raise
            else:
                raise WorkspaceBindingError('Pod still exists; PVC deletion refused')
            try:
                pvc = await api.read_namespaced_persistent_volume_claim(name, namespace)
                if ((pvc.metadata.labels or {}).get('agentscope.workspace.id') != resource_id
                        or not isinstance(pvc.metadata.uid, str) or not pvc.metadata.uid):
                    raise WorkspaceBindingError('PVC ownership label differs')
                await api.delete_namespaced_persistent_volume_claim(name, namespace,
                    body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=pvc.metadata.uid)))
                # A terminating PVC is not a successful deletion receipt.
                async with asyncio.timeout(30):
                    while True:
                        await api.read_namespaced_persistent_volume_claim(name, namespace)
                        await asyncio.sleep(.2)
            except ApiException as error:
                if error.status != 404:
                    raise
        return
    raise WorkspaceBindingError('Permanent cleanup is not implemented for this provider')
