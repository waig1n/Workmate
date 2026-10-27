"""Standard-library installer sent to a running Runtime container over stdin."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import site
import subprocess
import sys
import tempfile
import uuid


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, timeout=1800, **kwargs)


def mirror_sources(source, mirror, security_mirror):
    sections = source.strip().split('\n\n')
    rewritten = []
    for section in sections:
        suites = re.search(r'^Suites:\s*(.+)$', section, re.MULTILINE)
        if not suites or not re.search(r'^Signed-By:\s*\S+', section, re.MULTILINE):
            raise ValueError('Unsupported debian.sources: missing Suites or Signed-By')
        selected = security_mirror if any(x.endswith('-security') for x in suites[1].split()) else mirror
        content, count = re.subn(r'^URIs:.*$', 'URIs: ' + selected.rstrip('/'), section, flags=re.MULTILINE)
        if count != 1:
            raise ValueError('Unsupported debian.sources: expected one URIs field per section')
        rewritten.append(content)
    return '\n\n'.join(rewritten) + '\n'


def install_apt(packages, mirror=None, security_mirror=None,
                source_path=Path('/etc/apt/sources.list.d/debian.sources')):
    options = ['-o', 'DPkg::Lock::Timeout=60']
    if mirror is None:
        return apt_commands(packages, options)
    if not source_path.is_file():
        raise ValueError('APT mirror override requires Debian debian.sources')
    content = mirror_sources(source_path.read_text(encoding='utf-8'), mirror, security_mirror)
    with tempfile.TemporaryDirectory(prefix='workmate-apt-') as directory:
        # APT's _apt user must be able to read the temporary public source file.
        os.chmod(directory, 0o755)
        path = Path(directory) / 'debian.sources'
        path.write_text(content, encoding='utf-8')
        os.chmod(path, 0o644)
        apt_commands(packages, [*options, '-o', f'Dir::Etc::sourcelist={path}',
                                '-o', 'Dir::Etc::sourceparts=-'])


def apt_commands(packages, options):
    env = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')
    run(['apt-get', *options, 'update'], env=env)
    install = ['install', '--no-install-recommends', '--no-upgrade', *packages]
    plan = run(['apt-get', *options, '--simulate', *install], env=env, capture_output=True).stdout
    print(plan, flush=True)
    if re.search(r'^Remv |^Inst \S+ \[', plan, re.MULTILINE):
        raise ValueError('APT would remove or upgrade existing packages; use a rebuilt image')
    run(['apt-get', *options, '-y', *install], env=env)


def install_python(packages, index_url, root=Path('/usr/local/share/workmate-hotfix')):
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='python-', dir=root))
    # .pth adds paths after existing site-packages: no overwrite of Runtime deps.
    token = uuid.uuid4().hex
    pth = Path(site.getsitepackages()[0]) / f'workmate_hotfix_{token}.pth'
    try:
        run([sys.executable, '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
             '--no-input', '--retries', '2', '--timeout', '60', '--index-url', index_url,
             '--target', str(directory), *packages])
        temporary = pth.with_suffix('.tmp')
        temporary.write_text(str(directory) + '\n', encoding='utf-8')
        temporary.replace(pth)
    except BaseException:
        shutil.rmtree(directory)
        raise
    print(f'Published additional Python dependencies: {directory}', flush=True)


def verify(commands, modules):
    # This independent sandbox has no business Workspace or Runtime environment.
    with tempfile.TemporaryDirectory(prefix='workmate-install-check-') as directory:
        argv = ['bwrap', '--die-with-parent', '--new-session', '--unshare-pid',
                '--unshare-ipc', '--unshare-uts', '--unshare-cgroup-try',
                '--cap-drop', 'ALL', '--proc', '/proc', '--dev', '/dev',
                '--bind', directory, '/workspace', '--tmpfs', '/tmp',
                '--clearenv', '--setenv', 'HOME', '/workspace', '--setenv',
                'PATH', '/usr/local/bin:/usr/bin:/bin', '--setenv', 'LANG', 'C.UTF-8']
        for path in ('/usr', '/bin', '/sbin', '/lib', '/lib64'):
            if Path(path).is_symlink():
                argv.extend(['--symlink', os.readlink(path), path])
            elif Path(path).exists():
                argv.extend(['--ro-bind', path, path])
        # Match the production mounts needed by dynamic loaders and fonts.
        argv.extend(['--dir', '/etc'])
        for path in ('/etc/alternatives', '/etc/ssl'):
            if Path(path).exists():
                argv.extend(['--ro-bind', path, path])
        source = '''import importlib, json, shutil, subprocess, sys
commands, modules = json.loads(sys.argv[1])
probes = {"soffice": ["--version"], "libreoffice": ["--version"],
          "pdftoppm": ["-v"], "pdfinfo": ["-v"], "ffmpeg": ["-version"]}
for name in commands:
    path = shutil.which(name)
    if path is None: raise RuntimeError("Command unavailable: " + name)
    print("COMMAND", name, path)
    if name in probes:
        subprocess.run([path, *probes[name]], check=True, timeout=30,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
for name in modules:
    module = importlib.import_module(name)
    print("IMPORT", name, getattr(module, "__file__", "builtin"))
'''
        run([*argv, '--', sys.executable, '-c', source, json.dumps([commands, modules])],
            env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'})


def acquire_install_lock(lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('BUSY: another dependency installer holds the container lock. '
              'Wait for it to finish before retrying; do not delete the lock file. '
              'This attempt has not installed any packages.', file=sys.stderr, flush=True)
        raise SystemExit(75)


def apply(spec):
    # Serializes this installer per container; apt also uses its native locks.
    with open('/tmp/workmate-runtime-install.lock', 'a') as lock:
        acquire_install_lock(lock)
        if spec['apt']:
            install_apt(spec['apt'], spec.get('apt_mirror'), spec.get('apt_security_mirror'))
        if spec['pip']:
            install_python(spec['pip'], spec['pip_index_url'])
        verify(spec['check_command'], spec['check_import'])
        print('Installation and sandbox checks completed; Runtime was not restarted.', flush=True)
