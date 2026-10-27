"""Product-owned immutable Skill source packages; no App or Core imports.

Archive bytes are validated before persistence or materialization. No ZIP entry
is extracted onto the API/Worker host. Only the authorized Task backend receives
normalized, verified files in the Worker adapter.
"""
from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path,PurePosixPath
import re
import stat
from types import MappingProxyType
import zipfile

import yaml

MAX_ARCHIVE_BYTES=20*1024*1024
MAX_EXPANDED_BYTES=100*1024*1024
MAX_ARCHIVE_FILES=10000
_NAME=re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_CATALOG_NAME=re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')


class SkillArtifactError(ValueError):
    """Skill content or current Product authorization is unavailable."""


@dataclass(frozen=True)
class SkillArtifact:
    digest: str
    archive: bytes
    name: str
    description: str
    version: str | None
    markdown: str
    files: object
    manifest: tuple[tuple[str,str,int,bool],...]


def _path(value):
    path=PurePosixPath(value)
    if (not value or len(value)>4096 or len(path.parts)>64 or any(len(p)>255 for p in path.parts) or value.startswith('/') or '\\' in value or any(ord(c)<32 for c in value)
            or any(part in {'','..','.'} for part in value.rstrip('/').split('/'))
            or path.is_absolute() or ':' in path.parts[0]):
        raise SkillArtifactError('Skill archive contains an unsafe path')
    return path


def parse_skill_archive(raw):
    if not isinstance(raw,bytes) or not raw or len(raw)>MAX_ARCHIVE_BYTES:
        raise SkillArtifactError('Skill archive must contain at most 20 MiB')
    try:
        with zipfile.ZipFile(BytesIO(raw)) as bundle:
            members=bundle.infolist()
            if not members or len(members)>MAX_ARCHIVE_FILES:
                raise SkillArtifactError('Skill archive contains too many entries or is empty')
            seen,files,executable=set(),{},{}
            expanded=0
            for info in members:
                path=_path(info.filename)
                normalized=path.as_posix().rstrip('/')
                if normalized in seen:
                    raise SkillArtifactError('Skill archive contains duplicate paths')
                seen.add(normalized)
                kind=stat.S_IFMT(info.external_attr>>16)
                if kind not in {0,stat.S_IFREG,stat.S_IFDIR} or info.flag_bits&1:
                    raise SkillArtifactError('Skill archive contains a link, special or encrypted entry')
                if info.is_dir():
                    if kind==stat.S_IFREG:
                        raise SkillArtifactError('Skill archive directory type is inconsistent')
                    continue
                if kind==stat.S_IFDIR:
                    raise SkillArtifactError('Skill archive file type is inconsistent')
                expanded+=info.file_size
                if expanded>MAX_EXPANDED_BYTES:
                    raise SkillArtifactError('Expanded Skill exceeds 100 MiB')
                content=bundle.read(info)
                if len(content)!=info.file_size:
                    raise SkillArtifactError('Skill archive size is inconsistent')
                files[normalized]=content
                executable[normalized]=bool((info.external_attr>>16)&0o111)
            roots=[PurePosixPath(path).parent for path in files if PurePosixPath(path).name=='SKILL.md']
            if len(roots)!=1:
                raise SkillArtifactError('Skill archive requires exactly one SKILL.md')
            root=roots[0]
            normalized_files,normalized_exec={},{}
            for name,content in files.items():
                try:
                    relative=PurePosixPath(name).relative_to(root).as_posix()
                except ValueError as error:
                    raise SkillArtifactError('Skill archive contains files outside its root') from error
                normalized_files[relative]=content
                normalized_exec[relative]=executable[name]
            for name in normalized_files:
                if any(parent.as_posix() in normalized_files for parent in PurePosixPath(name).parents
                       if parent.as_posix()!='.'):
                    raise SkillArtifactError('Skill archive file shadows a directory')
            markdown=normalized_files['SKILL.md'].decode('utf-8')
    except (zipfile.BadZipFile,UnicodeError,RuntimeError,NotImplementedError,OSError) as error:
        raise SkillArtifactError('Invalid Skill archive') from error
    lines=markdown.splitlines()
    if not lines or lines[0]!='---' or '---' not in lines[1:]:
        raise SkillArtifactError('SKILL.md requires YAML frontmatter')
    delimiter=lines.index('---',1)
    try:
        meta=yaml.safe_load('\n'.join(lines[1:delimiter]))
    except yaml.YAMLError as error:
        raise SkillArtifactError('Invalid Skill frontmatter') from error
    if (not isinstance(meta,dict) or not isinstance(meta.get('name'),str)
            or not _NAME.fullmatch(meta['name']) or not isinstance(meta.get('description'),str)
            or not meta['description'].strip()):
        raise SkillArtifactError('Skill name and description are required')
    version=meta.get('version')
    if version is not None and (isinstance(version,bool) or not isinstance(version,(str,int,float))):
        raise SkillArtifactError('Skill version must be a scalar label')
    manifest=tuple((name,hashlib.sha256(content).hexdigest(),len(content),normalized_exec[name])
                   for name,content in sorted(normalized_files.items()))
    return SkillArtifact(hashlib.sha256(raw).hexdigest(),raw,meta['name'],meta['description'].strip(),
        str(version) if version is not None else None,'\n'.join(lines[delimiter+1:]),
        MappingProxyType(normalized_files),manifest)


def discover_skill_directories(root):
    root=Path(root)
    if root.is_symlink():
        raise SkillArtifactError('Skill catalog root cannot be a symbolic link')
    return {path.parent.name:path.parent for path in sorted(root.glob('*/SKILL.md'))
        if _CATALOG_NAME.fullmatch(path.parent.name) and not path.parent.is_symlink()
        and not path.is_symlink() and path.is_file()}


def package_skill_directory(directory):
    """Package a trusted catalog directory deterministically, never following links."""
    root=Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise SkillArtifactError('Skill source must be a real directory')
    output=BytesIO()
    total=0
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob('*')):
            relative=path.relative_to(root)
            if any(part in {'evals','__pycache__','.git','node_modules'} for part in relative.parts) or path.suffix=='.pyc':
                continue
            if path.is_symlink():
                raise SkillArtifactError('Skill source cannot contain symbolic links')
            if not path.is_file():
                if not path.is_dir():
                    raise SkillArtifactError('Skill source contains a special file')
                continue
            content=path.read_bytes()
            total+=len(content)
            if total>MAX_EXPANDED_BYTES:
                raise SkillArtifactError('Expanded Skill exceeds 100 MiB')
            info=zipfile.ZipInfo(relative.as_posix())
            info.compress_type=zipfile.ZIP_DEFLATED
            info.create_system=3
            info.external_attr=(stat.S_IFREG|(0o555 if path.stat().st_mode&0o111 else 0o444))<<16
            bundle.writestr(info,content)
            if output.tell()>MAX_ARCHIVE_BYTES:
                raise SkillArtifactError('Skill archive exceeds 20 MiB')
    artifact=parse_skill_archive(output.getvalue())
    if artifact.name!=root.name:
        raise SkillArtifactError('Skill name must match its directory')
    return artifact
