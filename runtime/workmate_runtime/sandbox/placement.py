"""Pin provider and remote resource namespace beside the trusted ownership lease."""
import json
import os
import secrets
import stat
from pathlib import Path

from .base import SandboxWorkspaceRequest, WorkspaceBindingError


def workspace_request(basedir, workspace_id, local_workdir, provider, *, expected_storage_proof=None):
    path = Path(basedir)/('.sandbox-'+workspace_id+'.json')
    if (provider!='bubblewrap' and not path.exists() and local_workdir is not None
            and Path(local_workdir).exists() and any(Path(local_workdir).iterdir())):
        raise WorkspaceBindingError('Existing local Workspace requires explicit migration')
    # Kubernetes also uses this identity as a label value (maximum 63 bytes).
    resource_id = workspace_id if provider == 'bubblewrap' else secrets.token_hex(24 if provider == 'kubernetes' else 32)
    record = {'provider':provider,'resource_id':resource_id}
    try:
        fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise WorkspaceBindingError('Unsafe trusted placement record')
            try:
                existing = json.load(stream)
            except (ValueError,OSError) as error:
                raise WorkspaceBindingError('Sandbox placement record is unreadable') from error
        if existing.get('provider')!=provider or not isinstance(existing.get('resource_id'),str):
            raise WorkspaceBindingError('Workspace provider cannot change without explicit migration')
        record = existing
    else:
        with os.fdopen(fd,'w') as stream:
            json.dump(record,stream)
            stream.flush()
            os.fsync(stream.fileno())
    from .persistent_storage import validate_storage_proof
    stored = record.get('storage_proof')
    if stored is not None:
        stored = validate_storage_proof(stored)
        if stored['provider'] != provider or stored['resource_id'] != record['resource_id']:
            raise WorkspaceBindingError('Persistent placement differs from Workspace')
    if expected_storage_proof is not None:
        expected = validate_storage_proof(expected_storage_proof)
        if stored != expected:
            raise WorkspaceBindingError('Cold recovery proof differs from trusted placement')
    return SandboxWorkspaceRequest(record['resource_id'],local_workdir,stored,path)
