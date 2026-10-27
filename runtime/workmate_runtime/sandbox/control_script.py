"""Trusted remote control operations; copied to a root-only directory by the Worker."""
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time

UIDS = {62000, 62001}


def processes():
    found = []
    for name in os.listdir('/proc'):
        if not name.isdecimal():
            continue
        try:
            lines = Path('/proc', name, 'status').read_text().splitlines()
        except FileNotFoundError:
            continue
        values = next(line.split()[1:] for line in lines if line.startswith('Uid:'))
        state = next(line.split()[1] for line in lines if line.startswith('State:'))
        if state != 'Z' and UIDS.intersection(map(int, values)):
            found.append(int(name))
    return found


def main():
    directory = Path(__file__).resolve().parent
    operation, payload = sys.argv[1], json.loads(sys.argv[2])
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'state.json'
        state = json.loads(path.read_text())
        if operation == 'begin':
            if payload['epoch'] in state.get('sealed', []):
                raise RuntimeError('Execution epoch was permanently sealed')
            if state['active'] and state['epoch'] != payload['epoch']:
                raise RuntimeError('Another Task is active')
            if not state['active'] and processes():
                raise RuntimeError('Previous execution is not quiescent')
            state.update(active=True, epoch=payload['epoch'])
            path.write_text(json.dumps(state))
        elif operation == 'seal':
            if state['epoch'] != payload['epoch']:
                raise RuntimeError('Cleanup epoch differs')
            state['active'] = False
            state['sealed'] = sorted(set(state.get('sealed', [])) | {payload['epoch']})
            path.write_text(json.dumps(state))
        elif operation == 'kill':
            if state['active'] or state['epoch'] != payload['epoch']:
                raise RuntimeError('Seal exact execution before cleanup')
            deadline = time.monotonic() + 3
            while processes():
                for pid in processes():
                    try:
                        fd = os.pidfd_open(pid)
                        try:
                            # pidfd pins the process even if its numeric PID is recycled.
                            if pid in processes():
                                signal.pidfd_send_signal(fd, signal.SIGKILL)
                        finally:
                            os.close(fd)
                    except ProcessLookupError:
                        pass
                if time.monotonic() >= deadline:
                    raise RuntimeError('Execution processes remain')
                time.sleep(.02)
        elif operation == 'probe':
            print(json.dumps({'active': state['active'], 'epoch': state['epoch'], 'pids': processes()}))
            return
        else:
            raise ValueError('Unknown operation')
    print('{}')


if __name__ == '__main__':
    main()
