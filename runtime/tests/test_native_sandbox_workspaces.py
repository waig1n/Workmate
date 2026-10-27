"""Native provider wrappers preserve upstream behavior and restrict workload authority."""
from types import SimpleNamespace
import pytest
from workmate_runtime.sandbox.native_workspaces import persistent_docker, secure_kubernetes
from workmate_runtime.sandbox.base import WorkspaceBindingError


class FakeAPI:
    def __init__(self):
        self.created=[]
        self.pod=SimpleNamespace(metadata=SimpleNamespace(name='pod',namespace='ns',uid='actual-pod-uid'),
            spec=SimpleNamespace(automount_service_account_token=False))
    async def create_namespaced_pod(self,namespace,body,**kwargs):
        self.created.append((namespace,body))
    async def read_namespaced_pod(self,name,namespace):
        assert (name,namespace)==('pod','ns')
        return self.pod


class K8sBase:
    def __init__(self,api):
        self._v1=api
        self._namespace='ns'
        self._pod_name='pod'
    async def _create_pod(self):
        body=SimpleNamespace(metadata=SimpleNamespace(name=self._pod_name,namespace=self._namespace),
            spec=SimpleNamespace(automount_service_account_token=None))
        await self._v1.create_namespaced_pod(self._namespace,body)
    async def _provision_backend(self):
        await self._create_pod()


async def test_kubernetes_disables_token_and_records_server_uid():
    api=FakeAPI()
    workspace=secure_kubernetes(K8sBase)(api)
    await workspace._provision_backend()
    assert workspace._v1 is api
    assert api.created[0][1].spec.automount_service_account_token is False
    assert workspace._workmate_instance_id=='actual-pod-uid'


@pytest.mark.parametrize('field,value', [('automount_service_account_token',True),('automount_service_account_token',None)])
async def test_kubernetes_rejects_unsafe_adopted_pod(field,value):
    api=FakeAPI()
    setattr(api.pod.spec,field,value)
    with pytest.raises(WorkspaceBindingError):
        await secure_kubernetes(K8sBase)(api)._provision_backend()


async def test_kubernetes_restores_api_after_rejected_identity():
    class WrongPod(K8sBase):
        async def _create_pod(self):
            await self._v1.create_namespaced_pod('other',SimpleNamespace(
                metadata=SimpleNamespace(name='pod',namespace='other'),spec=SimpleNamespace()))
    api=FakeAPI()
    workspace=secure_kubernetes(WrongPod)(api)
    with pytest.raises(WorkspaceBindingError):
        await workspace._create_pod()
    assert workspace._v1 is api and not api.created


async def test_kubernetes_requires_actual_instance_uid():
    api=FakeAPI()
    api.pod.metadata.uid=None
    with pytest.raises(WorkspaceBindingError):
        await secure_kubernetes(K8sBase)(api)._provision_backend()


async def test_docker_uses_named_volume_init_and_explicit_control_capabilities(monkeypatch):
    created=[]
    class Container:
        id='actual-container-id'
        async def start(self):
            self.started=True
    container=Container()
    async def create(**kwargs):
        created.append(kwargs)
        return container
    class DockerBase:
        _image_tag='trusted-image'
        workspace_id='workspace'
        workdir='/workspace'
        _client=SimpleNamespace(containers=SimpleNamespace(create=create))
    monkeypatch.setattr('agentscope.workspace.DockerBackend',lambda c,w: (c,w))
    workspace=persistent_docker(DockerBase)()
    await workspace._create_and_start_container()
    config=created[0]['config']
    assert config['HostConfig']['Init'] is True
    assert config['HostConfig']['Mounts']==[{'Type':'volume','Source':'workmate-workspace','Target':'/workspace'}]
    assert config['HostConfig']['CapDrop']==['ALL']
    assert set(config['HostConfig']['CapAdd'])=={'CHOWN','DAC_OVERRIDE','FOWNER','KILL','SETGID','SETUID'}
    assert config['User']=='0:0'
    assert workspace._workmate_instance_id=='actual-container-id'
    assert workspace._backend==(container,'/workspace')
