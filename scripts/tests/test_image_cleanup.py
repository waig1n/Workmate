"""Cleanup must be opt-in and cannot cross image ownership boundaries."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync_images_to_remote as sync


def test_cleanup_dispatches_without_entering_sync(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, 'image_cleanup', SimpleNamespace(
        main=lambda argv, runner_factory: calls.append((argv, runner_factory)) or 0))
    monkeypatch.setattr(sync, 'require_commands', lambda *_: pytest.fail('sync must not run'))
    assert sync.main(['cleanup', '--target', 'local', '--compose-dir', '/deployment']) == 0
    assert calls == [(['--target', 'local', '--compose-dir', '/deployment'], sync.CommandRunner)]

import copy
import json
from datetime import datetime, timedelta, timezone
import image_cleanup as cleanup

NOW = datetime.now(timezone.utc)

def image(name, tags, *, days=30, digests=()):
    return {'Id': 'sha256:' + name, 'RepoTags': list(tags), 'RepoDigests': list(digests),
            'Created': (NOW - timedelta(days=days)).isoformat()}

class Docker:
    def __init__(self, images, *, compose=('workmate-gateway:deployed',), containers=()):
        self.images = copy.deepcopy(images)
        self.compose = list(compose)
        self.containers = list(containers)
        self.calls = []
        self.scans = 0
        self.on_scan = None
        self.remote_calls = 0
        self.fail = None

    def remote_command(self, command, **kwargs):
        self.remote_calls += 1
        return self.local(command, **kwargs)

    def local(self, command, **kwargs):
        self.calls.append(list(command))
        if self.fail and self.fail(command):
            return SimpleNamespace(returncode=1, stdout='', stderr='secret-environment-value')
        if command[:4] == ['docker', 'image', 'ls', '-aq']:
            self.scans += 1
            if self.on_scan: self.on_scan(self)
            value = '\n'.join(row['Id'] for row in self.images)
        elif command[:3] == ['docker', 'image', 'inspect']:
            refs = command[3:command.index('--format')] if '--format' in command else command[3:]
            refs = [ref.removeprefix('docker.io/library/') for ref in refs]
            rows = [r for r in self.images if r['Id'] in refs or set(refs) & set(r['RepoTags'])]
            if not rows:
                return SimpleNamespace(returncode=1, stdout='', stderr='Error: No such image: '+command[3])
            value = rows[0]['Id'] if '--format' in command else json.dumps(rows)
        elif command == ['docker', 'ps', '-aq']:
            value = '\n'.join(str(i) for i in range(len(self.containers)))
        elif command[:3] == ['docker', 'container', 'inspect']:
            value = '\n'.join(self.containers[int(i)] for i in command[3:command.index('--format')])
        elif command[:2] == ['docker', 'compose']:
            value = '\n'.join(self.compose)
        elif command[:3] == ['docker', 'image', 'rm']:
            assert len(command) == 4  # Never use force or remove by ID.
            row = next(r for r in self.images if command[3] in r['RepoTags'])
            row['RepoTags'].remove(command[3])
            if not row['RepoTags']: self.images.remove(row)
            value = ''
        else:
            raise AssertionError(command)
        return SimpleNamespace(returncode=0, stdout=value, stderr='')

    @property
    def removed(self):
        return [c[3] for c in self.calls if c[:3] == ['docker', 'image', 'rm']]


def invoke(docker, *extra):
    return cleanup.main(['--target', 'local', '--compose-dir', '/deploy', *extra], lambda *_: docker)


def test_dry_run_protects_containers_compose_keep_and_external_aliases(capsys):
    docker = Docker([
        image('old', ['workmate-runtime:old']),
        image('recent', ['workmate-runtime:recent'], days=1),
        image('running', ['workmate-runtime:active']),
        image('stopped', ['workmate-runtime:stopped']),
        image('configured', ['workmate-gateway:deployed', 'workmate-gateway:other']),
        image('rollback', ['workmate-frontend:rollback']),
        image('external', ['workmate-runtime:shared', 'other-project:latest']),
        image('untagged', []), image('unrelated', ['postgres:18']),
    ], containers=['sha256:running', 'sha256:stopped'])
    assert invoke(docker, '--keep', 'workmate-frontend:rollback') == 0
    output = capsys.readouterr().out
    assert [line for line in output.splitlines() if line.startswith('CANDIDATE')] == [
        'CANDIDATE workmate-runtime:old [sha256:old] old and unreferenced']
    assert 'postgres:18' not in output and 'sha256:untagged' not in output
    assert not docker.removed


def test_apply_removes_owned_registry_alias_and_canonical_tag():
    tags = ['localhost:5000/workmate-runtime:old', 'workmate-runtime:old']
    docker = Docker([image('old', tags, digests=['localhost:5000/workmate-runtime@sha256:abc'])])
    assert invoke(docker, '--apply') == 0
    assert set(docker.removed) == set(tags)


def test_external_digest_protects_whole_image():
    docker = Docker([image('old', ['workmate-runtime:old'], digests=['external.example/private@sha256:abc'])])
    assert invoke(docker, '--apply') == 0
    assert not docker.removed


@pytest.mark.parametrize('change', ['container', 'compose', 'retag', 'external_alias'])
def test_apply_rechecks_each_candidate(change):
    docker = Docker([image('old', ['workmate-runtime:old'])])
    def mutate(d):
        if d.scans != 2: return
        if change == 'container': d.containers.append('sha256:old')
        elif change == 'compose': d.compose.append('workmate-runtime:old')
        elif change == 'retag': d.images[0]['Id'] = 'sha256:replacement'
        else: d.images[0]['RepoTags'].append('other:protected')
    docker.on_scan = mutate
    assert invoke(docker, '--apply') == 0
    assert not docker.removed


def test_missing_keep_and_failed_compose_stop_before_mutation(capsys):
    for fail_compose in (False, True):
        docker = Docker([image('old', ['workmate-runtime:old'])])
        if fail_compose: docker.fail = lambda c: c[:2] == ['docker', 'compose']
        extra = [] if fail_compose else ['--keep', 'workmate-runtime:typo']
        assert invoke(docker, '--apply', *extra) == 1
        assert not docker.removed
    assert 'secret-environment-value' not in capsys.readouterr().err


def test_inspection_failure_and_remove_failure_are_not_ignored(capsys):
    for prefix in (['docker', 'image', 'inspect'], ['docker', 'image', 'rm']):
        docker = Docker([image('old', ['workmate-runtime:old'])])
        docker.fail = lambda c: c[:3] == prefix
        assert invoke(docker, '--apply') == 1
        assert not docker.removed or docker.images
    assert 'secret-environment-value' not in capsys.readouterr().err


def test_remote_uses_remote_runner_only():
    docker = Docker([image('old', ['workmate-runtime:old'])])
    assert cleanup.main(['--target', 'remote', '--remote', 'ubuntu@test', '--compose-dir', '/path with space'],
                        lambda host, timeout: docker) == 0
    assert docker.remote_calls == len(docker.calls)
    assert ['docker', 'compose', '--project-directory', '/path with space', 'config', '--images'] in docker.calls
    assert not docker.removed


def test_compose_digest_and_default_latest_protect_identity():
    docker = Docker([image('one', ['workmate-runtime:latest']),
                     image('two', ['workmate-gateway:old'], digests=['workmate-gateway@sha256:abc'])],
                    compose=['workmate-runtime', 'workmate-gateway@sha256:abc'])
    assert invoke(docker, '--apply') == 0
    assert not docker.removed


def test_retention_boundary_and_loopback_scope():
    row = cleanup.Image('id', ('127.0.0.1:5001/workmate-runtime:old',), NOW-timedelta(days=7))
    state = cleanup.State((row,), frozenset(), frozenset())
    assert not cleanup.build_plan(state, 7, NOW)[0].candidate
    assert cleanup.build_plan(state, 6, NOW)[0].candidate
    assert not cleanup.OWNED_TAG.fullmatch('workmate-runtime-other:old')
    assert not cleanup.OWNED_TAG.fullmatch('registry.example/workmate-runtime:old')


@pytest.mark.parametrize('argv', [[], ['--target', 'both'], ['--target', 'local', '--compose-dir', ''],
    ['--target', 'local', '--compose-dir', '/x', '--older-than-days', '-1'],
    ['--target', 'local', '--compose-dir', '/x', '--keep', 'bad;command']])
def test_invalid_arguments_fail_before_docker(argv):
    with pytest.raises(SystemExit):
        cleanup.main(argv, lambda *_: pytest.fail('must not run Docker'))

@pytest.mark.parametrize('protection', ['compose', 'keep'])
def test_docker_normalized_references_protect_identity(protection):
    docker = Docker([image('old', ['workmate-runtime:old'])])
    ref = 'docker.io/library/workmate-runtime:old'
    extra = ['--keep', ref] if protection == 'keep' else []
    if protection == 'compose': docker.compose = [ref]
    assert invoke(docker, '--apply', *extra) == 0
    assert not docker.removed


def test_inventory_is_batched_for_remote_hosts():
    rows = [image(str(i), [f'workmate-runtime:build-{i}']) for i in range(205)]
    docker = Docker(rows, containers=[row['Id'] for row in rows])
    assert invoke(docker) == 0
    inspections = [c for c in docker.calls if c[:3] == ['docker', 'image', 'inspect'] and '--format' not in c]
    assert len(inspections) == 3
    assert all(len(c[3:]) <= 100 for c in inspections)
    container_inspections = [c for c in docker.calls if c[:3] == ['docker', 'container', 'inspect']]
    assert len(container_inspections) == 3
    assert not docker.removed


def test_exact_image_ignores_age_and_does_not_remove_aliases():
    docker = Docker([image('one', ['workmate-runtime:new', 'localhost:5000/workmate-runtime:new', 'workmate-runtime:other'], days=0)])
    assert invoke(docker, '--image', 'workmate-runtime:new', '--apply') == 0
    assert docker.removed == ['workmate-runtime:new']


def test_tag_matches_exact_suffix_across_roles_and_selectors_form_union():
    docker = Docker([image('a', ['workmate-runtime:v1', 'workmate-runtime:v10'], days=0),
                     image('b', ['workmate-gateway:v1'], days=0),
                     image('c', ['workmate-frontend:extra'], days=0)])
    assert invoke(docker, '--tag', 'v1', '--image', 'workmate-frontend:extra', '--apply') == 0
    assert set(docker.removed) == {'workmate-runtime:v1', 'workmate-gateway:v1', 'workmate-frontend:extra'}


@pytest.mark.parametrize('protection', ['container', 'compose', 'keep', 'external'])
def test_selectors_never_override_protection(protection):
    docker = Docker([image('a', ['workmate-runtime:v1'], days=0)])
    extra = []
    if protection == 'container': docker.containers = ['sha256:a']
    elif protection == 'compose': docker.compose = ['workmate-runtime:v1']
    elif protection == 'keep': extra = ['--keep', 'workmate-runtime:v1']
    else: docker.images[0]['RepoTags'].append('other:shared')
    assert invoke(docker, '--tag', 'v1', '--apply', *extra) == 0
    assert not docker.removed


def test_selected_dry_run_and_missing_tag_never_expand_scope(capsys):
    docker = Docker([image('a', ['workmate-runtime:v1', 'workmate-runtime:v2'])])
    assert invoke(docker, '--image', 'workmate-runtime:v1') == 0
    output = capsys.readouterr().out
    assert 'CANDIDATE workmate-runtime:v1 [' in output and 'workmate-runtime:v2' not in output
    assert invoke(docker, '--tag', 'absent', '--apply') == 0
    assert not docker.removed


def test_missing_exact_image_fails_before_any_deletion():
    docker = Docker([image('a', ['workmate-runtime:v1'])])
    assert invoke(docker, '--image', 'workmate-runtime:v1', '--image', 'workmate-runtime:missing', '--apply') == 1
    assert not docker.removed


@pytest.mark.parametrize('extra', [['--image','postgres:18'], ['--image','workmate-runtime:*'],
    ['--tag','v*'], ['--tag',''], ['--tag','v1','--older-than-days','0']])
def test_invalid_or_ambiguous_selectors_are_rejected(extra):
    with pytest.raises(SystemExit):
        cleanup.main(['--target','local','--compose-dir','/deploy',*extra],
                     lambda *_: pytest.fail('must not access Docker'))
