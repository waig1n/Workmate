"""Inspect every exported Docker layer, including files deleted in later layers."""
import json
import re
import io
from pathlib import PurePosixPath
import tarfile
import zipfile



def audit_name(name, *, embedded=False):
    name = name.removeprefix('./').lstrip('/')
    path = PurePosixPath(name)
    if '..' in path.parts:
        raise ValueError(f'Unsafe archive path: {name}')
    if name.startswith('app/') and '/.next/cache/' in name:
        raise ValueError(f'Frontend build cache: {name}')
    if '.git' in path.parts or path.name in {'.netrc', '.pypirc', 'id_rsa', 'id_ed25519'} or ('.aws' in path.parts and path.name == 'credentials'):
        raise ValueError(f'Private credential or repository file in image: {name}')
    if path.name == '.env' or path.name.startswith('.env.'):
        raise ValueError(f'Environment file in image: {name}')


def audit_embedded(name, stream, size, depth=0):
    if not name.endswith(('.whl', '.zip', '.tar', '.tar.gz', '.tgz')):
        # Screen known private-key markers without logging secret contents.
        if re.search(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',
                     stream.read(1024 * 1024)):
            raise ValueError(f'Private key material in image: {name}')
        return
    if depth > 3 or size > 128 * 1024 * 1024:
        raise ValueError(f'Embedded archive exceeds audit bounds: {name}')
    data = io.BytesIO(stream.read(128 * 1024 * 1024 + 1))
    if name.endswith(('.whl', '.zip')):
        with zipfile.ZipFile(data) as archive:
            if sum(p.file_size for p in archive.infolist()) > 256 * 1024 * 1024:
                raise ValueError(f'Expanded archive exceeds audit bounds: {name}')
            for item in archive.infolist():
                audit_name(item.filename, embedded=True)
                with archive.open(item) as child:
                    audit_embedded(item.filename, child, item.file_size, depth + 1)
    else:
        with tarfile.open(fileobj=data) as archive:
            for item in archive:
                audit_name(item.name, embedded=True)
                if item.isfile():
                    audit_embedded(item.name, archive.extractfile(item), item.size, depth + 1)


def audit_archive(path):
    count = 0
    with tarfile.open(path) as saved:
        stream = saved.extractfile('manifest.json')
        if stream is None:
            raise ValueError('Missing Docker save manifest')
        manifests = json.load(stream)
        layers = {layer for manifest in manifests for layer in manifest['Layers']}
        if not layers:
            raise ValueError('No image layers to audit')
        for name in sorted(layers):
            stream = saved.extractfile(name)
            if stream is None:
                raise ValueError(f'Missing image layer: {name}')
            with tarfile.open(fileobj=stream, mode='r|*') as layer:
                for member in layer:
                    if member.isfile() or member.issym() or member.islnk():
                        audit_name(member.name)
                        if member.isfile():
                            audit_embedded(member.name, layer.extractfile(member), member.size)
                        count += 1
    return {'layers': len(layers), 'entries': count}
