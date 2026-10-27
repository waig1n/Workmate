"""Local Workspace exclusion, not a proof that crashed resources have died.

The coordination directory is outside the Agent's Workspace mount. A dirty
record survives process death: acquiring a now-free flock never grants recovery.
Only trusted Worker lifecycle code may mark an orderly, drained close clean.
No code here clears a crash latch or changes Product execution ownership.
"""
import fcntl
import json
import os
import re
from pathlib import Path
import stat
import uuid


from .workspace_ownership import WorkspaceOwnershipError

# Internal on-disk protocol. Changing these requires an explicit format/storage
# migration, not a deployment toggle or a weaker lock fallback.
OWNERSHIP_DIRECTORY_NAME = '.workmate-ownership'
LOCK_FILE_SUFFIX = '.lock'
RECORD_FILE_SUFFIX = '.json'
RECORD_VERSION = 1
RECORD_FIELDS = frozenset({'version','identity','generation','root','workspace','dirty'})
MAX_WORKSPACE_ID_LENGTH = 128
MAX_RECORD_CHARACTERS = 16_384  # The existing format bounds decoded text reads.
WORKSPACE_ID_PATTERN = re.compile(r'[A-Za-z0-9_-]+')
GENERATION_PATTERN = re.compile(r'[0-9a-f]{32}')  # UUID hex, stable for directory lifetime.
# Owner-only creation; existing state must not be writable by group or others.
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
UNTRUSTED_WRITE_BITS = 0o022


def validate_workspace_identity(workspace_id, identity):
    """Reject path traversal and ambiguous identity before touching the filesystem."""
    if (not isinstance(workspace_id,str) or len(workspace_id)>MAX_WORKSPACE_ID_LENGTH
            or WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None
            or not isinstance(identity,dict) or not identity
            or any(not isinstance(k,str) or not isinstance(v,str) or not v for k,v in identity.items())):
        raise WorkspaceOwnershipError('An exact Workspace identity is required')


def _validate_clean_record(record, identity, root_identity, path):
    if (not isinstance(record,dict) or set(record)!=RECORD_FIELDS
            or type(record['version']) is not int or record['version']!=RECORD_VERSION
            or record['identity']!=identity or record['root']!=root_identity
            or record['workspace']!=_identity(path)
            or not isinstance(record['generation'],str)
            or GENERATION_PATTERN.fullmatch(record['generation']) is None
            or record['dirty'] is not False):
        raise WorkspaceOwnershipError('Workspace identity changed or prior ownership requires recovery')


def _identity(path):
    value=path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(value.st_mode):
        raise WorkspaceOwnershipError('Workspace ownership requires real directories')
    return [value.st_dev,value.st_ino]


def _trusted_file(fd):
    value=os.fstat(fd)
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink!=1
            or value.st_uid!=os.geteuid() or value.st_mode & UNTRUSTED_WRITE_BITS):
        raise WorkspaceOwnershipError('Workspace ownership file is not private trusted state')


def _sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PosixWorkspaceOwnership:
    @classmethod
    def acquire(cls,basedir,workspace_id,identity):
        try:
            return cls._acquire(basedir,workspace_id,identity)
        except (OSError,ValueError) as error:
            raise WorkspaceOwnershipError('Workspace ownership state cannot be safely read or persisted') from error

    @classmethod
    def _acquire(cls,basedir,workspace_id,identity):
        validate_workspace_identity(workspace_id,identity)
        root=Path(basedir).absolute()
        root.mkdir(parents=True,exist_ok=True,mode=PRIVATE_DIRECTORY_MODE)
        root_identity=_identity(root)
        state=root/OWNERSHIP_DIRECTORY_NAME
        state.mkdir(mode=PRIVATE_DIRECTORY_MODE,exist_ok=True)
        state_identity=_identity(state)
        mode=state.stat()
        if mode.st_uid!=os.geteuid() or mode.st_mode & UNTRUSTED_WRITE_BITS:
            raise WorkspaceOwnershipError('Ownership directory must be controlled by the Worker')
        lock_path=state/(workspace_id+LOCK_FILE_SUFFIX)
        fd=os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,PRIVATE_FILE_MODE)
        try:
            _trusted_file(fd)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WorkspaceOwnershipError('Workspace is held by another Worker') from error
            lease=cls()
            lease._fd,lease._root,lease._state=fd,root,state
            lease._root_identity,lease._state_identity=root_identity,state_identity
            lease._lock_path=lock_path
            lease._record_path=state/(workspace_id+RECORD_FILE_SUFFIX)
            lease.path=root/workspace_id
            lease._closed=False
            try:
                record_fd=os.open(lease._record_path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
            except FileNotFoundError:
                record=None
                missing=True
                lease._record_identity=None
            else:
                missing=False
                with os.fdopen(record_fd,'r') as source:
                    _trusted_file(source.fileno())
                    info=os.fstat(source.fileno())
                    lease._record_identity=(info.st_dev,info.st_ino)
                    raw=source.read(MAX_RECORD_CHARACTERS+1)
                    if len(raw)>MAX_RECORD_CHARACTERS:
                        raise WorkspaceOwnershipError('Workspace ownership record is oversized')
                    record=json.loads(raw)
            if missing:
                if lease.path.exists() and (not lease.path.is_dir() or any(lease.path.iterdir())):
                    raise WorkspaceOwnershipError('Existing Workspace has no trusted ownership generation')
                lease.path.mkdir(mode=PRIVATE_DIRECTORY_MODE,exist_ok=True)
                record={'version':RECORD_VERSION,'identity':dict(identity),'generation':uuid.uuid4().hex,
                    'root':root_identity,'workspace':_identity(lease.path),'dirty':False}
            _validate_clean_record(record,identity,root_identity,lease.path)
            lease._record=record
            lease.generation=record['generation']
            lease._write(dirty=True)
            # Persist new Workspace/control directory entries and any newly
            # created ancestors before a gateway can begin producing resources.
            for directory in (root,*root.parents):
                _sync_directory(directory)
            return lease
        except BaseException:
            os.close(fd)
            raise

    def _check_paths(self):
        value=os.fstat(self._fd)
        current=self._lock_path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode) or (value.st_dev,value.st_ino)!=(current.st_dev,current.st_ino)
                or _identity(self._root)!=self._root_identity or _identity(self._state)!=self._state_identity
                or _identity(self.path)!=self._record['workspace']):
            raise WorkspaceOwnershipError('Workspace or ownership storage was replaced')

    def _write(self,*,dirty):
        self._check_paths()
        try:
            current=self._record_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            current=None
        actual=None if current is None else (current.st_dev,current.st_ino)
        if actual!=self._record_identity or (current is not None and not stat.S_ISREG(current.st_mode)):
            raise WorkspaceOwnershipError('Workspace ownership record was replaced')
        temporary=self._record_path.with_name(self._record_path.name+'.'+uuid.uuid4().hex)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,PRIVATE_FILE_MODE)
        try:
            with os.fdopen(fd,'w') as target:
                json.dump({**self._record,'dirty':dirty},target,separators=(',',':'))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary,self._record_path)
            info=self._record_path.stat(follow_symlinks=False)
            self._record_identity=(info.st_dev,info.st_ino)
            _sync_directory(self._state)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self,*,clean):
        if type(clean) is not bool:
            raise WorkspaceOwnershipError('Explicit drained-close evidence is required')
        if self._closed:
            return
        if clean:
            self._write(dirty=False)
        # Keep the permanent inode; unlinking flock files permits two holders.
        os.close(self._fd)
        self._closed=True
