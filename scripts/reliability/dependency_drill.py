"""FAULT isolated dependency fault drill; only newly labelled local containers."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import socket
import sys
import time
import uuid

from .cold_backup import docker, local_daemon
from .cleanup import source_hashes

ROOT = Path(__file__).resolve().parents[2]
POLICY = {'pause_seconds': 2, 'operation_timeout_seconds': .5,
          'redelivery_after_seconds': 10, 'recovery_deadline_seconds': 20,
          'lease_seconds': 1, 'claim_observation_seconds': 2}


def validate_owner(owner, name, labels):
    if not re.fullmatch(r'fault-dependency-[0-9a-f]{12}', owner):
        raise ValueError('Invalid drill owner')
    if name not in (owner+'-postgres', owner+'-redis'):
        raise ValueError('Not a dedicated dependency')
    if labels.get('workmate.validation.owner') != owner:
        raise ValueError('Ownership label mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(POLICY)); return
    local_daemon()
    directory = args.output.resolve(); directory.mkdir(parents=True, exist_ok=False)
    owner = 'fault-dependency-'+uuid.uuid4().hex[:12]
    images = {role: docker('image','inspect',image,'--format','{{.Id}}') for role,image in
              [('postgres','postgres:18-alpine'),('redis','redis:7-alpine')]}
    report = {'passed':False, 'owner':owner, 'images':images, 'policy':POLICY}
    (directory/'plan.json').write_text(json.dumps(report,indent=2)+'\n')
    (directory/'source.json').write_text(json.dumps(source_hashes(ROOT),indent=2)+'\n')
    names=[]
    before=set(docker('ps','-aq').split())
    env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1', 'FAULT_DEPENDENCY_OWNER':owner,
         'FAULT_DEPENDENCY_EVIDENCE':str(directory), 'WORKMATE_CONFIG_FILE':''}
    env['PYTHONPATH']=':'.join(str(ROOT/p) for p in ['.','packages/product/src',
        'packages/product/tests','packages/contracts/src','runtime','runtime/tests'])
    try:
        for role,port in [('postgres',5432),('redis',6379)]:
            name=owner+'-'+role; names.append(name)
            options=['--tmpfs','/var/lib/postgresql:rw,size=512m','-e','POSTGRES_HOST_AUTH_METHOD=trust',
                     '-e','POSTGRES_DB=drill'] if role=='postgres' else ['--tmpfs','/data:rw,size=128m']
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0))
                host_port=reservation.getsockname()[1]
            docker('run','-d','--name',name,'--label','workmate.validation.owner='+owner,
                   '--memory','1g','--cpus','1','--pids-limit','128','-p',f'127.0.0.1:{host_port}:{port}',*options,images[role])
            published=docker('port',name,str(port)).rsplit(':',1)[1]
            env['WORKMATE_TEST_POSTGRES_DSN' if role=='postgres' else 'WORKMATE_TEST_REDIS_URL']=(
                f'postgresql://postgres@127.0.0.1:{published}/drill' if role=='postgres' else f'redis://127.0.0.1:{published}/0')
        deadline=time.monotonic()+40
        while subprocess.run(['docker','exec',names[0],'pg_isready','-U','postgres'],capture_output=True).returncode:
            if time.monotonic()>deadline: raise TimeoutError('PostgreSQL startup')
            time.sleep(.2)
        with (directory/'tests.log').open('w') as output:
            result=subprocess.run([sys.executable,'-m','pytest','-c','/dev/null','--asyncio-mode=auto',
                'scripts/reliability/test_dependency_faults.py','-q','-ra',
                '-o','cache_dir='+str(directory/'pytest-cache'),'--junitxml',str(directory/'junit.xml')],env=env,cwd=ROOT,stdout=output,stderr=subprocess.STDOUT,timeout=180)
        report['exit_code']=result.returncode
        report['passed']=result.returncode==0 and 'skipped' not in (directory/'tests.log').read_text()
    finally:
        errors=[]
        for name in reversed(names):
            try:
                data=json.loads(docker('inspect',name))[0]
                validate_owner(owner,name,data['Config']['Labels'])
                if data['State']['Paused']: docker('unpause',name)
                (directory/(name+'.log')).write_text(docker('logs',name))
                docker('rm','-f','-v',name)
            except Exception as error: errors.append(type(error).__name__+': '+str(error))
        report['remaining']=docker('ps','-aq','--filter','label=workmate.validation.owner='+owner).split()
        report['existing_containers_preserved']=before.issubset(set(docker('ps','-aq').split()))
        report['cleanup_errors']=errors
        report['passed']=report['passed'] and not errors and not report['remaining'] and report['existing_containers_preserved']
        (directory/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    if not report['passed']: raise SystemExit('FAULT dependency drill failed; see '+str(directory))
    print(json.dumps(report))


if __name__=='__main__': main()
