"""Small native Workspace specializations; no AgentScope App control chain."""
from .base import WorkspaceBindingError


def secure_kubernetes(base):
    class SecureKubernetesWorkspace(base):
        async def _ensure_pvc(self):
            expected = getattr(self, '_workmate_expected_storage_proof', None)
            if expected is None:
                return await super()._ensure_pvc()
            from .persistent_storage import verify_storage_identity
            # This is a read-only preflight before _ensure_pod can adopt/create.
            self._workmate_storage_preflight_failed = False
            try:
                await verify_storage_identity(self, expected)
            except Exception:
                self._workmate_storage_preflight_failed = True
                raise

        async def _ensure_pod(self):
            if getattr(self, '_workmate_expected_storage_proof', None) is None:
                return await super()._ensure_pod()
            from kubernetes_asyncio.client.rest import ApiException
            try:
                await self._v1.read_namespaced_pod(self._pod_name, self._namespace)
            except ApiException as error:
                if error.status != 404:
                    raise
            else:
                raise WorkspaceBindingError('Prior or foreign Pod still exists; cannot adopt during cold replacement')
            await self._create_pod()

        async def _create_pod(self):
            api = self._v1
            namespace, name = self._namespace, self._pod_name

            class PodCreationAPI:
                def __getattr__(self, key):
                    return getattr(api, key)

                async def create_namespaced_pod(self, target, body, **kwargs):
                    # Preserve all upstream construction decisions, changing only
                    # credential exposure on the exact Workspace being created.
                    if (target != namespace or body.metadata.namespace != namespace
                            or body.metadata.name != name):
                        raise WorkspaceBindingError('Kubernetes Workspace identity changed during creation')
                    body.spec.automount_service_account_token = False
                    return await api.create_namespaced_pod(target, body, **kwargs)

            self._v1 = PodCreationAPI()
            try:
                await super()._create_pod()
            finally:
                self._v1 = api

        async def _provision_backend(self):
            await super()._provision_backend()
            # Upstream may adopt an existing Pod; creation-time patching alone
            # cannot certify it. Use the server UID, never its reusable name.
            pod = await self._v1.read_namespaced_pod(self._pod_name, self._namespace)
            if (pod.metadata.name != self._pod_name or pod.metadata.namespace != self._namespace
                    or pod.spec.automount_service_account_token is not False
                    or not isinstance(pod.metadata.uid, str) or not pod.metadata.uid.strip()):
                raise WorkspaceBindingError('Kubernetes Pod identity or service-account token isolation is unverified')
            self._workmate_instance_id = pod.metadata.uid

    return SecureKubernetesWorkspace


def persistent_docker(base):
    class PersistentDockerWorkspace(base):
        async def _create_and_start_container(self):
            from agentscope.workspace import DockerBackend
            expected = getattr(self, '_workmate_expected_storage_proof', None)
            if expected is not None:
                from .persistent_storage import verify_storage_identity
                # No container API was called; only this precise failure is clean.
                self._workmate_storage_preflight_failed = False
                try:
                    await verify_storage_identity(self, expected)
                except Exception:
                    self._workmate_storage_preflight_failed = True
                    raise
            config = {
                'Image': self._image_tag,
                'Cmd': ['sleep', 'infinity'],
                'User': '0:0',
                'WorkingDir': self.workdir,
                'Labels': {'workmate.workspace': self.workspace_id},
                'HostConfig': {
                    'Init': True,
                    # Root runs only the trusted control channel. Agent/MCP
                    # execution always drops identity and sets no_new_privs.
                    'CapDrop': ['ALL'],
                    'CapAdd': ['CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'KILL', 'SETGID', 'SETUID'],
                    'Mounts': [{'Type': 'volume', 'Source': 'workmate-' + self.workspace_id,
                                'Target': self.workdir}],
                },
            }
            # Creating an existing name fails; never replace another incarnation.
            self._container = await self._client.containers.create(
                config=config, name='workmate-' + self.workspace_id)
            await self._container.start()
            self._workmate_instance_id = str(self._container.id)
            self._backend = DockerBackend(self._container, self.workdir)

    return PersistentDockerWorkspace
