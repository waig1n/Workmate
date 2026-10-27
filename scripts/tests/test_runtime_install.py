import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_install as cli
import runtime_install_payload as payload


def container(identifier='a' * 64):
    return {'Id': identifier, 'Name': '/workmate-runtime-1',
            'State': {'Running': True, 'StartedAt': 'original-start'},
            'Config': {'Image': 'workmate-runtime:2.1.1', 'Labels': {
                'com.docker.compose.project': 'workmate',
                'com.docker.compose.service': 'runtime'}}}


class Docker:
    def __init__(self, *, changed=False, failure=False):
        self.commands = []
        self.changed = changed
        self.failure = failure
        self.inspections = 0

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[:2] == ['docker', 'ps']:
            return subprocess.CompletedProcess(command, 0, 'a' * 64 + '\n', '')
        if command[:2] == ['docker', 'inspect']:
            self.inspections += 1
            row = container()
            if self.changed and self.inspections > 1:
                row['State']['StartedAt'] = 'restarted'
            return subprocess.CompletedProcess(command, 0, json.dumps([row]), '')
        return subprocess.CompletedProcess(command, int(self.failure), '', '')


def args(*extra):
    return cli.parse_args(['--target', 'local', '--apt', 'poppler-utils', *extra])


def test_preview_never_executes_installer(capsys):
    run = Docker()
    assert cli.execute(args(), run) == 0
    assert not any(command[0][1] == 'exec' for command in run.commands)
    assert 'poppler-utils' in capsys.readouterr().out


def test_apply_uses_root_stdin_and_all_checks():
    run = Docker()
    assert cli.execute(args('--pip', 'pymupdf==1.26.5', '--check-command', 'pdftoppm',
                            '--check-import', 'fitz', '--apply'), run) == 0
    command, options = next(item for item in run.commands if item[0][1] == 'exec')
    assert command[2:5] == ['-i', '--user', '0']
    assert command[-3:] == ['python', '-u', '-']
    assert 'pdftoppm' in options['input'] and 'fitz' in options['input']
    assert not any('restart' in command or 'stop' in command for command, _ in run.commands)


def test_changed_start_aborts_before_install():
    run = Docker(changed=True)
    with pytest.raises(ValueError, match='changed'):
        cli.execute(args('--apply'), run)
    assert not any(command[0][1] == 'exec' for command in run.commands)


def test_install_failure_stops_processing():
    run = Docker(failure=True)
    with pytest.raises(ValueError, match='failed'):
        cli.execute(args('--apply'), run)


@pytest.mark.parametrize('extra', [['--apt', '-bad'], ['--pip', 'x;touch /tmp/x'],
    ['--pip', 'git+https://example.org/x'], ['--pip-index-url', 'https://user:secret@example.org'],
    ['--check-import', 'os;exit()'], ['--check-command', '/bin/sh'],
    ['--project', 'bad name'], ['--remote', '-option']])
def test_rejects_unsafe_inputs(extra):
    with pytest.raises(SystemExit):
        cli.parse_args(['--target', 'local', '--apt', 'poppler-utils', *extra])


@pytest.mark.parametrize('field,value', [('Image', 'postgres:18'), ('service', 'gateway'),
                                        ('project', 'other'), ('Running', False)])
def test_rejects_wrong_containers(field, value):
    row = container()
    if field in {'service', 'project'}:
        row['Config']['Labels']['com.docker.compose.' + field] = value
    elif field == 'Running':
        row['State'][field] = value
    else:
        row['Config'][field] = value
    with pytest.raises(ValueError):
        cli.validate_container(row, 'workmate')


def test_remote_quotes_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, 'run', lambda cmd, **kw: calls.append((cmd, kw)))
    cli.runner(cli.parse_args(['--target', 'remote', '--remote', 'operator@example.test', '--apt', 'poppler-utils']))(
        ['docker', 'exec', 'abc', 'python', '-c', "print('ok')"], input='data')
    assert calls[0][0][:3] == ['ssh', '-o', 'BatchMode=yes']
    assert calls[0][1]['input'] == 'data'
    assert 'sudo -n docker exec' in calls[0][0][-1]


def test_python_install_publishes_only_after_success(tmp_path, monkeypatch):
    site = tmp_path / 'site'
    site.mkdir()
    monkeypatch.setattr(payload.site, 'getsitepackages', lambda: [str(site)])
    commands = []
    monkeypatch.setattr(payload, 'run', lambda cmd, **kw: commands.append(cmd))
    payload.install_python(['pdf2image==1.17.0'], 'https://pypi.org/simple/', tmp_path / 'deps')
    assert '--target' in commands[0] and '--upgrade' not in commands[0]
    assert '--index-url' in commands[0]
    pth = list(site.glob('workmate_hotfix_*.pth'))
    assert len(pth) == 1 and pth[0].read_text().strip().startswith(str(tmp_path / 'deps'))


def test_failed_python_install_does_not_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(payload.site, 'getsitepackages', lambda: [str(tmp_path)])
    def fail(*_, **__):
        raise subprocess.CalledProcessError(1, ['pip'])
    monkeypatch.setattr(payload, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        payload.install_python(['bad'], 'https://pypi.org/simple/', tmp_path / 'deps')
    assert not list(tmp_path.glob('*.pth'))
    assert not list((tmp_path / 'deps').iterdir())


def test_checks_execute_in_bubblewrap(monkeypatch):
    commands = []
    monkeypatch.setattr(payload, 'run', lambda cmd, **kw: commands.append(cmd))
    payload.verify(['pdftoppm'], ['fitz'])
    assert commands[0][0] == 'bwrap'
    assert '--ro-bind' in commands[0] and '--cap-drop' in commands[0]
    assert '--clearenv' in commands[0]


@pytest.mark.parametrize('plan', ['Inst libx [1.0] (2.0 Debian)\n', 'Remv libx [1.0]\n'])
def test_apt_refuses_dependency_upgrades_or_removals(monkeypatch, plan):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, plan, '')
    monkeypatch.setattr(payload, 'run', run)
    with pytest.raises(ValueError, match='remove or upgrade'):
        payload.install_apt(['libreoffice'])
    assert len(commands) == 2


def test_apt_additive_install_uses_native_lock_and_no_upgrade(monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, 'Inst newlib (1.0 Debian)\n', '')
    monkeypatch.setattr(payload, 'run', run)
    payload.install_apt(['poppler-utils'])
    assert len(commands) == 3
    assert '--simulate' in commands[1][0]
    assert '-y' in commands[2][0] and '--no-upgrade' in commands[2][0]
    assert commands[2][1]['env']['DEBIAN_FRONTEND'] == 'noninteractive'


@pytest.mark.parametrize('sudo', [False, True])
def test_local_runner_does_not_use_shell(monkeypatch, sudo):
    calls = []
    monkeypatch.setattr(cli.subprocess, 'run', lambda cmd, **kw: calls.append(cmd))
    cli.runner(args(*(['--sudo'] if sudo else [])))(['docker', 'ps'])
    assert calls[0] == (['sudo', '-n'] if sudo else []) + ['docker', 'ps']


def test_empty_discovery_is_failure():
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '', '')
    with pytest.raises(ValueError, match='No running'):
        cli.execute(args(), run)


def test_invalid_inspection_is_failure():
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '[]', '')
    with pytest.raises(ValueError, match='Incomplete'):
        cli.inspect(run, ['a' * 64], 'workmate')


def test_inspection_transport_failure():
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', '')
    with pytest.raises(ValueError, match='inspection failed'):
        cli.inspect(run, ['a' * 64], 'workmate')


def test_main_reports_errors(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'runner', lambda _: Docker(failure=True))
    assert cli.main(['--target', 'local', '--apt', 'poppler-utils', '--apply']) == 1
    assert 'partial changes' in capsys.readouterr().err


def test_main_preview(monkeypatch):
    monkeypatch.setattr(cli, 'runner', lambda _: Docker())
    assert cli.main(['--target', 'local', '--apt', 'poppler-utils']) == 0


def test_apply_serializes_and_calls_installers(monkeypatch):
    calls = []
    monkeypatch.setattr(payload, 'install_apt', lambda p, *mirrors: calls.append(('apt', p)))
    monkeypatch.setattr(payload, 'install_python', lambda p, u: calls.append(('pip', p)))
    monkeypatch.setattr(payload, 'verify', lambda c, m: calls.append(('checks', m)))
    payload.apply({'apt': ['poppler-utils'], 'pip': ['pdf2image'], 'pip_index_url':
                   'https://pypi.org/simple/', 'check_command': [], 'check_import': ['pdf2image']})
    assert [c[0] for c in calls] == ['apt', 'pip', 'checks']


def test_no_package_rejected():
    with pytest.raises(SystemExit):
        cli.parse_args(['--target', 'local'])


def test_mirror_arguments_travel_to_payload():
    run = Docker()
    assert cli.execute(args('--apt-mirror', 'https://mirrors.aliyun.com/debian',
                            '--apt-security-mirror', 'https://mirrors.aliyun.com/debian-security',
                            '--apply'), run) == 0
    _, options = next(item for item in run.commands if item[0][1] == 'exec')
    assert 'https://mirrors.aliyun.com/debian-security' in options['input']


@pytest.mark.parametrize('extra', [
    ['--apt-mirror', 'https://example.org/debian'],
    ['--apt-security-mirror', 'https://example.org/debian-security'],
    ['--apt-mirror', 'https://user:secret@example.org/debian', '--apt-security-mirror', 'https://example.org/security'],
    ['--apt-mirror', 'https://example.org/debian\nTrusted: yes', '--apt-security-mirror', 'https://example.org/security'],
])
def test_invalid_or_incomplete_mirrors_are_rejected(extra):
    with pytest.raises(SystemExit):
        args(*extra)


def test_mirror_sources_preserve_suites_components_and_signature():
    source = ('Types: deb\nURIs: http://deb.debian.org/debian\n'
              'Suites: bookworm bookworm-updates\nComponents: main contrib\n'
              'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\n'
              'Types: deb\nURIs: http://deb.debian.org/debian-security\n'
              'Suites: bookworm-security\nComponents: main\n'
              'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n')
    result = payload.mirror_sources(source, 'https://mirror/debian', 'https://mirror/security')
    assert 'URIs: https://mirror/debian\n' in result
    assert 'URIs: https://mirror/security\n' in result
    assert 'Components: main contrib' in result
    assert result.count('Signed-By:') == 2 and 'bookworm-updates' in result
    assert 'Trusted:' not in result


def test_temporary_mirror_does_not_change_container_sources(tmp_path, monkeypatch):
    sources = tmp_path / 'debian.sources'
    original = 'Types: deb\nURIs: http://deb.debian.org/debian\nSuites: bookworm\nSigned-By: /keyring\n'
    sources.write_text(original)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        option = next(x for x in command if x.startswith('Dir::Etc::sourcelist='))
        assert 'URIs: https://mirror/debian' in Path(option.split('=',1)[1]).read_text()
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(payload, 'run', run)
    payload.install_apt(['poppler-utils'], 'https://mirror/debian', 'https://mirror/security', sources)
    assert sources.read_text() == original
    temporary = next(x.split('=',1)[1] for x in commands[0] if x.startswith('Dir::Etc::sourcelist='))
    assert not Path(temporary).exists()
    assert all('Dir::Etc::sourceparts=-' in command for command in commands)


def test_unsupported_sources_fail_before_apt(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(payload, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match='debian.sources'):
        payload.install_apt(['ffmpeg'], 'https://mirror/debian', 'https://mirror/security', tmp_path/'missing')
    assert not calls


def test_discovery_reports_docker_permission_error():
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '',
        'permission denied while trying to connect to the docker API at unix:///var/run/docker.sock')
    with pytest.raises(ValueError, match='--sudo'):
        cli.execute(args(), run)


def test_discovery_distinguishes_absent_project():
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '', '')
    with pytest.raises(ValueError, match='workmate'):
        cli.execute(args(), run)


def test_discovery_reports_daemon_failure():
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', 'Cannot connect to the Docker daemon')
    with pytest.raises(ValueError, match='Docker daemon'):
        cli.execute(args(), run)


def test_busy_install_lock_returns_clear_retryable_error(tmp_path):
    import fcntl
    path = tmp_path / 'installer.lock'
    with path.open('a') as holder, path.open('a') as contender:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as error:
            payload.acquire_install_lock(contender)
        assert error.value.code == 75
        # Busy attempt must not unlink/recreate the live lock.
        assert path.exists()


def test_busy_installer_report_does_not_claim_install_failed():
    docker = Docker()
    def run(command, **kwargs):
        if command[:2] == ['docker', 'exec']:
            return subprocess.CompletedProcess(command, 75, '', '')
        return docker(command, **kwargs)
    with pytest.raises(ValueError, match='Another installer'):
        cli.execute(args('--apply'), run)
