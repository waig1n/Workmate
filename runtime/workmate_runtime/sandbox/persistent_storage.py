"""Storage continuity for compute replacement; never an execution-authority grant."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from .base import WorkspaceBindingError

SUPPORTED = frozenset({'docker', 'kubernetes'})
MARKER_NAME = '.workmate-storage-generation'


def validate_storage_proof(value):
    if (not isinstance(value, dict) or set(value) != {'version','provider','resource_id','storage','marker'}
            or type(value['version']) is not int or value['version'] != 1
            or value['provider'] not in SUPPORTED
            or not isinstance(value['resource_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value['resource_id'])
            or not isinstance(value['marker'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['marker'])):
        raise WorkspaceBindingError('Invalid persistent storage proof')
    keys = {'name','driver','created_at'} if value['provider']=='docker' else {'name','namespace','uid'}
    storage = value['storage']
    if (not isinstance(storage, dict) or set(storage) != keys
            or any(not isinstance(v,str) or not v.strip() or len(v)>512 for v in storage.values())):
        raise WorkspaceBindingError('Invalid persistent storage identity')
    if value['provider']=='docker' and storage['name'] != 'workmate-'+value['resource_id']:
        raise WorkspaceBindingError('Volume does not belong to Workspace')
    return deepcopy(value)


async def read_storage_identity(workspace):
    try:
        if workspace._workmate_provider == 'docker':
            name='workmate-'+workspace.workspace_id
            volume=await workspace._client.volumes.get(name)
            data=await volume.show()
            identity={'name':data['Name'],'driver':data['Driver'],'created_at':data['CreatedAt']}
            if identity['name'] != name:
                raise ValueError('Volume name differs')
        elif workspace._workmate_provider == 'kubernetes':
            pvc=await workspace._v1.read_namespaced_persistent_volume_claim(workspace._pod_name,workspace._namespace)
            if (pvc.metadata.deletion_timestamp is not None or pvc.metadata.name != workspace._pod_name
                    or pvc.metadata.namespace != workspace._namespace):
                raise ValueError('PVC unavailable or changed')
            identity={'name':pvc.metadata.name,'namespace':pvc.metadata.namespace,'uid':pvc.metadata.uid}
        else:
            raise ValueError('Unsupported persistent provider')
        if any(not isinstance(v,str) or not v.strip() for v in identity.values()):
            raise ValueError('Storage identity is incomplete')
        return identity
    except Exception as error:
        raise WorkspaceBindingError('Original persistent storage is unavailable or unverifiable') from error


async def verify_storage_identity(workspace, proof):
    expected=validate_storage_proof(proof)
    if (expected['provider'] != workspace._workmate_provider or expected['resource_id'] != workspace.workspace_id
            or await read_storage_identity(workspace) != expected['storage']):
        raise WorkspaceBindingError('Persistent storage identity changed; refusing empty or replacement storage')
    return expected


# Fixed trusted code, usable in protected builds without inspect.getsource.
# The marker is root-only even though Agent owns its parent: deletion is denial,
# never authorization to create a new generation during reuse.
_MARKER_SCRIPT = r'''import os,stat,sys
root,expected,operation=sys.argv[1:]
if os.geteuid()!=0: raise RuntimeError('Root control required')
fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
try:
 for part in root.split('/')[1:]:
  if not part or part in ('.','..'): raise RuntimeError('Invalid root')
  child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
  os.close(fd);fd=child
 flags=os.O_NOFOLLOW|os.O_NONBLOCK
 if operation=='create':
  raw=os.open('.workmate-storage-generation',flags|os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd)
  try:
   os.fchown(raw,0,0);os.fchmod(raw,0o600)
   os.write(raw,expected.encode());os.fsync(raw)
  finally: os.close(raw)
  os.fsync(fd)
 elif operation!='verify': raise RuntimeError('Invalid operation')
 raw=os.open('.workmate-storage-generation',flags|os.O_RDONLY,dir_fd=fd)
 try:
  info=os.fstat(raw)
  if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=0 or info.st_gid!=0 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size!=64: raise RuntimeError('Unsafe marker')
  if os.read(raw,65)!=expected.encode(): raise RuntimeError('Storage generation changed')
  after=os.stat('.workmate-storage-generation',dir_fd=fd,follow_symlinks=False)
  if (after.st_dev,after.st_ino,after.st_mtime_ns,after.st_size)!=(info.st_dev,info.st_ino,info.st_mtime_ns,info.st_size): raise RuntimeError('Marker changed')
 finally: os.close(raw)
finally: os.close(fd)
print('verified')
'''


async def check_marker(control, workdir, marker, *, create):
    if not isinstance(marker,str) or not re.fullmatch(r'[0-9a-f]{64}',marker):
        raise WorkspaceBindingError('Invalid storage generation')
    from .remote_backend import joined
    result=await joined(control.exec_shell([control.python,'-I','-c',_MARKER_SCRIPT,workdir,marker,
                                           'create' if create else 'verify'],timeout=30))
    if result.exit_code != 0 or result.stdout.strip() not in (b'verified','verified'):
        raise WorkspaceBindingError('Persistent storage marker missing, replaced or unsafe')


def save_placement_proof(path, proof):
    proof=validate_storage_proof(proof)
    if path is None:
        return
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or info.st_mode&0o077:
            raise WorkspaceBindingError('Unsafe trusted placement record')
        record=json.load(stream)
    if record.get('provider') != proof['provider'] or record.get('resource_id') != proof['resource_id']:
        raise WorkspaceBindingError('Placement identity changed')
    if 'storage_proof' in record:
        if record['storage_proof'] != proof:
            raise WorkspaceBindingError('Persistent placement proof cannot be replaced')
        return
    record['storage_proof']=proof
    name=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',prefix=path.name+'.',dir=path.parent,delete=False) as stream:
            name=stream.name
            json.dump(record,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        if name and os.path.exists(name):os.unlink(name)


async def initialize_storage_proof(workspace):
    expected=getattr(workspace,'_workmate_expected_storage_proof',None)
    if expected is not None:
        proof=await verify_storage_identity(workspace,expected)
        await check_marker(workspace._workmate_control,workspace.workdir,proof['marker'],create=False)
    else:
        proof={'version':1,'provider':workspace._workmate_provider,'resource_id':workspace.workspace_id,
               'storage':await read_storage_identity(workspace),'marker':secrets.token_hex(32)}
        validate_storage_proof(proof)
        await check_marker(workspace._workmate_control,workspace.workdir,proof['marker'],create=True)
    save_placement_proof(getattr(workspace,'_workmate_placement_path',None),proof)
    workspace._workmate_storage_proof=proof
    return deepcopy(proof)
