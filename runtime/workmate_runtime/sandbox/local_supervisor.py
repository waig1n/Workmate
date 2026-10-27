"""Linux unprivileged subreaper for local commands, not a security boundary.

The receipt is created only after normal and adopted daemon descendants have
been killed and reaped. Same-UID hostile code remains outside this guarantee.
"""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def drain():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    deadline = time.monotonic() + 2
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        children = Path('/proc/self/task', str(os.getpid()), 'children').read_text().split()
        for child in children:
            try:
                fd = os.pidfd_open(int(child))
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
            except ProcessLookupError:
                pass
        if time.monotonic() > deadline:
            raise RuntimeError('Local descendant cleanup did not complete')
        time.sleep(.01)


def main():
    receipt, *command = sys.argv[1:]
    Path(receipt).unlink(missing_ok=True)
    cwd = None
    if command[:1] == ['--cwd']:
        cwd, separator, *command = command[1:]
        if separator != '--':
            raise ValueError('Invalid local supervisor arguments')
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('Local child subreaper is unavailable')
    def interrupted(*_):
        raise InterruptedError('Local command stopped')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    result = 127
    try:
        if cwd is not None:
            os.chdir(cwd)
        process = subprocess.Popen(command, close_fds=True)
        result = process.wait()
    except InterruptedError:
        result = 130
    except OSError as error:
        print(str(error), file=sys.stderr)
    finally:
        drain()
        Path(receipt).write_text('clean')
    raise SystemExit(result if result >= 0 else 128 - result)


if __name__ == '__main__':
    main()
