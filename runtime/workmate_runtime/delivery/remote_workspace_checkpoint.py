"""Bounded checkpoint transport over Docker/Kubernetes trusted control channels.

Callers own resource-quiescence proof and must not bind a failed restore. Object
credentials and snapshots remain on the Worker, never enter command payloads.
"""
import asyncio
import base64
import hashlib
import inspect
import json
import posixpath
import tempfile
import uuid
from . import remote_checkpoint_script
from .workspace_checkpoint import WorkspaceCheckpoint, CheckpointError
from ..sandbox.base import TrustedSandboxControlBackend

_SCRIPT=inspect.getsource(remote_checkpoint_script)
CHUNK=65536


async def _joined(awaitable, *, cleanup=None):
    task=asyncio.ensure_future(awaitable)
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:await asyncio.shield(task)
            except asyncio.CancelledError:pass
            except Exception:break
        if task.done() and not task.cancelled():
            error=task.exception()
            if error is None and cleanup is not None:cleanup(task.result())
        raise


class RemoteWorkspaceCheckpoint:
    def __init__(self,backend,workdir,object_store,*,provider,max_files,max_bytes,max_file_bytes=None):
        if not isinstance(backend,TrustedSandboxControlBackend):
            raise TypeError('Remote checkpoint requires trusted control backend')
        if provider not in ('docker','kubernetes'):
            raise ValueError('Remote checkpoint provider not supported')
        control=getattr(backend,'directory',None)
        for path in (workdir,control):
            if not isinstance(path,str) or not path.startswith('/') or path=='/' or posixpath.normpath(path)!=path:
                raise ValueError('Canonical workspace and control directories required')
        if control==workdir or control.startswith(workdir+'/'):
            raise ValueError('Restore marker requires private control directory')
        self.backend,self.root,self.control=backend,workdir,control
        self.local=WorkspaceCheckpoint(object_store,max_files=max_files,max_bytes=max_bytes,max_file_bytes=max_file_bytes)
        self.store=object_store
        self.limits={'max_files':max_files,'max_bytes':max_bytes,'max_file_bytes':self.local.max_file_bytes}

    async def _request(self,action,**payload):
        payload['_control']=self.control
        result=await _joined(self.backend.exec_shell([getattr(self.backend,'python','python3'),'-I','-c',_SCRIPT,
            self.root,action,json.dumps(payload,separators=(',',':'))],timeout=120))
        if result.exit_code!=0:raise CheckpointError('Remote checkpoint operation failed; target must remain unbound')
        # Inventory response is metadata bounded by configured file/directory counts.
        bound=max(CHUNK*2,self.limits['max_files']*24000)
        if len(result.stdout)>bound:raise CheckpointError('Remote checkpoint response exceeds limit')
        try:
            value=json.loads(result.stdout)
            if not isinstance(value,dict):raise ValueError()
            return value
        except (ValueError,TypeError) as error:raise CheckpointError('Invalid remote checkpoint response') from error

    async def capture(self):
        inventory=await self._request('inventory',**self.limits)
        manifest=inventory['manifest'];files,_=self.local._validate(manifest)
        for entry in files:
            with tempfile.TemporaryFile() as spool:
                digest=hashlib.sha256();offset=0
                while offset<entry['size']:
                    result=await self._request('read',path=entry['path'],offset=offset,
                        fingerprint=inventory['proof'][entry['path']])
                    try:block=base64.b64decode(result['data'],validate=True)
                    except (KeyError,ValueError,TypeError) as error:raise CheckpointError('Invalid remote file chunk') from error
                    if not block or len(block)>CHUNK or offset+len(block)>entry['size']:raise CheckpointError('Remote chunk size differs')
                    spool.write(block);digest.update(block);offset+=len(block)
                if digest.hexdigest()!=entry['sha256']:raise CheckpointError('Remote file checksum differs')
                spool.seek(0)
                await _joined(asyncio.to_thread(self.store.publish_file,entry['storage_key'],spool,entry['sha256'],entry['size']))
        if inventory!=await self._request('inventory',**self.limits):raise CheckpointError('Workspace changed during checkpoint')
        return manifest

    async def restore(self,manifest):
        files,dirs=self.local._validate(manifest);snapshots=[]
        try:
            # Validate every object locally before the first mutation remotely.
            for entry in files:
                snapshot=await _joined(asyncio.to_thread(self.store.snapshot,entry['storage_key'],entry['sha256'],entry['size']),cleanup=lambda item:item.close())
                snapshots.append(snapshot)
                def verify():
                    h=hashlib.sha256();count=0
                    with snapshot.path.open('rb') as stream:
                        while block:=stream.read(CHUNK):
                            count+=len(block)
                            if count>entry['size']:raise CheckpointError('Object grew beyond declared size')
                            h.update(block)
                    if count!=entry['size'] or h.hexdigest()!=entry['sha256']:raise CheckpointError('Object checksum differs')
                await _joined(asyncio.to_thread(verify))
            attempt=uuid.uuid4().hex
            await self._request('begin',attempt=attempt)
            for path in sorted(dirs,key=lambda p:(p.count('/'),p)):
                await self._request('mkdir',attempt=attempt,path=path)
            for entry,snapshot in zip(files,snapshots):
                offset=0
                with snapshot.path.open('rb') as stream:
                    while True:
                        block=stream.read(CHUNK)
                        if offset==0 or block:
                            await self._request('write',attempt=attempt,path=entry['path'],offset=offset,data=base64.b64encode(block).decode())
                        offset+=len(block)
                        if not block:break
                await self._request('finish-file',attempt=attempt,**entry)
            normalized={**manifest,'files':sorted(files,key=lambda f:f['path']),'directories':sorted(dirs)}
            digest=hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            await self._request('finish',attempt=attempt,manifest_digest=digest,limits=self.limits)
        finally:
            for snapshot in snapshots:snapshot.close()
