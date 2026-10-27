"""Run real Nginx + dual Gateway transport tests on disposable local PG/Redis."""
import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .cold_backup import docker, local_daemon
from .cleanup import attempt, source_hashes

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        print('Prepared: only new labelled local PG/Redis and Nginx test containers.')
        return
    local_daemon()
    directory = args.output.resolve(); directory.mkdir(parents=True, exist_ok=False)
    (directory/'source.json').write_text(json.dumps(source_hashes(ROOT),indent=2)+'\n')
    owner = 'gateway-drill-' + uuid.uuid4().hex[:12]
    images = {key: docker('image','inspect',ref,'--format','{{.Id}}') for key,ref in {
        'postgres':'postgres:18-alpine','redis':'redis:7-alpine','nginx':'nginx:latest'}.items()}
    names = []
    report = {'passed': False, 'images': images, 'owner': owner}
    environment = os.environ.copy()
    paths = ['backend','backend/tests','packages/product/src','packages/contracts/src','runtime','runtime/tests',
             'runtime/.venv/lib/python3.12/site-packages']
    environment['PYTHONPATH'] = ':'.join(str(ROOT/p) for p in paths)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment['WORKMATE_TEST_NGINX_IMAGE'] = images['nginx']
    environment['WORKMATE_TEST_ACCEPTANCE_OWNER'] = owner
    process = None
    try:
        for role, port in [('postgres',5432),('redis',6379)]:
            name = owner+'-'+role; names.append(name)
            options = ['--tmpfs','/var/lib/postgresql:rw,size=1g','-e','POSTGRES_HOST_AUTH_METHOD=trust','-e','POSTGRES_DB=drill'] if role=='postgres' else ['--tmpfs','/data:rw,size=512m']
            docker('run','-d','--name',name,'--label','workmate.validation.owner='+owner,
                   '-p',f'127.0.0.1::{port}',*options,images[role])
            published = docker('port',name,str(port)).rsplit(':',1)[1]
            if role=='postgres': environment['WORKMATE_TEST_POSTGRES_DSN']=f'postgresql://postgres@127.0.0.1:{published}/drill'
            else: environment['WORKMATE_TEST_REDIS_URL']=f'redis://127.0.0.1:{published}/0'
        deadline=time.monotonic()+40
        while subprocess.run(['docker','exec',names[0],'pg_isready','-h','127.0.0.1','-U','postgres'],capture_output=True).returncode:
            if time.monotonic()>deadline: raise TimeoutError('PostgreSQL startup')
            time.sleep(.25)
        with (directory/'tests.log').open('w') as output:
            process=subprocess.Popen([sys.executable,'-m','pytest','-c','backend/pyproject.toml',
                'backend/tests/test_gateway_lb_config.py','backend/tests/test_dual_gateway_lb.py',
                'backend/tests/test_dual_gateway_http.py','backend/tests/test_dual_gateway_schedule.py',
                'backend/tests/test_dual_gateway_lb_business.py','backend/tests/test_realtime_db_cancellation.py',
                '-q','-ra','--basetemp',str(directory/'pytest')],
                cwd=ROOT,env=environment,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
            exit_code = process.wait(timeout=360)
        report['exit_code']=exit_code
        text=(directory/'tests.log').read_text()
        report['passed']=exit_code==0 and 'skipped' not in text
    finally:
        errors = []
        if process is not None:
            # The group belongs only to our new session. pytest's timeout/exit
            # must not leave its Gateway children serving after the test ends.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                except OSError as error:
                    errors.append({'operation': 'stop test process group', 'error': str(error)})
                if sig == signal.SIGTERM:
                    time.sleep(1)
            attempt(errors, 'reap test process', lambda: process.wait(timeout=5))
        # Nginx is launched by pytest, but carries this outer run's exact label.
        owned = attempt(errors, 'find owned helpers', lambda:
            docker('ps','-aq','--filter','label=workmate.validation.owner='+owner,
                   '--filter','label=workmate.acceptance.role=proxy').split())
        for container in owned or []:
            attempt(errors, container + ' remove helper', lambda container=container:
                docker('rm','-f','-v',container))
        for name in reversed(names):
            attempt(errors, name + ' logs', lambda name=name:
                (directory/(name+'.log')).write_text(docker('logs',name)))
            attempt(errors, name + ' remove', lambda name=name: docker('rm','-f','-v',name))
        report['remaining'] = attempt(errors, 'verify remaining containers', lambda:
            docker('ps','-aq','--filter','label=workmate.validation.owner='+owner).split())
        report['cleanup_errors'] = errors
        report['passed'] = report['passed'] and not errors and report['remaining'] == []
        (directory/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    if not report['passed'] or report['remaining']:
        raise SystemExit('Acceptance failed; see '+str(directory))
    print(json.dumps({'passed':True,'evidence':str(directory)}))


if __name__=='__main__':
    main()
