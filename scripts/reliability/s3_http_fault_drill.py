"""Owned S3 outage with real multipart HTTP, Worker artifacts and PG metadata."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from scripts.reliability.cold_backup import local_daemon
from scripts.reliability.load import ROOT, PYTHONPATH, cleanup_owned, resource_args


def run(args):
    local_daemon()
    owner='reliability-load-'+uuid.uuid4().hex[:12]
    directory=args.output.resolve()/owner
    evidence=directory/'evidence'; evidence.mkdir(parents=True,mode=0o700)
    directory.chmod(0o700)
    disk=shutil.disk_usage(directory)
    assert disk.free-max(20*2**30,disk.total//10)>4*2**30,'Insufficient disk reserve'
    source=directory/'source'
    for relative in ('runtime/workmate_runtime','runtime/tests','packages/product/src','packages/contracts/src','backend/app',
                     'backend/alembic','scripts/reliability','config'):
        shutil.copytree(ROOT/relative,source/relative,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copy2(ROOT/'backend/alembic.ini',source/'backend/alembic.ini')
    (evidence/'source.json').write_text(json.dumps({str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob('*') if p.is_file()},indent=2))
    def docker(*parts):
        return subprocess.run(['docker',*parts],check=True,capture_output=True,text=True,timeout=90).stdout.strip()
    images={key:docker('image','inspect',value,'--format','{{.Id}}') for key,value in {
        'runtime':'workmate-runtime:2.0','gateway':'workmate-gateway:2.0','pg':'postgres:18-alpine',
        'redis':'redis:7-alpine','s3':'rustfs/rustfs:1.0.0-rc.6'}.items()}
    def existing():
        ids=docker('ps','-aq','--filter','label=com.docker.compose.project').split()
        return {cid:json.loads(docker('inspect',cid,'--format',
            '{"started":{{json .State.StartedAt}},"restarts":{{.RestartCount}}}')) for cid in ids}
    before=existing(); (evidence/'existing-before.json').write_text(json.dumps(before,indent=2))
    (evidence/'plan.json').write_text(json.dumps({'owner':owner,'images':images,'real_model_budget':0,
        'task_deadline_seconds':90,'artifact_visibility_seconds':5,'storage_timeout_seconds':1,
        'http_failure_budget_seconds':15,'http_failure_budget_basis':'3 SDK attempts with 1s connect/read plus backoff and HTTP margin',
        'component_failure_budget_seconds':60,'scope':'actual multipart upload, completed Worker artifact, HTTP downloads, PG metadata and paused-S3 checkpoint/seal components'},indent=2))
    extra=directory/'extra-gateway'; extra.mkdir()
    shared=directory/'shared'; shared.mkdir()
    containers=[]; network=False; process=None; paused=False; passed=False
    pg,redis,s3=[owner+'-'+kind for kind in ('pg','redis','s3')]
    def envs(values): return [part for k,v in values.items() for part in ('-e',k+'='+str(v))]
    def mounts():
        return [part for path,target,ro in ((source,'/source',True),(source/'scripts/reliability/fault_support','/fault',True),
            (extra,'/extra-gateway',True),(shared,'/shared',False),(evidence,'/evidence',False))
            for part in ('--mount',f'type=bind,src={path},dst={target}'+(',readonly' if ro else ''))]
    def launch(name,*parts):
        containers.append(name)
        return docker('run','--name',name,'--label','workmate.validation.owner='+owner,'--network',owner,*parts)
    base={'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':'/fault:'+PYTHONPATH,
        'WORKMATE_CONFIG_FILE':'/source/scripts/reliability/load_support/config.yaml','WORKMATE_LOAD_MODEL_KEY':'local-fixture-only',
        'DATABASE_URL':f'postgresql+asyncpg://fixture:fixture@{pg}:5432/fixture','WORKMATE_COORDINATION_REDIS_URL':f'redis://{redis}:6379/0',
        'WORKMATE_COORDINATION_NAMESPACE':owner,'WORKMATE_INTERNAL_TOKEN':'fault-fixture-internal','RUNTIME_INTERNAL_TOKEN':'fault-fixture-internal',
        'WORKMATE_WORKER_STORAGE_NODE_ID':'fault-s3-http','WORKMATE_WORKER_NODES':json.dumps([{'storage_node_id':'fault-s3-http','base_url':'http://127.0.0.1:8000'}]),
        'WORKMATE_WORKER_LEASE_SECONDS':'9','WORKMATE_WORKER_CAPACITY':'1','WORKMATE_WORKER_DRAIN_SECONDS':'5',
        'WORKMATE_WORKSPACE_BASEDIR':'/shared/workspaces','ARTIFACT_STORAGE_DIR':'/shared/artifacts','REME_ENABLED':'false',
        'ARTIFACT_STORAGE_BACKEND':'s3','ARTIFACT_STORAGE_WORKSPACE_CHECKPOINT_ENABLED':'true',
        'ARTIFACT_STORAGE_TIMEOUT_SECONDS':'1','ARTIFACT_S3_ENDPOINT':f'http://{s3}:9000',
        'ARTIFACT_S3_BUCKET':'fault-artifacts','ARTIFACT_S3_ACCESS_KEY':'fault-fixture','ARTIFACT_S3_SECRET_KEY':'fault-fixture-secret',
        'SKILL_S3_ENDPOINT':f'http://{s3}:9000','SKILL_S3_BUCKET':'fault-skills','SKILL_S3_ACCESS_KEY':'fault-fixture','SKILL_S3_SECRET_KEY':'fault-fixture-secret'}
    try:
        docker('network','create','--internal','--label','workmate.validation.owner='+owner,owner); network=True
        launch(pg,'-d',*resource_args('migration'),*envs({'POSTGRES_USER':'fixture','POSTGRES_PASSWORD':'fixture','POSTGRES_DB':'fixture'}),images['pg'])
        launch(redis,'-d',*resource_args('redis'),images['redis'])
        launch(s3,'-d','--user','0','--memory','768m','--memory-swap','768m','--cpus','1','--pids-limit','256',
            *envs({'RUSTFS_ACCESS_KEY':'fault-fixture','RUSTFS_SECRET_KEY':'fault-fixture-secret','RUSTFS_CONSOLE_ENABLE':'false'}),images['s3'],'/data')
        deadline=time.monotonic()+60
        while True:
            try: docker('exec',pg,'pg_isready','-U','fixture'); break
            except subprocess.CalledProcessError:
                if time.monotonic()>deadline: raise TimeoutError('PG readiness')
                time.sleep(.25)
        copy=owner+'-copy'; containers.append(copy)
        docker('create','--name',copy,'--label','workmate.validation.owner='+owner,'--entrypoint','true',images['gateway'])
        docker('cp',copy+':/usr/local/lib/python3.12/site-packages/.',str(extra))
        launch(owner+'-migration',*resource_args('migration'),*envs(base),*mounts(),'--workdir','/source/backend',
            '--entrypoint','python',images['gateway'],'-m','alembic','upgrade','head')
        launch(owner+'-buckets',*resource_args('migration'),*envs(base),'--entrypoint','python',images['gateway'],'-c',
            "import boto3,os,time; time.sleep(3); s=boto3.client('s3',endpoint_url=os.environ['ARTIFACT_S3_ENDPOINT'],aws_access_key_id='fault-fixture',aws_secret_access_key='fault-fixture-secret'); [s.create_bucket(Bucket=b) for b in ('fault-artifacts','fault-skills')]")
        host_s3_ip=docker('inspect',s3,'--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
        checked=subprocess.run([str(ROOT/'runtime/.venv/bin/python'),'-c',
            "import boto3,sys; s=boto3.client('s3',endpoint_url=sys.argv[1],aws_access_key_id='fault-fixture',aws_secret_access_key='fault-fixture-secret'); s.head_bucket(Bucket='fault-artifacts')",
            f'http://{host_s3_ip}:9000'],capture_output=True,text=True,timeout=15)
        assert checked.returncode==0, 'Host component S3 endpoint was not healthy before injection'
        (evidence/'component-s3-baseline.json').write_text(json.dumps({'head_bucket_http':200}))
        name=owner+'-scenario'; containers.append(name)
        docker('create','--init','--name',name,'--label','workmate.validation.owner='+owner,'--network',owner,
            *resource_args('workload'),*envs(base),*mounts(),'--cap-add','SYS_ADMIN','--security-opt','seccomp=unconfined',
            '--security-opt','apparmor=unconfined','--entrypoint','python',images['runtime'],'/fault/s3_http.py')
        log=(evidence/'scenario.log').open('w')
        process=subprocess.Popen(['docker','start','-a',name],stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+300
        while process.poll() is None:
            if time.monotonic()>deadline: raise TimeoutError('S3 HTTP scenario deadline')
            if (evidence/'pause-request.json').exists() and not (evidence/'paused.json').exists():
                assert docker('inspect',s3,'--format','{{index .Config.Labels "workmate.validation.owner"}}')==owner
                docker('pause',s3); paused=True
                (evidence/'paused.json').write_text(json.dumps({'time':time.monotonic()}))
            if (evidence/'resume-request.json').exists() and paused:
                pg_ip=docker('inspect',pg,'--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
                component_env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1',
                    'WORKMATE_TEST_POSTGRES_DSN':f'postgresql://fixture:fixture@{pg_ip}:5432/fixture',
                    'FAULT_TEST_S3_ENDPOINT':f'http://{host_s3_ip}:9000',
                    'PYTHONPATH':':'.join(str(source/p) for p in ('.','runtime','runtime/tests','packages/product/src','packages/contracts/src'))}
                with (evidence/'components.log').open('w') as component_log:
                    checked=subprocess.run([str(ROOT/'runtime/.venv/bin/python'),'-m','pytest','-c','runtime/pyproject.toml',
                        str(source/'scripts/reliability/fault_support/test_s3_components.py'),'-q','-ra'],cwd=ROOT,env=component_env,
                        stdout=component_log,stderr=subprocess.STDOUT,timeout=60)
                component_text=(evidence/'components.log').read_text()
                assert checked.returncode==0 and 'skipped' not in component_text, 'S3 PG components failed'
                docker('unpause',s3); paused=False
                (evidence/'resumed.json').write_text(json.dumps({'time':time.monotonic()}))
            time.sleep(.2)
        log.close()
        assert process.returncode==0,'Scenario failed; see '+str(evidence/'scenario.log')
        passed=json.loads((evidence/'result.json').read_text())['passed']
    finally:
        cleanup_errors=[]
        if paused:
            try: docker('unpause',s3)
            except Exception as error: cleanup_errors.append({'unpause':type(error).__name__})
        cleanup=cleanup_owned(owner,containers,network,evidence)
        if process is not None:
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        try:
            after=existing()
            preserved=all(after.get(cid)==state for cid,state in before.items())
        except Exception as error:
            after={}; preserved=False
            cleanup_errors.append({'existing_inventory':type(error).__name__})
        (evidence/'existing-after.json').write_text(json.dumps(after,indent=2))
        report={'passed':passed and preserved and not cleanup['failures'] and not cleanup['remaining_containers']
            and not cleanup['remaining_networks'] and not cleanup['remaining_volumes'],
            'existing_preserved':preserved,'cleanup':cleanup,'owner':owner,'cleanup_errors':cleanup_errors}
        report['passed'] = report['passed'] and not cleanup_errors
        (directory/'result.json').write_text(json.dumps(report,indent=2))
    assert report['passed'],report
    print(json.dumps({'passed':True,'evidence':str(evidence)}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True); parser.add_argument('--run',action='store_true')
    args=parser.parse_args()
    if args.run: run(args)
    else: print('Prepared: new internal-network PG/Redis/RustFS and Gateway/Worker only; real models=0.')


if __name__=='__main__': main()
