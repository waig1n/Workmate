"""Native, bounded capture from a trusted node-affine Workspace mount.

Call baseline before Core starts and capture only after verified Task quiescence.
These filesystem operations establish no execution authority themselves. Published
content-addressed objects use shared files or private S3, never mutable output paths.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid


class DeliveryFileError(RuntimeError):
    pass


@contextmanager
def _directory(path, *, create=False):
    """Pin each absolute directory component without following a symbolic link."""
    fd = os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in Path(path).absolute().parts[1:]:
            if part in {'.','..'}:
                raise DeliveryFileError('Unsafe directory path')
            if create:
                try:
                    os.mkdir(part,mode=0o700,dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd)
            fd=child
        yield fd
    except OSError as error:
        raise DeliveryFileError('Delivery directory is unavailable or unsafe') from error
    finally:
        os.close(fd)


def _name(value):
    if not isinstance(value,str) or not value or '\\' in value or value.startswith('/'):
        return None
    parts=value.split('/')
    if parts[0]=='output':
        parts=parts[1:]
    if not parts or any(p in {'','.','..'} for p in parts):
        return None
    return '/'.join(parts)


def _fingerprint(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


class DeliveryFiles:
    def __init__(self,workspace_root,storage_root,*,max_file_bytes,max_files,object_store=None):
        if any(type(n) is not int or n<1 for n in (max_file_bytes,max_files)):
            raise ValueError('Positive artifact byte/file limits are required')
        self.object_store=object_store
        self.workspace_root=Path(workspace_root).absolute()
        self.storage_root=Path(storage_root).absolute()
        if (self.storage_root==self.workspace_root or self.storage_root.is_relative_to(self.workspace_root)
                or self.workspace_root.is_relative_to(self.storage_root)):
            raise ValueError('Artifact objects and Workspace mounts must be separate')
        self.max_bytes,self.max_files=max_file_bytes,max_files

    @staticmethod
    def _read(parent,name,*,limit):
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise DeliveryFileError('Only regular delivery files are supported')
            if before.st_size>limit:
                return None,before
            chunks=[]
            size=0
            while chunk:=os.read(fd,min(1024*1024,limit+1-size)):
                size+=len(chunk)
                if size>limit:
                    raise DeliveryFileError('Delivery file grew beyond its bound')
                chunks.append(chunk)
            after=os.fstat(fd)
            current=os.stat(name,dir_fd=parent,follow_symlinks=False)
            if _fingerprint(before)!=_fingerprint(after) or _fingerprint(before)!=_fingerprint(current) or size!=before.st_size:
                raise DeliveryFileError('Delivery file changed during capture')
            return b''.join(chunks),before
        finally:
            os.close(fd)

    def _scan(self):
        result={}
        visited=0
        with _directory(self.workspace_root/'output') as root:
            def walk(fd,prefix='',depth=0):
                nonlocal visited
                if depth>64:
                    raise DeliveryFileError('Output directory nesting exceeds capture limit')
                for name in sorted(os.listdir(fd)):
                    visited+=1
                    if not name or '/' in name or '\\' in name or name in {'.','..'}:
                        raise DeliveryFileError('Unsafe output entry')
                    if visited>10000:
                        raise DeliveryFileError('Output inventory exceeds capture limit')
                    info=os.stat(name,dir_fd=fd,follow_symlinks=False)
                    relative=prefix+name
                    if stat.S_ISDIR(info.st_mode):
                        child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                        try:
                            walk(child,relative+'/',depth+1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        content,info=self._read(fd,name,limit=self.max_bytes)
                        result[relative]={'size':info.st_size,'mtime_ns':info.st_mtime_ns,
                            'sha256':hashlib.sha256(content).hexdigest() if content is not None else None}
                    else:
                        raise DeliveryFileError('Output contains a symlink or special file')
            try:
                walk(root)
            except OSError as error:
                raise DeliveryFileError('Output changed or could not be safely read') from error
        return result

    def _manifest(self):
        try:
            with _directory(self.workspace_root/'.workmate') as fd:
                content,_=self._read(fd,'presented_files.json',limit=256*1024)
            data=json.loads(content) if content is not None else {}
            return data if isinstance(data,dict) and data.get('version')==1 else {}
        except (DeliveryFileError,OSError,ValueError,TypeError):
            # Missing/invalid declarations keep the established fallback policy.
            return {}

    def baseline(self):
        manifest=self._manifest()
        return {'version':1,'files':self._scan(),'presentation_id':manifest.get('call_id')}

    def _publish(self,content,digest):
        key=f'.objects/sha256/{digest[:2]}/{digest}'
        if self.object_store is not None:
            self.object_store.publish(key,content,digest)
            return key
        with _directory(self.storage_root/Path(key).parent,create=True) as fd:
            temporary='.'+uuid.uuid4().hex+'.tmp'
            raw=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            try:
                with os.fdopen(raw,'wb') as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary,digest,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                except FileExistsError:
                    existing,_=self._read(fd,digest,limit=self.max_bytes)
                    if existing is None or hashlib.sha256(existing).hexdigest()!=digest:
                        raise DeliveryFileError('Existing immutable artifact object is corrupt')
                os.fsync(fd)
            except OSError as error:
                raise DeliveryFileError('Artifact object publication failed') from error
            finally:
                os.unlink(temporary,dir_fd=fd)
        return key

    def _select(self,baseline,after,manifest,*,mode,strict):
        declared=[]
        for value in manifest.get('files',[]) if isinstance(manifest.get('files'),list) else []:
            normalized=_name(value)
            if normalized and normalized not in declared:
                declared.append(normalized)
        fresh=bool(manifest.get('call_id') and manifest['call_id']!=baseline.get('presentation_id') and declared)
        warnings=[]
        if mode=='snapshot_fallback':
            source,selected='snapshot',None
        elif fresh:
            source,selected='explicit',declared
        else:
            warnings.append('present_files was not called with valid files during this run')
            source,selected=('strict_no_declaration',[]) if strict else ('snapshot_fallback',None)
        candidates=[]
        for name in selected if selected is not None else sorted(after):
            if name not in after:
                warnings.append(f'declared artifact does not exist: output/{name}')
                continue
            before=baseline['files'].get(name)
            # mtime is a capture stability check, not a content revision.
            same_content=bool(before and before.get('sha256') is not None
                and before.get('sha256')==after[name].get('sha256')
                and before.get('size')==after[name].get('size'))
            change='created' if before is None else 'presented' if same_content else 'modified'
            if selected is not None or change!='presented':
                candidates.append((name,change))
        if len(candidates)>self.max_files:
            warnings.append(f'artifact candidates exceed per-run limit: {len(candidates)} > {self.max_files}')
        return candidates,source,declared,warnings

    def capture(self,baseline,*,mode='hybrid',strict=False):
        if (not isinstance(baseline,dict) or baseline.get('version')!=1 or not isinstance(baseline.get('files'),dict)
                or mode not in {'hybrid','snapshot_fallback'} or type(strict) is not bool):
            raise DeliveryFileError('Unknown delivery baseline or policy')
        after=self._scan()
        manifest=self._manifest()
        candidates,source,declared,warnings=self._select(baseline,after,manifest,mode=mode,strict=strict)
        result=[]
        for name,change in sorted(candidates)[:self.max_files]:
            if after[name]['size']>self.max_bytes:
                warnings.append(f'artifact exceeds configured size limit: output/{name}')
                continue
            path=Path(name)
            with _directory(self.workspace_root/'output'/path.parent) as fd:
                content,info=self._read(fd,path.name,limit=self.max_bytes)
            if (content is None or hashlib.sha256(content).hexdigest()!=after[name]['sha256']
                    or info.st_mtime_ns!=after[name]['mtime_ns']):
                raise DeliveryFileError('Selected output changed after inventory')
            digest=after[name]['sha256']
            result.append({'name':name,'size':len(content),'sha256':digest,
                'storage_key':self._publish(content,digest),'change_type':change})
        if after!=self._scan() or manifest!=self._manifest():
            raise DeliveryFileError('Output or declaration changed before delivery closure')
        return {'version':1,'source':source,'declared_files':declared if source=='explicit' else [],
                'warnings':warnings,'files':result}


def verify_capture_objects(storage_root,payload,object_store=None):
    """Verify only immutable objects; never open a mutable Workspace path."""
    from workmate_product.application.delivery_metadata import _validate
    _validate(payload,capture=True)
    for artifact in payload['files']:
        if object_store is not None:
            snapshot=object_store.snapshot(artifact['storage_key'],artifact['sha256'],artifact['size'])
            snapshot.close()
            continue
        key=Path(artifact['storage_key'])
        with _directory(Path(storage_root).absolute()/key.parent) as fd:
            content,info=DeliveryFiles._read(fd,key.name,limit=artifact['size'])
        if (content is None or len(content)!=artifact['size']
                or hashlib.sha256(content).hexdigest()!=artifact['sha256']):
            raise DeliveryFileError('Sealed artifact object is unavailable or corrupt')
    return True
