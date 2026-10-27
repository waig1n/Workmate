"""Cold named-volume backup. Never stops services or restores over existing volumes.

All writers must remain stopped throughout. Docker checks are an extra guard,
not a distributed fence. Local Docker only; external volumes/writable binds are
rejected. Configuration and encryption keys must be backed up separately.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

LABEL = 'workmate.reliability.backup'


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def validate_inventory(value):
    if not value.get('project') or not value.get('volumes'):
        raise ValueError('A named Compose project and persistent volumes are required')
    if value.get('external_volumes') or value.get('binds'):
        raise ValueError('External volumes or writable binds need a separate backup procedure')
    if len(set(value['volumes'])) != len(value['volumes']):
        raise ValueError('Duplicate volume mapping')


def verify_bundle(root):
    root = Path(root)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') != 1:
        raise ValueError('Unsupported backup format')
    validate_inventory(manifest['inventory'])
    files = manifest.get('files', {})
    if sorted(item['volume'] for item in files.values()) != sorted(manifest['inventory']['volumes']):
        raise ValueError('Incomplete volume set')
    for name, item in files.items():
        if not re.fullmatch(r'volume-[0-9]+\.tar', name):
            raise ValueError('Unsafe archive name')
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Archive missing or symlink')
        if path.stat().st_size != item['size'] or digest(path) != item['sha256']:
            raise ValueError('Archive checksum/size mismatch')
    return manifest


def inventory(compose_files, project):
    command = ['compose', '-p', project]
    for path in compose_files:
        command += ['-f', str(Path(path).resolve())]
    config = json.loads(docker(*command, 'config', '--format', 'json'))
    volumes = config.get('volumes', {})
    result = {'project': project, 'volumes': [v['name'] for v in volumes.values()],
              'external_volumes': [k for k, v in volumes.items() if v.get('external')],
              'binds': [], 'services': {}}
    for name, service in config['services'].items():
        result['services'][name] = {'image': service.get('image')}
        for mount in service.get('volumes', []):
            if mount['type'] == 'bind' and not mount.get('read_only'):
                result['binds'].append(mount['source'])
    validate_inventory(result)
    return result


def assert_quiet(value):
    """Inspect ALL containers, including other projects mounting these volumes."""
    ids = docker('ps', '-aq').split()
    containers = json.loads(docker('inspect', *ids)) if ids else []
    for container in containers:
        uses_volume = any(m.get('Name') in value['volumes'] for m in container.get('Mounts', []))
        same_project = (container['Config'].get('Labels') or {}).get('com.docker.compose.project') == value['project']
        if (uses_volume or same_project) and (container['State']['Running'] or container['State'].get('Restarting')):
            raise ValueError('Writer/container still running: ' + container['Name'])
        if same_project:
            for mount in container.get('Mounts', []):
                if mount.get('RW') and ((mount['Type'] == 'volume' and mount.get('Name') not in value['volumes']) or mount['Type'] == 'bind'):
                    raise ValueError('Unlisted writable mount on deployed container')
            service = container['Config']['Labels'].get('com.docker.compose.service')
            entry = value['services'].setdefault(service, {})
            entry['deployed_image_ids'] = sorted(set(entry.get('deployed_image_ids', [])) | {container['Image']})
    for name in value['volumes']:
        info = json.loads(docker('volume', 'inspect', name))[0]
        if info.get('Driver') != 'local' or info.get('Options'):
            raise ValueError('Only plain local Docker volumes are supported')
        if (info.get('Labels') or {}).get('com.docker.compose.project') != value['project']:
            raise ValueError('Volume not owned by selected Compose project: ' + name)


def local_daemon():
    selected = os.environ.get('DOCKER_CONTEXT')
    host = os.environ.get('DOCKER_HOST')
    if selected or not host:
        arguments = ('context', 'inspect', selected) if selected else ('context', 'inspect')
        context = json.loads(docker(*arguments))[0]
        host = context['Endpoints']['docker']['Host']
    if not host.startswith('unix://'):
        raise ValueError('This tool supports only a local Unix Docker socket')


def archive_volume(image, volume, root, filename, *, restore=False):
    # The helper has no network, no Docker socket and only this volume + bundle.
    command = ['run', '--rm', '--network', 'none', '--label', LABEL + '=helper',
               '--mount', f'type=volume,src={volume},dst=/volume' + ('' if restore else ',readonly'),
               '--mount', f'type=bind,src={root},dst=/backup' + (',readonly' if restore else ''), image]
    if restore:
        command += ['sh', '-ec', 'test -z "$(ls -A /volume)"; tar xpf "$1" -C /volume',
                    'restore', '/backup/' + filename]
    else:
        command += ['sh', '-ec', 'umask 077; tar cpf "$1" -C /volume .; chown "$2:$3" "$1"',
                    'backup', '/backup/' + filename, str(os.getuid()), str(os.getgid())]
    docker(*command)


def backup(value, root, image):
    assert_quiet(value)
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'inventory': value, 'helper_image': image, 'files': {},
              'limitations': ['configuration/secrets are separate', 'same database major version',
                              'new directory Workspace resume is not authorized']}
    for index, volume in enumerate(value['volumes']):
        assert_quiet(value)
        name = f'volume-{index}.tar'
        archive_volume(image, volume, root, name)
        path = root / name
        path.chmod(0o600)
        report['files'][name] = {'volume': volume, 'size': path.stat().st_size, 'sha256': digest(path)}
    assert_quiet(value)
    (root / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    verify_bundle(root)
    return report


def restore(root, image):
    # Check every byte before creating any target; targets are always new names.
    root = Path(root).resolve()
    report = verify_bundle(root)
    identity = 'workmate-restore-' + uuid.uuid4().hex
    mapping = {}
    for index, (name, entry) in enumerate(report['files'].items()):
        volume = f'{identity}-{index}'
        docker('volume', 'create', '--label', LABEL + '=' + identity, volume)
        mapping[entry['volume']] = volume
        # Record partial targets on failure; never silently delete recovery data.
        (root / f'{identity}.json').write_text(json.dumps(mapping, indent=2) + '\n')
        archive_volume(image, volume, root, name, restore=True)
    return {'restore_id': identity, 'volumes': mapping, 'services_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'verify', 'restore'])
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--compose-file', action='append', default=[])
    parser.add_argument('--project')
    parser.add_argument('--helper-image', default='alpine:latest')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--writers-stopped', action='store_true')
    args = parser.parse_args()
    if args.action == 'verify':
        verify_bundle(args.bundle)
        print(json.dumps({'verified': True}))
        return
    local_daemon()
    image = docker('image', 'inspect', args.helper_image, '--format', '{{.Id}}')
    if args.action == 'backup':
        if not args.compose_file or not args.project:
            parser.error('backup requires explicit --project and --compose-file')
        value = inventory(args.compose_file, args.project)
        assert_quiet(value)
        if args.apply and not args.writers_stopped:
            parser.error('--apply requires --writers-stopped throughout the maintenance window')
        report = backup(value, args.bundle, image) if args.apply else {'plan': value, 'applied': False}
    else:
        verify_bundle(args.bundle)
        report = restore(args.bundle, image) if args.apply else {'verified': True, 'applied': False}
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
