"""Real Bubblewrap boundary crashes using copied load infrastructure only."""
import argparse
import json
import subprocess
from pathlib import Path
import shutil
import tempfile
from . import load

ROOT=Path(__file__).resolve().parents[2]

def business_inventory():
    ids=subprocess.check_output(['docker','ps','-aq'],text=True).split()
    rows=json.loads(subprocess.check_output(['docker','inspect',*ids],text=True)) if ids else []
    return {row['Id']:{'name':row['Name'],'started_at':row['State']['StartedAt'],
                      'restart_count':row['RestartCount']} for row in rows}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--run',action='store_true')
    args=parser.parse_args()
    if not args.run:
        print('Three SIGKILL boundaries: preparing, release-before, release-after; only owned resources.'); return
    with tempfile.TemporaryDirectory(prefix='fault-boundary-source-') as temporary:
        overlay=Path(temporary)
        for relative in ('runtime/workmate_runtime','packages/product/src','packages/contracts/src','backend/app',
                         'backend/alembic','config'):
            target=overlay/relative; target.parent.mkdir(parents=True,exist_ok=True)
            target.symlink_to(ROOT/relative,target_is_directory=True)
        (overlay/'backend/alembic.ini').symlink_to(ROOT/'backend/alembic.ini')
        shutil.copytree(ROOT/'scripts/reliability',overlay/'scripts/reliability',ignore=shutil.ignore_patterns('__pycache__'))
        support=overlay/'scripts/reliability/load_support'
        shutil.copy2(support/'worker.py',support/'original_worker.py')
        shutil.copy2(ROOT/'scripts/reliability/boundary_drill_worker.py',support/'worker.py')
        shutil.copy2(ROOT/'scripts/reliability/boundary_drill_run.py',support/'run.py')
        load.ROOT=overlay
        options=load.parse_args(['--runtime-image','workmate-runtime:2.0','--gateway-image','workmate-gateway:2.0',
            '--postgres-image','postgres:18-alpine','--output',str(args.output),'--workload','approval','--mode','smoke','--run'])
        before=business_inventory()
        try:
            load.execute(options)
        finally:
            after=business_inventory()
            args.output.mkdir(parents=True,exist_ok=True)
            (args.output/'business-preservation.json').write_text(json.dumps(
                {'passed':before==after,'before':before,'after':after},indent=2)+'\n')
        assert before==after,'Existing business containers changed identity/start/restart count'


if __name__=='__main__': main()
