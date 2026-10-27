"""Release manifest, architecture and release Compose contracts."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import quote

from .policy import normalize_platform

ROLES = {'gateway': 'backend', 'runtime': 'runtime', 'frontend': 'frontend'}
SERVICES = {'gateway': 'gateway', 'runtime': 'runtime', 'frontend': 'frontend',
            'storage-init': 'runtime'}
CHECKS = {'migration', 'authentication', 'permissions', 'sse', 'task', 'hitl', 'stop',
          'artifact', 'skill', 'sandbox', 'frontend'}


def run(command, *, capture=True, cwd=None, log=None):
    if log is not None:
        with log.open('w') as stream:
            result = subprocess.run(command, cwd=cwd, text=True, stdout=stream,
                                    stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError(f'Command failed ({result.returncode}); log: {log}\n'
                             + log.read_text()[-3000:])
        return ''
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=capture)
    if result.returncode:
        raise ValueError(f'Command failed ({result.returncode}): {command[0:3]}\n'
                         + (result.stderr[-3000:] if capture else 'See build output.'))
    return result.stdout.strip() if capture else ''


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temporary.replace(path)


def daemon_platform():
    return normalize_platform(run(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}']))


def inspect_image(ref, platform):
    image = json.loads(run(['docker', 'image', 'inspect', ref]))[0]
    actual = normalize_platform(image['Os'] + '/' + image['Architecture'])
    if actual != platform:
        raise ValueError(f'Image architecture mismatch: {ref}: {actual} != {platform}')
    return image


def load_manifest(path):
    manifest = json.loads(path.read_text())
    if manifest.get('schema') != 1 or not manifest.get('images'):
        raise ValueError('Invalid release manifest')
    if daemon_platform() != manifest['platform']:
        raise ValueError('Release platform differs from Docker daemon')
    for role, entry in manifest['images'].items():
        if role not in ROLES or inspect_image(entry['id'], manifest['platform'])['Id'] != entry['id']:
            raise ValueError(f'Invalid or missing release image: {role}')
    return manifest


def require_acceptance(manifest, report):
    ids = {role: entry['id'] for role, entry in manifest['images'].items()}
    if report.get('platform') != manifest['platform'] or report.get('images') != ids:
        raise ValueError('Acceptance report does not match release platform and exact image IDs')
    if any(report.get('checks', {}).get(name) != 'passed' for name in CHECKS):
        raise ValueError('Acceptance report must pass all checks: ' + ', '.join(sorted(CHECKS)))
    if not report.get('evidence'):
        raise ValueError('Acceptance report must reference test evidence')


def require_delivery(manifest):
    if manifest.get('development'):
        raise ValueError('Development builds cannot be bundled or published')
    if set(manifest['images']) != set(ROLES):
        raise ValueError('Delivery requires all three application images')
    verification = manifest.get('verification', {})
    if verification.get('images') != {k: v['id'] for k, v in manifest['images'].items()}:
        raise ValueError('Run verify for these exact images before delivery')
    require_acceptance(manifest, verification.get('acceptance', {}))


def release_compose(source, refs, *, pull_policy='never'):
    result = copy.deepcopy(source)
    for service, settings in result['services'].items():
        settings.pop('build', None)
        settings.pop('develop', None)
        if service in SERVICES and SERVICES[service] in refs:
            settings['image'] = refs[SERVICES[service]]
            settings['pull_policy'] = pull_policy
    return result


def application_sbom(manifest):
    """CycloneDX application package inventory; excludes OS/infrastructure packages."""
    components = {}
    for role, result in manifest['verification']['packaging'].items():
        ecosystem = 'npm' if role == 'frontend' else 'pypi'
        for package in result.get('smoke', {}).get('packages', []):
            name, version = package['name'], package['version']
            purl = f'pkg:{ecosystem}/{quote(name, safe="/")}@{quote(version, safe="")}'
            components.setdefault(purl, {'type': 'library', 'name': name, 'version': version,
                                         'purl': purl, 'bom-ref': purl})
            if package.get('license_expression'):
                components[purl]['licenses'] = [{'expression': package['license_expression']}]
            elif package.get('license'):
                components[purl]['licenses'] = [{'license': {'name': package['license']}}]
    return {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
            'metadata': {'component': {'type': 'application', 'name': 'Workmate',
                                       'version': manifest['version']},
                         'properties': [{'name': 'workmate:scope', 'value': 'application-packages'},
                                        {'name': 'workmate:platform', 'value': manifest['platform']}]},
            'components': [components[key] for key in sorted(components)]}
