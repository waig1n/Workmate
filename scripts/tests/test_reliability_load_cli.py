import pytest
from scripts.reliability.load import parse_args, plan


def test_default_dry_run_needs_no_docker():
    args = parse_args(['--runtime-image', 'sha256:' + 'a' * 64, '--gateway-image', 'sha256:' + 'b' * 64])
    value = plan(args)
    assert not args.run
    assert value['concurrency'] == 1
    assert value['tasks'] == 1
    assert value['scope'] == 'current source, isolated independent processes, deterministic text model'


@pytest.mark.parametrize('extra', [['--concurrency', '0'], ['--repeat', '11'], ['--tasks', '0']])
def test_rejects_unbounded_or_empty_options(extra):
    with pytest.raises(SystemExit):
        parse_args(['--runtime-image', 'r', '--gateway-image', 'g', *extra])


def test_main_dry_run_never_calls_subprocess(monkeypatch, capsys):
    from scripts.reliability import load
    monkeypatch.setattr(load.subprocess, 'run', lambda *a, **k: pytest.fail('dry-run accessed Docker'))
    load.main(['--runtime-image', 'r', '--gateway-image', 'g'])
    assert '"run": false' in capsys.readouterr().out


@pytest.mark.parametrize('fail_workload', [False, True])
def test_launcher_isolated_source_and_cleanup(tmp_path, monkeypatch, fail_workload):
    import json
    import subprocess
    from types import SimpleNamespace
    from scripts.reliability import load
    source = tmp_path / 'repo'
    paths = ('runtime/workmate_runtime', 'packages/product/src', 'packages/contracts/src',
             'backend/app', 'backend/alembic', 'scripts/reliability/load_support', 'config')
    for path in paths:
        directory = source / path
        directory.mkdir(parents=True)
        (directory / 'fixture.py').write_text('# fixture\n')
    (source / 'backend/alembic.ini').write_text('[alembic]\n')
    (source / '.env').write_text('DO_NOT_COPY=secret\n')
    monkeypatch.setattr(load, 'ROOT', source)
    monkeypatch.setattr(load, 'local_daemon', lambda: None)
    monkeypatch.setattr(load.subprocess, 'check_output', lambda *a, **k: 'sha256:' + 'a' * 64)
    calls = []
    def run(parts, **kwargs):
        calls.append(parts)
        if fail_workload and '/support/run.py' in parts:
            raise subprocess.CalledProcessError(1, parts)
        if parts[:3] == ['docker', 'container', 'inspect']:
            owner = parts[-1].rsplit('-', 1)[0]
            return SimpleNamespace(returncode=0, stdout=json.dumps([{'Config': {'Labels': {'workmate.validation.owner': owner}}, 'Mounts': []}]), stderr='')
        if parts[:3] == ['docker', 'network', 'inspect']:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'workmate.validation.owner': parts[3]}), stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(load.subprocess, 'run', run)
    args = load.parse_args(['--runtime-image', 'r', '--gateway-image', 'g', '--output', str(tmp_path / 'output'), '--run'])
    if fail_workload:
        with pytest.raises(subprocess.CalledProcessError):
            load.execute(args)
    else:
        load.execute(args)
    runs = list((tmp_path / 'output').iterdir())
    assert len(runs) == 1
    assert not (runs[0] / 'source/.env').exists()
    assert json.loads((runs[0] / 'evidence/cleanup.json').read_text())['failures'] == []
    assert any('--internal' in call for call in calls)
    assert any(call[:3] == ['docker', 'network', 'rm'] for call in calls)
    assert not any('pull' in call or 'build' in call for call in calls)
    assert all(call[4].startswith('reliability-load-') for call in calls if call[:4] == ['docker', 'rm', '-f', '-v'])


def test_cleanup_verifies_residuals_and_never_removes_foreign_container(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from scripts.reliability import load
    owner = 'reliability-load-owned'
    calls = []
    def run(parts, **kwargs):
        calls.append(parts)
        if parts[:3] == ['docker', 'container', 'inspect']:
            label = owner if parts[-1] == owner + '-pg' else 'someone-else'
            return SimpleNamespace(returncode=0, stdout=json.dumps([{'Config': {'Labels': {'workmate.validation.owner': label}}, 'Mounts': [{'Type': 'volume', 'Name': 'anonymous-owned'}]}]), stderr='')
        if parts[:3] == ['docker', 'volume', 'inspect']:
            return SimpleNamespace(returncode=0, stdout='[]', stderr='')
        if parts[:3] == ['docker', 'container', 'ls']:
            return SimpleNamespace(returncode=0, stdout='residual-container\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(load.subprocess, 'run', run)
    result = load.cleanup_owned(owner, [owner + '-pg', 'foreign'], True, tmp_path)
    assert result['remaining_volumes'] == ['anonymous-owned']
    assert result['remaining_containers'] == ['residual-container']
    assert result['failures']
    assert not any(parts[:4] == ['docker', 'rm', '-f', '-v'] and parts[-1] == 'foreign' for parts in calls)


def test_rejects_remote_daemon_before_resolving_images(monkeypatch):
    from scripts.reliability import load
    def remote():
        raise ValueError('not local')
    monkeypatch.setattr(load, 'local_daemon', remote)
    monkeypatch.setattr(load.subprocess, 'check_output', lambda *a, **k: pytest.fail('resolved image before locality guard'))
    with pytest.raises(ValueError, match='not local'):
        load.execute(load.parse_args(['--runtime-image', 'r', '--gateway-image', 'g', '--run']))


@pytest.mark.parametrize('missing', ['no such volume', 'No such volume'])
def test_cleanup_accepts_confirmed_absent_volume_in_either_case(tmp_path, monkeypatch, missing):
    import json
    from types import SimpleNamespace
    from scripts.reliability import load
    owner = 'reliability-load-absent'
    def run(parts, **kwargs):
        if parts[:3] == ['docker', 'container', 'inspect']:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{'Config': {'Labels': {'workmate.validation.owner': owner}}, 'Mounts': [{'Type': 'volume', 'Name': 'removed-volume'}]}]), stderr='')
        if parts[:3] == ['docker', 'volume', 'inspect']:
            return SimpleNamespace(returncode=1, stdout='[]', stderr='Error response from daemon: ' + missing + ': removed-volume')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(load.subprocess, 'run', run)
    result = load.cleanup_owned(owner, [owner + '-pg'], False, tmp_path)
    assert result['failures'] == []
    assert result['remaining_volumes'] == []


@pytest.mark.parametrize('message,accepted', [
    ('Error: no such container: missing', True),
    ('Error: No such object: missing', True),
    ('permission denied while connecting to daemon', False),
])
def test_cleanup_missing_container_is_not_permission_failure(tmp_path, monkeypatch, message, accepted):
    from types import SimpleNamespace
    from scripts.reliability import load
    def run(parts, **kwargs):
        if parts[:3] == ['docker', 'container', 'inspect']:
            return SimpleNamespace(returncode=1, stdout='[]', stderr=message)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(load.subprocess, 'run', run)
    result = load.cleanup_owned('reliability-load-absent', ['missing'], False, tmp_path)
    assert bool(result['failures']) is not accepted
