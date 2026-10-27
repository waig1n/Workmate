"""Deterministic file bytes and independent tool-attempt/download checks."""
import hashlib
import json
import re


def file_content(key, size):
    if not re.fullmatch('[0-9a-f]{32}',key) or type(size) is not int or not 1<=size<=1048576:
        raise ValueError('Invalid bounded file fixture')
    pattern=key+'\n'
    return (pattern*((size//len(pattern))+1))[:size]


def verify_file(key, downloaded, size, evidence):
    expected=file_content(key,size).encode()
    assert downloaded==expected,'Artifact download differs from intended file'
    attempts=[]
    for trace in evidence.glob('write-effects-*.jsonl'):
        attempts.extend(row for row in (json.loads(line) for line in trace.read_text().splitlines())
            if row['file_path'].endswith('/'+key+'.txt'))
    assert len(attempts)==1,('Write attempt count',len(attempts))
    return {'write_attempt_count':len(attempts),'download_bytes':len(downloaded),
            'download_sha256':hashlib.sha256(downloaded).hexdigest()}
