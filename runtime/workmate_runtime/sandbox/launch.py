"""Installed root-owned inside a remote sandbox; never execute from Agent files."""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

TASK_UID = 62000
MCP_UID = 62001


def demote(uid):
    if os.geteuid() != 0 or uid not in {TASK_UID, MCP_UID}:
        raise RuntimeError('Dedicated execution requires a root control channel')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), 'Cannot disable privilege escalation')
    os.setgroups([])
    os.setgid(TASK_UID)
    os.setuid(uid)
    if os.getresuid() != (uid, uid, uid) or libc.prctl(39, 0, 0, 0, 0) != 1:
        raise RuntimeError('Execution identity could not be verified')
    os.umask(0o002)


def main():
    uid = int(sys.argv[1])
    args = sys.argv[2:]
    epoch = None
    if args[:1] == ['--epoch']:
        epoch, args = args[1], args[2:]
    if args[:1] != ['--'] or len(args) < 2:
        raise ValueError('Expected UID [--epoch TOKEN] -- argv')
    directory = Path(__file__).resolve().parent
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads((directory / 'state.json').read_text())
        if not state['active'] or (epoch is not None and state['epoch'] != epoch):
            raise RuntimeError('Task execution is sealed')
        supplied = json.loads(os.environ.get('WORKMATE_MCP_CHILD_ENV', '{}'))
        # Only the child gets MCP credentials; the root interpreter environment is clean.
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': state['workdir'],
            'TMPDIR': '/tmp', 'LANG': 'C.UTF-8', **supplied}
        def child():
            demote(uid)
        process = subprocess.Popen(args[1:], cwd=state['workdir'], env=env, preexec_fn=child)
    # No lock is held while the command runs. Seal takes the same lock before killing all UIDs.
    raise SystemExit(process.wait())


if __name__ == '__main__':
    main()
