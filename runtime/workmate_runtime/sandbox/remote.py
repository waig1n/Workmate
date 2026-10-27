"""Direct AgentScope Workspace providers with a shared governed remote adapter."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets

from workmate_runtime.execution.controller import SandboxVerificationFailed
from .base import WorkspaceBindingError
from .remote_backend import RemoteControlBackend, RemoteTaskBackend
from .native_workspaces import persistent_docker, secure_kubernetes

PROVIDERS = {
    'docker': ('DockerWorkspace', 'aiodocker'),
    'kubernetes': ('K8sWorkspace', 'kubernetes_asyncio'),
    'e2b': ('E2BWorkspace', 'e2b'),
    'daytona': ('DaytonaWorkspace', 'daytona'),
    'opensandbox': ('OpenSandboxWorkspace', 'opensandbox'),
}

# Workspaces retain their native paths so PVCs and vendor persistence continue to work.
_SETUP = '''import json,os,pathlib,stat,sys
control,workdir,identity=sys.argv[1:]
if os.geteuid()!=0: raise RuntimeError('Root control required')
for target in (control,workdir):
    current=pathlib.Path('/')
    for part in pathlib.Path(target).parts[1:]:
        current=current/part
        if current.is_symlink(): raise RuntimeError('Unsafe workspace ancestor')
        current.mkdir(mode=0o755,exist_ok=True)
        if target==control:
            st=current.stat()
            if st.st_uid!=0 or st.st_mode&0o022: raise RuntimeError('Control ancestor is writable')
for parent in (pathlib.Path(control),):
    os.chown(parent,0,0);os.chmod(parent,0o700)
record=pathlib.Path(control,'identity')
if record.exists() and record.read_text()!=identity: raise RuntimeError('Sandbox identity differs')
record.write_text(identity)
os.chown(workdir,62000,62000);os.chmod(workdir,0o775)
for name in ('input','working','output','skills','sessions','data'):
    path=pathlib.Path(workdir,name)
    if path.is_symlink(): raise RuntimeError('Unsafe workspace directory')
    path.mkdir(exist_ok=True)
    os.chown(path,62000,62000);os.chmod(path,0o775)
state=pathlib.Path(control,'state.json')
if not state.exists(): state.write_text(json.dumps({'active':False,'epoch':'','workdir':workdir}))
'''


async def establish_control(workspace):
    raw = workspace.get_backend()
    discovery = 'import os,sys;print(str(os.geteuid())+"\\n"+os.path.realpath(sys.executable))'
    result = await raw.exec_shell(['python3','-I','-c',discovery],timeout=10)
    if result.exit_code:
        result = await raw.exec_shell(['/usr/bin/python3','-I','-c',discovery],timeout=10)
    if result.exit_code:
        raise WorkspaceBindingError('Sandbox template requires Python 3')
    uid, python = result.stdout.decode().strip().splitlines()
    prefix = [] if uid=='0' else ['sudo','-n']
    check = """import os,pathlib,sys
p=pathlib.Path(os.path.realpath(sys.executable))
assert os.geteuid()==0
for item in (p,*p.parents):
    s=item.stat()
    assert s.st_uid==0 and not s.st_mode&0o022
assert not p.is_relative_to(pathlib.Path(sys.argv[1]))
"""
    result = await raw.exec_shell([*prefix,python,'-I','-c',check,workspace.workdir],timeout=10)
    if result.exit_code:
        raise WorkspaceBindingError('Sandbox requires root or passwordless sudo and a root-owned Python')
    directory = '/var/lib/workmate/'+hashlib.sha256(workspace.workspace_id.encode()).hexdigest()
    control = RemoteControlBackend(raw,python=python,prefix=prefix,directory=directory,workdir=workspace.workdir)
    # Retain the real management handle even if setup or its response fails.
    workspace._workmate_control = control
    if workspace._workmate_provider in {'docker', 'kubernetes'}:
        from .persistent_storage import initialize_storage_proof
        await initialize_storage_proof(workspace)
    result = await raw.exec_shell([*prefix,python,'-I','-c',_SETUP,directory,workspace.workdir,workspace.workspace_id],timeout=10)
    if result.exit_code:
        raise WorkspaceBindingError('Remote Workspace identity or permissions are unavailable')
    source = Path(__file__).parent
    for local,remote in [('launch.py','launch.py'),('control_script.py','control.py')]:
        await control.write_file(directory+'/'+remote,(source/local).read_bytes())
    state = await control.operation('probe')
    if state['active'] or state['pids']:
        raise WorkspaceBindingError('Remote Workspace has unresolved previous execution')
    return control


class RemoteSandboxProvider:
    def __init__(self, settings):
        self.settings, self.name = settings, settings.provider

    async def probe(self):
        module = PROVIDERS[self.name][1]
        if importlib.util.find_spec(module) is None:
            raise WorkspaceBindingError('Install the configured sandbox provider SDK: '+module)
        # The actual instance is verified before model execution, not by provisioning a paid probe sandbox.
        if self.name in {'e2b','daytona'} and not self.settings.api_key.get_secret_value():
            raise WorkspaceBindingError('Configured sandbox API key is missing')
        if self.name == 'opensandbox' and not self.settings.domain:
            raise WorkspaceBindingError('Configured OpenSandbox endpoint is missing')
        from .probe import probe_provider
        await probe_provider(self.settings)

    def create_workspace(self, request):
        import agentscope.workspace as workspaces
        cls = getattr(workspaces, PROVIDERS[self.name][0])
        kwargs = self.settings.model_dump(exclude={'provider'})
        for field in ('api_key','domain','api_url','kubeconfig'):
            if hasattr(self.settings,field):
                value = getattr(self.settings,field)
                kwargs[field] = value.get_secret_value() if hasattr(value,'get_secret_value') else value
        if self.name == 'docker':
            # Bind mounts would resolve on the daemon host, not in the Worker container.
            cls = persistent_docker(cls)
        elif self.name == "kubernetes":
            cls = secure_kubernetes(cls)
        workspace = cls(workspace_id=request.workspace_id,**kwargs)
        workspace.host_workdir = None
        workspace._workmate_control = None
        workspace._workmate_provider = self.name
        workspace._workmate_initialized = False
        workspace._workmate_expected_storage_proof = request.expected_storage_proof
        workspace._workmate_placement_path = request.placement_path
        return workspace

    async def initialize_workspace(self, workspace):
        # Deliberately do not call upstream initialize: its default gateway writes MCP declarations.
        workspace._workmate_provision_attempted = True
        await workspace._provision_backend()
        control = await establish_control(workspace)
        workspace._workmate_control = control
        workspace._backend = control
        # Install the gateway into a root-only venv, never root-execute Agent-owned files.
        venv = control.directory+'/venv'
        code = """import os,shutil,subprocess,sys
v=sys.argv[1]
if not shutil.which('rg'):
    subprocess.run(['apt-get','update'],check=True,stdout=sys.stderr)
    subprocess.run(['apt-get','install','-y','--no-install-recommends','ripgrep'],check=True,stdout=sys.stderr)
if not os.path.isfile(v+'/bin/python'):
    subprocess.run([sys.executable,'-I','-m','venv','--system-site-packages',v],check=True)
probe="import fastapi,uvicorn,httpx,cryptography;from importlib.metadata import version;assert version('agentscope')=='2.0.7'"
ready=subprocess.run([v+'/bin/python','-I','-c',probe],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
if not ready:
    subprocess.run([v+'/bin/python','-I','-m','pip','install','--disable-pip-version-check',
        'agentscope==2.0.7','fastapi','uvicorn','httpx','cryptography'],check=True,stdout=sys.stderr)
"""
        result = await control.exec_shell([control.python,'-I','-c',code,venv],cwd='/',timeout=600)
        if result.exit_code:
            raise WorkspaceBindingError('Sandbox gateway dependencies are unavailable; preinstall the documented template dependencies')
        from workmate_runtime.mcp.remote import start_remote_gateway
        workspace._workmate_gateway_starting = True
        workspace._gateway = await start_remote_gateway(control,venv+'/bin/python',control.directory,uid=62001)
        workspace._workmate_gateway_starting = False
        from agentscope.workspace._bubblewrap._bubblewrap_workspace import _read_glob_helper_bytes
        helper = '/usr/local/lib/workmate/glob.py'
        prepared = await control.exec_shell(['mkdir','-p','/usr/local/lib/workmate'])
        if prepared.exit_code:
            raise WorkspaceBindingError('Remote tool helper directory is unavailable')
        await control.write_file(helper,_read_glob_helper_bytes())
        prepared = await control.exec_shell(['chmod','644',helper])
        if prepared.exit_code:
            raise WorkspaceBindingError('Remote tool helper permissions are unavailable')
        workspace._workmate_glob_helper_path = helper
        workspace.is_alive = True
        workspace._workmate_initialized = True

    def bind_backend(self, workspace, owner, registry):
        if not workspace._workmate_initialized or workspace._workmate_provider != self.name:
            raise WorkspaceBindingError('Remote Workspace is not initialized for this provider')
        return RemoteTaskBackend(workspace._workmate_control,owner,registry,workspace)

    async def capture_persistent_storage(self, workspace):
        """Recheck original storage immediately before orderly compute release."""
        from .persistent_storage import verify_storage_identity, check_marker
        if self.name not in {'docker', 'kubernetes'} or workspace._workmate_provider != self.name:
            raise WorkspaceBindingError('Provider cannot prove persistent approval storage')
        proof = await verify_storage_identity(workspace, getattr(workspace, '_workmate_storage_proof', None))
        await check_marker(workspace._workmate_control, workspace.workdir, proof['marker'], create=False)
        return proof

    async def verify_persistent_storage(self, workspace, expected):
        from .persistent_storage import validate_storage_proof
        expected = validate_storage_proof(expected)
        actual = await self.capture_persistent_storage(workspace)
        if actual != expected:
            raise WorkspaceBindingError('Persistent storage differs from durable release proof')
        return actual

    async def verify_task_isolation(self, backend):
        if not isinstance(backend,RemoteTaskBackend) or backend.workspace._workmate_provider != self.name:
            raise SandboxVerificationFailed('Sandbox proof requires a matching remote Task binding')
        result = await backend.exec_shell([backend.control.python,'-I','-c',
            'import ctypes,json,os;print(json.dumps({"uid":os.getuid(),"uids":os.getresuid(),"no_new_privs":ctypes.CDLL(None).prctl(39,0,0,0,0)==1,"caps":next(x.split()[1] for x in open("/proc/self/status") if x.startswith("CapEff:"))}))'],timeout=10)
        try:
            evidence = json.loads(result.stdout)
            valid = result.exit_code==0 and evidence['uid']==62000 and evidence['uids']==[62000]*3 and evidence['no_new_privs'] is True and int(evidence['caps'],16)==0
        except (ValueError,KeyError,TypeError):
            valid = False
        if not valid:
            raise SandboxVerificationFailed('Dedicated sandbox user or no-new-privileges could not be verified')
        return {'isolation':self.name,'containment':'dedicated_uid',
            'instance_id': _instance_id(backend.workspace), 'evidence':evidence}

    async def close_workspace(self, workspace):
        control = getattr(workspace,'_workmate_control',None)
        if (self.name in {'docker', 'kubernetes'}
                and getattr(workspace, '_workmate_storage_preflight_failed', False) is True
                and control is None and getattr(workspace, '_container', None) is None
                and getattr(workspace, '_workmate_instance_id', None) is None
                and getattr(workspace, '_backend', None) is None
                and not getattr(workspace, '_workmate_gateway_starting', False)):
            # A verified read-only volume/PVC preflight failed before any compute
            # create/adopt/exec call. Close the client, never delete by a Pod name
            # which may now belong to an unrelated instance. Unknown create
            # outcomes do not set this flag and retain the dirty barrier below.
            field = '_client' if self.name == 'docker' else '_api_client'
            client = getattr(workspace, field, None)
            if client is not None:
                await client.close()
                setattr(workspace, field, None)
            if self.name == 'kubernetes':
                workspace._v1 = None
            workspace.is_alive = False
            return
        if ((control is None and getattr(workspace,'_workmate_provision_attempted',False))
                or getattr(workspace,'_workmate_gateway_starting',False)):
            # A timed-out SDK create/start may have succeeded remotely. Do not
            # turn a missing response/handle into clean ownership or a retry.
            raise WorkspaceBindingError('Remote initialization outcome is unresolved; retain ownership for recovery')
        if control is not None:
            state = await control.operation('probe')
            if state['active'] or state['pids']:
                raise WorkspaceBindingError('Remote Workspace resources are not quiescent')
            gateway = getattr(workspace,'_gateway',None)
            if gateway is not None:
                await gateway.stop()
                await gateway.aclose()
                workspace._gateway = None
        await _release(workspace,self.name)
        workspace.is_alive = False
        workspace._backend = None


def _instance_id(workspace):
    if workspace._workmate_provider in {'docker','kubernetes'}:
        return workspace._workmate_instance_id
    sandbox = workspace._sandbox
    identity = getattr(sandbox,'sandbox_id',None) or getattr(sandbox,'id',None)
    if not isinstance(identity,str) or not identity:
        raise SandboxVerificationFailed('Provider instance identity is unavailable')
    return identity


async def _release(workspace, name):
    # Do not use upstream swallow-on-close paths as a cleanup receipt.
    if name=='docker':
        if workspace._container is not None:
            await workspace._container.delete(force=True)
            workspace._container = None
        if workspace._client is not None:
            await workspace._client.close()
            workspace._client = None
    elif name=='kubernetes':
        if workspace._v1 is not None and workspace._pod_name:
            identity = getattr(workspace, '_workmate_instance_id', None)
            if not isinstance(identity, str) or not identity.strip():
                raise WorkspaceBindingError('Kubernetes Pod identity unavailable; retain ownership')
            # Tasks are already proven quiescent and the gateway closed. Leave
            # a normal SIGTERM grace below the SDK's 30-second absence wait.
            await workspace._v1.delete_namespaced_pod(workspace._pod_name,workspace._namespace,
                body={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'gracePeriodSeconds': 5,
                      'preconditions': {'uid': identity}})
            await workspace._wait_pod_deleted()
        if workspace._api_client is not None:
            await workspace._api_client.close()
            workspace._api_client = None
    elif workspace._sandbox is not None:
        if name=='e2b':
            await workspace._sandbox.pause()
        elif name=='daytona':
            await workspace._sandbox.stop(timeout=workspace.timeout_seconds,force=False)
        else:
            await workspace._sandbox.pause()
            await workspace._sandbox.close()
        workspace._sandbox = None
        if name=='daytona' and workspace._daytona is not None:
            await workspace._daytona.close()
            workspace._daytona = None
