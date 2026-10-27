"""One governed command/file adapter shared by the remote Workspace backends."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import uuid

from agentscope.tool import BackendBase
from workmate_runtime.execution.resources import TrustedTaskBackend
from .base import TrustedSandboxControlBackend, WorkspaceBindingError

TASK_UID = 62000


async def joined(operation):
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Do not abandon a root control request that could still launch a child.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()
        raise


class RemoteControlBackend(TrustedSandboxControlBackend, BackendBase):
    """Trusted root channel. Never bind this object to Agent tools."""
    def __init__(self, raw, *, python, prefix, directory, workdir):
        self.raw, self.python, self.prefix = raw, python, prefix
        self.directory, self.workdir = directory, workdir

    async def exec_shell(self, command, *, cwd=None, timeout=None):
        # Use an absolute trusted Python to exec root commands under a clean environment.
        script = 'import os,sys;os.chdir(sys.argv[1]);os.execvpe(sys.argv[2],sys.argv[2:],{"PATH":"/usr/local/bin:/usr/bin:/bin","HOME":"/root","LANG":"C.UTF-8"})'
        return await self.raw.exec_shell([*self.prefix, self.python, '-I', '-c', script,
            cwd or self.workdir, *command], timeout=timeout)

    async def read_file(self, path):
        result = await self.exec_shell([self.python, '-I', '-c',
            'import base64,sys;print(base64.b64encode(open(sys.argv[1],"rb").read()).decode())', path])
        if result.exit_code:
            raise FileNotFoundError(path)
        return base64.b64decode(result.stdout, validate=False)

    async def write_file(self, path, data):
        # Trusted bootstrap only; callers never pass model-supplied paths here.
        script = ('import base64,os,sys;flags=os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW|'
            '(os.O_TRUNC if sys.argv[2]=="0" else os.O_APPEND);'
            'fd=os.open(sys.argv[1],flags,0o600);'
            'f=os.fdopen(fd,"wb");f.write(base64.b64decode(sys.argv[3]));f.close()')
        for offset in range(0,max(1,len(data)),32768):
            result = await self.exec_shell([self.python, '-I', '-c', script, path, str(offset),
                base64.b64encode(data[offset:offset+32768]).decode()])
            if result.exit_code:
                raise WorkspaceBindingError('Remote control installation failed')

    async def operation(self, operation, **payload):
        result = await self.exec_shell([self.python, '-I', self.directory+'/control.py',
            operation, json.dumps(payload)], timeout=5)
        if result.exit_code:
            raise WorkspaceBindingError('Remote execution control operation failed')
        return json.loads(result.stdout)

    async def getcwd(self):
        return self.workdir


class RemoteTaskBackend(TrustedTaskBackend, BackendBase):
    """Dedicated-UID execution with root-side epoch admission and cleanup proof."""
    def __init__(self, control, owner, registry, workspace):
        self.control, self.workspace = control, workspace
        self._execution_owner, self._task_resources = owner, registry
        self.epoch = hashlib.sha256(json.dumps([owner.session_id, owner.task_id,
            owner.worker_id, owner.execution_epoch]).encode()).hexdigest()
        self._lock = asyncio.Lock()
        self._started = False
        self._clean = False
        resource = 'sandbox:'+self.epoch
        registry.register_external(owner, resource, tool_call_id=resource,
            cleanup=self.cleanup, probe=self.quiescent)

    async def _begin(self):
        async with self._lock:
            if not self._started:
                await joined(self.control.operation('begin', epoch=self.epoch))
                self._started = True
                gateway = getattr(self.workspace,'_gateway',None)
                if gateway is not None:
                    gateway.bind_epoch(self.epoch)

    async def exec_shell(self, command, *, cwd=None, timeout=None):
        self._task_resources._scope(self._execution_owner, launching=True)
        await self._begin()
        self._task_resources._scope(self._execution_owner, launching=True)
        # launcher checks the epoch under the same root-side lock used by seal.
        argv = [self.control.python, '-I', self.control.directory+'/launch.py', str(TASK_UID),
            '--epoch', self.epoch, '--']
        if cwd is not None:
            command = [self.control.python, '-I', '-c',
                'import os,sys;os.chdir(sys.argv[1]);os.execvp(sys.argv[2],sys.argv[2:])',cwd,*command]
        return await self.control.exec_shell([*argv,*command], timeout=timeout)

    async def read_file(self, path):
        result = await self.exec_shell([self.control.python, '-I', '-c',
            'import base64,sys;print(base64.b64encode(open(sys.argv[1],"rb").read()).decode())', path])
        if result.exit_code:
            raise FileNotFoundError(path)
        return base64.b64decode(result.stdout)

    async def write_file(self, path, data):
        # Chunking avoids argv limits imposed by SDKs and Linux execve.
        await self.write_stream(path, _chunks(data))

    async def write_stream(self, path, data):
        script = ('import base64,os,sys;os.makedirs(os.path.dirname(sys.argv[1]) or ".",exist_ok=True);'
            'f=open(sys.argv[1],sys.argv[2]);f.write(base64.b64decode(sys.argv[3]));f.close()')
        mode = 'wb'
        async for chunk in data:
            for index in range(0,len(chunk),32768):
                result = await self.exec_shell([self.control.python,'-I','-c',script,path,mode,
                    base64.b64encode(chunk[index:index+32768]).decode()])
                if result.exit_code:
                    raise OSError('Remote file write failed')
                mode = 'ab'
        if mode == 'wb':
            result = await self.exec_shell([self.control.python,'-I','-c',script,path,mode,''])
            if result.exit_code:
                raise OSError('Remote empty file write failed')

    async def _exec_with_input(self, command, data):
        path = '/tmp/workmate-input-'+uuid.uuid4().hex
        try:
            await self.write_file(path,data)
            return await self.exec_shell([self.control.python,'-I','-c',
                'import os,sys;fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW);os.unlink(sys.argv[1]);os.dup2(fd,0);os.close(fd);os.execvp(sys.argv[2],sys.argv[2:])',path,*command])
        finally:
            # The script unlinks before executing; cleanup kills an interrupted writer.
            pass

    async def getcwd(self):
        return self.control.workdir

    async def cleanup(self):
        await self._begin()
        await joined(self.control.operation('seal',epoch=self.epoch))
        gateway = getattr(self.workspace,'_gateway',None)
        try:
            if gateway is not None:
                await gateway.reset()
        finally:
            await joined(self.control.operation('kill',epoch=self.epoch))
        # Reset must succeed too, otherwise a remote MCP result is unknown.
        if hasattr(self.workspace,"_native_mcp_connections"):
            self.workspace._native_mcp_connections = {}
        self._clean = True

    async def quiescent(self):
        if not self._clean:
            return False
        result = await self.control.operation('probe')
        return result['epoch']==self.epoch and result['active'] is False and not result['pids']


async def _chunks(data):
    yield data
