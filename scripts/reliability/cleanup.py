"""Best-effort cleanup of exact resources owned by reliability drills."""
import json
import re

LABEL = 'workmate.reliability.backup'
IDENTITY = re.compile(r'workmate-restore-[a-f0-9]{32}')


def attempt(errors, operation, action):
    try:
        return action()
    except Exception as error:
        errors.append({'operation': operation, 'error': f'{type(error).__name__}: {error}'})
        return None


def restore_targets(bundle, docker):
    """Read partial ledgers, validating exact ownership before deleting anything."""
    volumes = []
    for ledger in bundle.glob('workmate-restore-*.json'):
        identity = ledger.stem
        if not IDENTITY.fullmatch(identity) or ledger.is_symlink():
            raise ValueError('Invalid restore ledger')
        for volume in json.loads(ledger.read_text()).values():
            if not re.fullmatch(re.escape(identity) + r'-[0-9]+', volume):
                raise ValueError('Invalid restore target')
            info = json.loads(docker('volume', 'inspect', volume))[0]
            if info['Name'] != volume or (info.get('Labels') or {}).get(LABEL) != identity:
                raise ValueError('Restore target ownership mismatch')
            volumes.append(volume)
    return sorted(set(volumes))


def remaining_resources(docker, projects, restore_ids=()):
    result = {'containers': [], 'volumes': [], 'networks': []}
    for project in projects:
        label = 'label=com.docker.compose.project=' + project
        for kind, args in [('containers', ('ps', '-aq')), ('volumes', ('volume', 'ls', '-q')),
                           ('networks', ('network', 'ls', '-q'))]:
            result[kind].extend(docker(*args, '--filter', label).split())
    for identity in restore_ids:
        if not IDENTITY.fullmatch(identity):
            raise ValueError('Invalid restore identity')
        result['volumes'].extend(docker('volume', 'ls', '-q', '--filter',
                                       'label=' + LABEL + '=' + identity).split())
    return {key: sorted(set(values)) for key, values in result.items()}


def source_hashes(root):
    """Identify repository code used by source-based drills, excluding credentials."""
    import hashlib
    prefixes = ('scripts/reliability', 'backend/app', 'backend/tests',
                'runtime/workmate_runtime', 'runtime/tests', 'packages/product/src',
                'packages/contracts/src')
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for prefix in prefixes for path in (root/prefix).rglob('*.py')
            if '__pycache__' not in path.parts}
