"""Temporarily install additive dependencies in running Compose Runtime containers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import urlsplit


APT_PACKAGE = r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?(?:=[A-Za-z0-9.+:~_-]+)?'
PIP_PACKAGE = r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:==[A-Za-z0-9_.+!-]+)?'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['local', 'remote'], required=True)
    parser.add_argument('--remote', help='SSH target required for --target remote')
    parser.add_argument('--project', default='workmate')
    parser.add_argument('--sudo', action='store_true', help='Use sudo -n for local Docker (remote always uses it)')
    parser.add_argument('--apt', action='append', default=[], metavar='PACKAGE')
    parser.add_argument('--apt-mirror', help='Temporary Debian archive mirror (requires security mirror)')
    parser.add_argument('--apt-security-mirror', help='Temporary Debian security archive mirror')
    parser.add_argument('--pip', action='append', default=[], metavar='PACKAGE[==VERSION]')
    parser.add_argument('--pip-index-url', default='https://pypi.tuna.tsinghua.edu.cn/simple')
    parser.add_argument('--check-command', action='append', default=[])
    parser.add_argument('--check-import', action='append', default=[])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if args.target == 'remote' and not args.remote:
        parser.error('--remote is required for a remote target')
    patterns = [(args.apt, APT_PACKAGE), (args.pip, PIP_PACKAGE),
                (args.check_command, r'[A-Za-z0-9][A-Za-z0-9_.-]*'),
                (args.check_import, r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*'),
                ([args.project], r'[a-z0-9][a-z0-9_-]*'),
                ([args.remote] if args.remote else [], r'(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*')]
    if any(not re.fullmatch(pattern, value) for values, pattern in patterns for value in values):
        parser.error('Invalid package, check, Compose project or SSH target')
    for value in (args.pip_index_url, args.apt_mirror, args.apt_security_mirror):
        if value is None:
            continue
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(c.isspace() for c in value)):
            parser.error('Package mirrors must be credential-free HTTPS URLs')
    if bool(args.apt_mirror) != bool(args.apt_security_mirror):
        parser.error('Specify both --apt-mirror and --apt-security-mirror')
    if args.apt_mirror and not args.apt:
        parser.error('APT mirrors require at least one --apt package')
    if not args.apt and not args.pip:
        parser.error('Specify at least one --apt or --pip package')
    return args


def runner(args):
    def invoke(command, **kwargs):
        if args.target == 'remote':
            command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                       args.remote, shlex.join(['sudo', '-n', *command])]
        elif args.sudo:
            command = ['sudo', '-n', *command]
        return subprocess.run(command, text=True, timeout=1900, **kwargs)
    return invoke


def validate_container(row, project):
    labels = row['Config'].get('Labels') or {}
    image = row['Config']['Image']
    if (labels.get('com.docker.compose.project') != project
            or labels.get('com.docker.compose.service') != 'runtime'
            or not row['State']['Running']
            or not re.fullmatch(r'(?:[A-Za-z0-9._:/-]+/)?workmate-runtime:[A-Za-z0-9_.-]+', image)):
        raise ValueError('Target is not a running Workmate Runtime in the selected project')
    return row['Id'], row['State']['StartedAt']


def inspect(run, identifiers, project):
    result = run(['docker', 'inspect', *identifiers], capture_output=True)
    if result.returncode:
        raise ValueError('Container inspection failed')
    rows = json.loads(result.stdout)
    if len(rows) != len(identifiers):
        raise ValueError('Incomplete container inspection')
    for row in rows:
        validate_container(row, project)
    return rows


def execute(args, run):
    result = run(['docker', 'ps', '--no-trunc', '--filter',
                  f'label=com.docker.compose.project={args.project}', '--filter',
                  'label=com.docker.compose.service=runtime', '--format', '{{.ID}}'], capture_output=True)
    identifiers = result.stdout.split()
    if result.returncode:
        diagnostic = (result.stderr or '').lower()
        if 'permission denied' in diagnostic and ('docker' in diagnostic or 'docker.sock' in diagnostic):
            raise ValueError('Docker access denied; for local execution add --sudo or obtain Docker socket access')
        if 'cannot connect to the docker daemon' in diagnostic:
            raise ValueError('Cannot connect to the Docker daemon; check Docker service and context')
        raise ValueError('Docker discovery failed; check Docker/SSH/sudo access on the selected target')
    if not identifiers:
        raise ValueError(f'No running Runtime containers found in Compose project {args.project}; '
                         'check --project and --target (local means this machine, remote requires --remote)')
    if any(not re.fullmatch(r'[a-f0-9]{64}', x) for x in identifiers):
        raise ValueError('Unexpected Docker container identifiers')
    rows = inspect(run, identifiers, args.project)
    spec = {key: getattr(args, key) for key in ('apt', 'apt_mirror', 'apt_security_mirror',
                                              'pip', 'pip_index_url', 'check_command', 'check_import')}
    print(json.dumps({'containers': [row['Name'].lstrip('/') for row in rows], **spec}, ensure_ascii=False, indent=2))
    if not args.apply:
        print('Preview only. Add --apply to install into every listed container without restarting it.')
        return 0
    source = Path(__file__).with_name('runtime_install_payload.py').read_text(encoding='utf-8')
    program = source + '\napply(json.loads(' + repr(json.dumps(spec)) + '))\n'
    for row in rows:
        identifier, started = validate_container(row, args.project)
        fresh = inspect(run, [identifier], args.project)[0]
        if validate_container(fresh, args.project) != (identifier, started):
            raise ValueError('Container changed before installation')
        print(f'Installing in {row["Name"]} ({identifier[:12]})', flush=True)
        result = run(['docker', 'exec', '-i', '--user', '0', identifier, 'python', '-u', '-'], input=program)
        if result.returncode == 75:
            raise ValueError('Another installer is running in this container; wait before retrying. '
                             'This container was not modified by this attempt; earlier replicas may have completed')
        if result.returncode:
            raise ValueError('Installation/check failed; partial changes may remain; remaining replicas were not modified')
        fresh = inspect(run, [identifier], args.project)[0]
        if validate_container(fresh, args.project) != (identifier, started):
            raise ValueError('Container changed during installation; verify deployment')
    print('All selected Runtime replicas completed; container start times are unchanged.')
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        return execute(args, runner(args))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
