"""Verify an application-only bundle before loading any Docker image."""
import json
import hashlib
from pathlib import Path
import tarfile

from .core import daemon_platform, digest, inspect_image, require_delivery, run
from .policy import normalize_platform


def validate_bundle(directory):
    sums = directory / 'SHA256SUMS'
    if sums.exists():
        for line in sums.read_text().splitlines():
            expected, name = line.split('  ', 1)
            path = directory / name
            if (path.is_symlink() or not path.resolve().is_relative_to(directory.resolve())
                    or digest(path) != expected):
                raise ValueError(f'Bundle file checksum/path mismatch: {name}')
    manifest = json.loads((directory / 'release.json').read_text())
    require_delivery(manifest)
    if daemon_platform() != manifest['platform']:
        raise ValueError('Bundle architecture does not match target Docker daemon')
    name = manifest['archive']['file']
    if Path(name).name != name:
        raise ValueError('Invalid bundle archive path')
    archive = directory / name
    if digest(archive) != manifest['archive']['sha256']:
        raise ValueError('Image archive checksum mismatch')
    expected = {entry['id'] for entry in manifest['images'].values()}
    observed = set()
    with tarfile.open(archive) as saved:
        manifests = json.load(saved.extractfile('manifest.json'))
        for entry in manifests:
            data = saved.extractfile(entry['Config']).read()
            image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
            config = json.loads(data)
            platform = normalize_platform(config['os'] + '/' + config['architecture'])
            if platform != manifest['platform']:
                raise ValueError('Archive contains a different architecture')
            role = next((k for k, v in manifest['images'].items() if v['id'] == image_id), None)
            if role is None or entry.get('RepoTags') != [f'workmate-{role}:{manifest["version"]}']:
                raise ValueError('Archive image identity or tag does not match manifest')
            observed.add(image_id)
    if observed != expected or len(manifests) != len(expected):
        raise ValueError('Archive is missing images or contains unexpected images')
    return manifest, archive


def load(directory):
    manifest, archive = validate_bundle(directory)
    run(['docker', 'load', '-i', str(archive)], capture=False)
    for role, entry in manifest['images'].items():
        ref = f'workmate-{role}:{manifest["version"]}'
        if inspect_image(ref, manifest['platform'])['Id'] != entry['id']:
            raise ValueError(f'Loaded image differs from manifest: {ref}')
    print('Images imported and verified. No application services were started.')
