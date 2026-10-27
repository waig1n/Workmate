"""Standard-library control helper. Never executed through an agent Task backend."""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

CHUNK = 65536
MARKER = '.workmate-checkpoint-restore'


def safe(value):
    parts = value.split('/')
    if (not value or len(value)>4096 or len(parts)>65 or parts[0] not in ('input','working','output')
            or any(p in ('','.','..') or '\\' in p or any(ord(c)<32 for c in p) for p in parts)):
        raise ValueError('Unsafe path')
    return parts


@contextmanager
def directory(path):
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        yield fd
    finally:os.close(fd)


def fingerprint(s):return [s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink]


def inventory(root, limits):
    files=[];dirs=[];proof={};total=0;visited=0
    def walk(fd,path):
        nonlocal total,visited
        safe(path);dirs.append(path);proof[path]=fingerprint(os.fstat(fd))
        for name in sorted(os.listdir(fd)):
            visited+=1
            if visited>limits['max_files']*4+3:raise ValueError('Inventory too large')
            item=path+'/'+name;safe(item);st=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:walk(child,item)
                finally:os.close(child)
            elif stat.S_ISREG(st.st_mode):
                if st.st_nlink!=1:raise ValueError('Hardlinked file')
                if len(files)>=limits['max_files'] or st.st_size>limits['max_file_bytes'] or total+st.st_size>limits['max_bytes']:raise ValueError('Size limit')
                raw=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                try:
                    if fingerprint(os.fstat(raw))!=fingerprint(st):raise ValueError('Changed file')
                    h=hashlib.sha256();size=0
                    while block:=os.read(raw,CHUNK):
                        size+=len(block)
                        if size>st.st_size:raise ValueError('Growing file')
                        h.update(block)
                    if size!=st.st_size or fingerprint(os.fstat(raw))!=fingerprint(st):raise ValueError('Changed file')
                finally:os.close(raw)
                proof[item]=fingerprint(st);total+=size;digest=h.hexdigest()
                files.append({'path':item,'sha256':digest,'size':size,'executable':bool(st.st_mode&0o111),
                    'storage_key':'.objects/sha256/'+digest[:2]+'/'+digest})
            else:raise ValueError('Special file or symlink')
    with directory(root) as fd:
        proof['.']=fingerprint(os.fstat(fd))
        for name in ('input','working','output'):
            try:child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            except FileNotFoundError:continue
            try:walk(child,name)
            finally:os.close(child)
    for path,expected in proof.items():
        with directory(root if path=='.' else str(Path(root)/Path(path).parent)) as fd:
            actual=os.fstat(fd) if path=='.' else os.stat(Path(path).name,dir_fd=fd,follow_symlinks=False)
        if fingerprint(actual)!=expected:raise ValueError('Inventory changed')
    return {'manifest':{'version':1,'directories':dirs,'files':files,'total_bytes':total},'proof':proof}


def owned(fd):
    os.fchown(fd,62000,62000)


def root_identity(root):
    with directory(root) as fd:
        st=os.fstat(fd)
        return [st.st_dev,st.st_ino]


def marker(control, root, attempt):
    with directory(control) as fd:
        raw=os.open(MARKER,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
        with os.fdopen(raw) as stream:
            if os.fstat(stream.fileno()).st_nlink!=1:raise ValueError('Unsafe restore marker')
            state=json.load(stream)
        if (state.get('attempt')!=attempt or state.get('status')!='installing'
                or state.get('root_identity')!=root_identity(root)):
            raise ValueError('Restore attempt or root identity differs')
        return state['root_identity']


def execute(root, action, payload):
    control=payload.get('_control')
    if action=='inventory':return inventory(root,payload)
    if action=='read':
        safe(payload['path']);path=Path(payload['path'])
        with directory(str(Path(root)/path.parent)) as parent:
            fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            try:
                if os.fstat(fd).st_nlink!=1 or fingerprint(os.fstat(fd))!=payload['fingerprint']:raise ValueError('Changed file')
                os.lseek(fd,payload['offset'],os.SEEK_SET);block=os.read(fd,CHUNK)
                if os.fstat(fd).st_nlink!=1 or fingerprint(os.fstat(fd))!=payload['fingerprint']:raise ValueError('Changed file')
                return {'data':base64.b64encode(block).decode()}
            finally:os.close(fd)
    if action=='begin':
        identity=root_identity(root)
        with directory(root) as fd:
            for name in ('input','working','output'):
                try:child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                except FileNotFoundError:continue
                try:
                    if os.listdir(child):raise ValueError('Target is not empty')
                finally:os.close(child)
        with directory(control) as fd:
            raw=os.open(MARKER,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            with os.fdopen(raw,'w') as stream:
                json.dump({'attempt':payload['attempt'],'status':'installing','root_identity':identity},stream);stream.flush();os.fsync(stream.fileno())
            os.fsync(fd)
        if root_identity(root)!=identity:raise ValueError('Restore root changed during admission')
        return {}
    identity=marker(control,root,payload['attempt'])
    if action=='mkdir':
        path=Path(payload['path']);safe(str(path))
        with directory(str(Path(root)/path.parent)) as fd:
            try:os.mkdir(path.name,0o700,dir_fd=fd)
            except FileExistsError:
                if len(path.parts)!=1:raise
            child=os.open(path.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:owned(child)
            finally:os.close(child)
    elif action=='write':
        path=Path(payload['path']);safe(str(path));block=base64.b64decode(payload['data'],validate=True)
        if len(block)>CHUNK:raise ValueError('Chunk too large')
        with directory(str(Path(root)/path.parent)) as parent:
            flags=os.O_WRONLY|os.O_NOFOLLOW|os.O_NONBLOCK
            if payload['offset']==0:flags|=os.O_CREAT|os.O_EXCL
            fd=os.open(path.name,flags,0o600,dir_fd=parent)
            try:
                st=os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size!=payload['offset']:raise ValueError('Write offset differs')
                os.lseek(fd,payload['offset'],os.SEEK_SET)
                while block:block=block[os.write(fd,block):]
                os.fsync(fd)
                if os.fstat(fd).st_nlink!=1:raise ValueError('Hardlink appeared during write')
            finally:os.close(fd)
    elif action=='finish-file':
        path=Path(payload['path']);safe(str(path))
        with directory(str(Path(root)/path.parent)) as parent:
            fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            try:
                st=os.fstat(fd);h=hashlib.sha256();size=0
                if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size!=payload['size']:raise ValueError('Restored size differs')
                while block:=os.read(fd,CHUNK):h.update(block);size+=len(block)
                if size!=payload['size'] or h.hexdigest()!=payload['sha256'] or fingerprint(st)!=fingerprint(os.fstat(fd)):raise ValueError('Restored digest differs')
                os.fchmod(fd,0o700 if payload['executable'] else 0o600);owned(fd);os.fsync(fd)
            finally:os.close(fd)
    elif action=='finish':
        captured=inventory(root,payload['limits'])['manifest']
        captured['files']=sorted(captured['files'],key=lambda f:f['path'])
        captured['directories']=sorted(captured['directories'])
        digest=hashlib.sha256(json.dumps(captured,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if digest!=payload['manifest_digest']:raise ValueError('Restored inventory differs')
        if root_identity(root)!=identity:raise ValueError('Restore root changed during verification')
        with directory(control) as fd:
            raw=os.open(MARKER,os.O_WRONLY|os.O_NOFOLLOW|os.O_TRUNC,dir_fd=fd)
            with os.fdopen(raw,'w') as stream:
                json.dump({'attempt':payload['attempt'],'status':'complete','root_identity':identity},stream);stream.flush();os.fsync(stream.fileno())
            os.fsync(fd)
    else:raise ValueError('Unknown operation')
    if root_identity(root)!=identity:raise ValueError('Restore root changed during operation')
    return {}

if __name__=='__main__':
    root,action,payload=sys.argv[1:]
    print(json.dumps(execute(root,action,json.loads(payload)),separators=(',',':')))
