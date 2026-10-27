"""Owned PostgreSQL pause while a real Bubblewrap tool is running."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time

from . import load
from .boundary_drill import ROOT, business_inventory


def inject(output, stop, errors):
    paused = None
    try:
        while not stop.wait(.1):
            requests = list(output.glob('reliability-load-*/evidence/pg-pause-request.json'))
            if not requests:
                continue
            assert len(requests) == 1, 'Use a fresh output directory'
            request = requests[0]
            owner = request.parent.parent.name
            assert re.fullmatch(r'reliability-load-[0-9a-f]{12}', owner)
            target = owner + '-pg'
            labels = json.loads(subprocess.check_output(['docker', 'inspect', target,
                '--format', '{{json .Config.Labels}}'], text=True))
            assert labels.get('workmate.validation.owner') == owner
            started = time.monotonic()
            subprocess.run(['docker', 'pause', target], check=True, capture_output=True, timeout=10)
            paused = target
            (request.parent/'pg-paused.json').write_text(json.dumps({'started':started,'container':target}))
            stop.wait(15)
            subprocess.run(['docker', 'unpause', target], check=True, capture_output=True, timeout=10)
            paused = None
            (request.parent/'pg-restored.json').write_text(json.dumps({'started':started,
                'restored':time.monotonic(),'pause_seconds':time.monotonic()-started}))
            return
    except BaseException as error:
        errors.append(type(error).__name__ + ': ' + str(error))
    finally:
        if paused:
            subprocess.run(['docker','unpause',paused],check=True,capture_output=True,timeout=10)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--run',action='store_true')
    args=parser.parse_args()
    if not args.run:
        print('Lease 9s; owned PG pause 15s; real tool sleep 30s; quarantine deadline 90s.');return
    assert not args.output.exists(), 'Fresh output directory required'
    stop=threading.Event();errors=[]
    with tempfile.TemporaryDirectory(prefix='fault-pg-source-') as temporary:
        overlay=Path(temporary)
        for relative in ('runtime/workmate_runtime','packages/product/src','packages/contracts/src',
                         'backend/app','backend/alembic','config'):
            target=overlay/relative;target.parent.mkdir(parents=True,exist_ok=True)
            target.symlink_to(ROOT/relative,target_is_directory=True)
        (overlay/'backend/alembic.ini').symlink_to(ROOT/'backend/alembic.ini')
        shutil.copytree(ROOT/'scripts/reliability',overlay/'scripts/reliability',ignore=shutil.ignore_patterns('__pycache__'))
        support=overlay/'scripts/reliability/load_support'
        for name in ('worker','run','proof','diagnostics'):
            shutil.copy2(ROOT/'scripts/reliability/database_fault_support'/f'{name}.py',support/f'{name}.py')
        load.ROOT=overlay
        options=load.parse_args(['--runtime-image','workmate-runtime:2.0','--gateway-image','workmate-gateway:2.0',
            '--postgres-image','postgres:18-alpine','--output',str(args.output),'--workload','approval','--mode','smoke','--run'])
        before=business_inventory()
        thread=threading.Thread(target=inject,args=(args.output,stop,errors),daemon=False);thread.start()
        try:
            load.execute(options)
        finally:
            stop.set();thread.join(25)
            after=business_inventory();args.output.mkdir(parents=True,exist_ok=True)
            (args.output/'business-preservation.json').write_text(json.dumps({'passed':before==after,
                'before':before,'after':after,'fault_errors':errors,'injector_joined':not thread.is_alive()},indent=2))
        assert not thread.is_alive() and not errors,errors
        assert before==after


if __name__=='__main__':main()
