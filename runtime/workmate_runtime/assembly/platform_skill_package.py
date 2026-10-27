"""Package working/ Skills only through the tracked Task sandbox backend."""
import base64
import json
import posixpath
from workmate_runtime.execution.resources import TrustedTaskBackend
from workmate_product.application.skill_artifacts import parse_skill_archive


PACKAGE_SCRIPT=r'''
import os,sys,stat,io,zipfile,base64,json
opened=[]
try:
    current=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
    opened.append(current)
    for part in sys.argv[1].strip('/').split('/'):
        if part in {'','.','..'}: raise RuntimeError('unsafe Skill path')
        current=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=current)
        opened.append(current)
    files=[]
    size=0
    visited=0
    def walk(fd,prefix='',depth=0):
        global size,visited
        if depth>32: raise RuntimeError('Skill tree too deep')
        with os.scandir(fd) as stream:
            names=[]
            for entry in stream:
                visited+=1
                if visited>1000: raise RuntimeError('Skill tree too large')
                names.append(entry.name)
        for name in sorted(names):
            if name in {'','.','..'} or '/' in name or chr(92) in name: raise RuntimeError('unsafe name')
            info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
                try: walk(child,prefix+name+'/',depth+1)
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
                source=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=fd)
                try:
                    before=os.fstat(source)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise RuntimeError('unsafe file')
                    parts=[]
                    while chunk:=os.read(source,65536):
                        size+=len(chunk)
                        if size>20971520: raise RuntimeError('Skill exceeds 20 MiB')
                        parts.append(chunk)
                    after=os.fstat(source)
                    pathstat=os.stat(name,dir_fd=fd,follow_symlinks=False)
                    def fingerprint(s): return (s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink)
                    if fingerprint(before)!=fingerprint(after) or fingerprint(after)!=fingerprint(pathstat):
                        raise RuntimeError('Skill changed during capture')
                    files.append((prefix+name,b''.join(parts),bool(before.st_mode&0o111)))
                    if len(files)>500: raise RuntimeError('Skill exceeds 500 files')
                finally: os.close(source)
            else: raise RuntimeError('Skill contains linked or special entry')
    walk(current)
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
        for name,data,executable in files:
            entry=zipfile.ZipInfo(name,(1980,1,1,0,0,0))
            entry.compress_type=zipfile.ZIP_DEFLATED
            entry.external_attr=((stat.S_IFREG|(0o755 if executable else 0o644))<<16)
            bundle.writestr(entry,data)
    raw=output.getvalue()
    if len(raw)>20971520: raise RuntimeError('Skill archive exceeds 20 MiB')
    print(json.dumps({'archive':base64.b64encode(raw).decode('ascii')}))
finally:
    for fd in reversed(opened): os.close(fd)
'''


async def package_skill(binding,relative):
    if (binding is None or not isinstance(binding.backend,TrustedTaskBackend)
            or binding.backend._execution_owner!=binding.owner):
        raise RuntimeError('Skill packaging requires a tracked Task backend')
    if (not isinstance(relative,str) or '\\' in relative or relative.startswith('/')
            or relative.split('/')[0]!='working' or any(p in {'','.','..'} for p in relative.split('/'))):
        raise ValueError('Skill directory must be a safe path under working/')
    root=binding.workspace.workdir
    if not isinstance(root,str) or not root.startswith('/') or posixpath.normpath(root)!=root:
        raise RuntimeError('Workspace must have a canonical absolute path')
    result=await binding.backend._exec_with_input(['python3','-I','-c',PACKAGE_SCRIPT,
        posixpath.join(root,relative)],b'')
    if result.exit_code!=0:
        raise RuntimeError('Skill packaging failed')
    value=json.loads(result.stdout)
    raw=base64.b64decode(value['archive'],validate=True)
    parsed=parse_skill_archive(raw)
    return raw,{'name':parsed.name,'description':parsed.description,'files':len(parsed.files),
        'expanded_bytes':sum(len(v) for v in parsed.files.values()),'archive_bytes':len(raw),'sha256':parsed.digest}
