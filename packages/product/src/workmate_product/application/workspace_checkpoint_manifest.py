"""Bounded file manifest validation shared by recovery admission and storage."""
import re


def validate_checkpoint_path(path):
    if (not isinstance(path,str) or len(path)>4096 or '\\' in path or any(ord(c)<32 for c in path)):
        raise ValueError('Invalid checkpoint path')
    parts=path.split('/')
    if len(parts)>65 or parts[0] not in {'input','working','output'} or any(p in {'','.','..'} for p in parts):
        raise ValueError('Invalid checkpoint path')
    return parts


def validate_manifest(manifest, *, max_files=1000, max_bytes=128*1024*1024, max_file_bytes=16*1024*1024):
    if (not isinstance(manifest,dict) or type(manifest.get('version')) is not int or manifest['version']!=1
            or not isinstance(manifest.get('files'),list) or not isinstance(manifest.get('directories'),list)):
        raise ValueError('Invalid checkpoint manifest')
    files,directories=manifest['files'],manifest['directories']
    if len(files)>max_files or len(files)+len(directories)>max_files*4+6:
        raise ValueError('Checkpoint inventory exceeds limit')
    seen=set()
    for path in directories:
        validate_checkpoint_path(path)
        if path in seen:
            raise ValueError('Duplicate checkpoint directory')
        seen.add(path)
    parents=set(seen)
    total=0
    for entry in files:
        if not isinstance(entry,dict):
            raise ValueError('Invalid checkpoint file')
        parts=validate_checkpoint_path(entry.get('path'))
        digest,size=entry.get('sha256'),entry.get('size')
        if (len(parts)<2 or entry['path'] in seen or not isinstance(digest,str)
                or re.fullmatch('[0-9a-f]{64}',digest) is None or type(size) is not int or not 0<=size<=max_file_bytes
                or type(entry.get('executable')) is not bool
                or entry.get('storage_key')!=f'.objects/sha256/{digest[:2]}/{digest}'):
            raise ValueError('Invalid checkpoint file descriptor')
        seen.add(entry['path'])
        total+=size
    for path in seen:
        parts=path.split('/')
        if any('/'.join(parts[:n]) not in parents for n in range(1,len(parts))):
            raise ValueError('Missing checkpoint parent directory')
    if type(manifest.get('total_bytes')) is not int or manifest['total_bytes']!=total or total>max_bytes:
        raise ValueError('Checkpoint byte bound exceeded')
    return files,directories
