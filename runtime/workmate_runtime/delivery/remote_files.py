"""Bounded read-only remote capture through the provider's control channel.

Never launch through the Task backend: capture runs after its scope is sealed.
Each selected file is transferred independently, preserving existing file/count
limits without adding an aggregate limit or buffering the entire output tree.
"""
import base64
import hashlib
import inspect
import json
import posixpath

from workmate_runtime.sandbox.base import TrustedSandboxControlBackend
from . import files as file_module
from .files import DeliveryFiles, DeliveryFileError, _name

# Reuse exactly the local no-follow traversal, inode stability checks and bounds
# in the sandbox; this script performs no publication or other remote writes.
_CAPTURE_SCRIPT = inspect.getsource(file_module) + r'''
import base64,sys
root,action,limit,name=sys.argv[1:]
files=DeliveryFiles(root,'/__workmate_unused_delivery_storage__',max_file_bytes=int(limit),max_files=1)
if action=='inventory':
    print(json.dumps({'files':files._scan(),'manifest':files._manifest()}))
elif action=='read':
    if _name(name)!=name: raise DeliveryFileError('Unsafe selected output path')
    path=Path(name)
    with _directory(Path(root)/'output'/path.parent) as fd:
        content,info=files._read(fd,path.name,limit=int(limit))
    if content is None: raise DeliveryFileError('Selected file exceeds limit')
    print(json.dumps({'data':base64.b64encode(content).decode(),'mtime_ns':info.st_mtime_ns}))
else:
    raise DeliveryFileError('Unknown read-only operation')
'''


class RemoteDeliveryFiles(DeliveryFiles):
    def __init__(self,backend,workdir,storage_root,*,max_file_bytes,max_files,object_store=None):
        if not isinstance(backend,TrustedSandboxControlBackend):
            raise TypeError('Remote capture requires a trusted sandbox control backend')
        if (not isinstance(workdir,str) or not workdir.startswith('/')
                or posixpath.normpath(workdir)!=workdir or workdir=='/'):
            raise ValueError('Remote capture requires a canonical workspace directory')
        super().__init__('/__workmate_remote_capture_workspace__',storage_root,
            max_file_bytes=max_file_bytes,max_files=max_files,object_store=object_store)
        self.backend,self.workdir=backend,workdir

    async def _request(self,action,name=''):
        result=await self.backend.exec_shell(['python3','-I','-c',_CAPTURE_SCRIPT,
            self.workdir,action,str(self.max_bytes),name],timeout=120)
        if result.exit_code!=0:
            raise DeliveryFileError('Remote output changed or could not be safely read')
        try:
            value=json.loads(result.stdout)
            if not isinstance(value,dict):
                raise ValueError('Invalid capture response')
            return value
        except (ValueError,TypeError) as error:
            raise DeliveryFileError('Invalid remote capture response') from error

    async def baseline(self):
        inventory=await self._request('inventory')
        return {'version':1,'files':inventory['files'],
            'presentation_id':inventory['manifest'].get('call_id')}

    async def capture(self,baseline,*,mode='hybrid',strict=False):
        if (not isinstance(baseline,dict) or baseline.get('version')!=1
                or not isinstance(baseline.get('files'),dict)
                or mode not in {'hybrid','snapshot_fallback'} or type(strict) is not bool):
            raise DeliveryFileError('Unknown delivery baseline or policy')
        inventory=await self._request('inventory')
        after,manifest=inventory['files'],inventory['manifest']
        candidates,source,declared,warnings=self._select(baseline,after,manifest,mode=mode,strict=strict)
        captured=[]
        for name,change in sorted(candidates)[:self.max_files]:
            if _name(name)!=name:
                raise DeliveryFileError('Unsafe selected output path')
            if after[name]['size']>self.max_bytes:
                warnings.append(f'artifact exceeds configured size limit: output/{name}')
                continue
            response=await self._request('read',name)
            try:
                content=base64.b64decode(response['data'],validate=True)
            except (ValueError,TypeError,KeyError) as error:
                raise DeliveryFileError('Invalid remote file bytes') from error
            digest=hashlib.sha256(content).hexdigest()
            if (len(content)>self.max_bytes or len(content)!=after[name]['size']
                    or digest!=after[name]['sha256'] or response['mtime_ns']!=after[name]['mtime_ns']):
                raise DeliveryFileError('Selected output changed after inventory')
            # Join immutable publication on cancellation, like local capture.
            from .coordinator import _joined_io
            key=await _joined_io(self._publish,content,digest)
            captured.append({'name':name,'size':len(content),'sha256':digest,
                'storage_key':key,'change_type':change})
        if inventory!=await self._request('inventory'):
            raise DeliveryFileError('Output or declaration changed before delivery closure')
        return {'version':1,'source':source,'declared_files':declared if source=='explicit' else [],
            'warnings':warnings,'files':captured}
