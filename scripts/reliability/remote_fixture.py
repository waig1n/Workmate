"""Owned remote CAPACITY lifecycle. Dry-run by default; never adopts business resources."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
from scripts.reliability.targets import remote_target
LABEL='workmate.capacity.owner'
ALLOW=('runtime/workmate_runtime','packages/product/src','packages/contracts/src','backend/app','backend/alembic','scripts/reliability','config')
BUDGETS={'app':{'cpus':2,'memory_mib':4096,'pids':512},'pg':{'cpus':1,'memory_mib':1024,'pids':128},
         'redis':{'cpus':.25,'memory_mib':512,'pids':128},'s3':{'cpus':.5,'memory_mib':768,'pids':256}}


def validate_owner(owner):
    if not re.fullmatch(r'capacity-[0-9a-f]{12}',owner):raise ValueError('Unowned CAPACITY identifier')
    return '/tmp/'+owner


def limits(role):return dict(BUDGETS[role])


def verify_labels(labels,owner):
    validate_owner(owner)
    if labels.get(LABEL)!=owner:raise ValueError('Refuse foreign resource')


def tunnel(address,port,remote=None):
    ip=ipaddress.ip_address(address)
    if ip.version!=4 or not ip.is_private or ip.is_loopback or ip.is_unspecified or not 1024<=port<=65535:raise ValueError('Invalid tunnel endpoint')
    return ['ssh','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=15','-N',
            '-L',f'127.0.0.1:{port}:{address}:8080',remote_target(remote)]


def environment(owner):
    validate_owner(owner)
    return {'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':'/capacity:/support:/source/scripts/reliability:/source/runtime:/source/packages/product/src:/source/packages/contracts/src:/source/backend:/source:/extra-gateway',
        'WORKMATE_CONFIG_FILE':'/source/scripts/reliability/load_support/config.yaml','WORKMATE_LOAD_MODEL_KEY':'local-fixture-only',
        'WORKMATE_LOAD_NAMESPACE':owner,'WORKMATE_COORDINATION_NAMESPACE':owner,'WORKMATE_LOAD_MODE':'load','WORKMATE_LOAD_WORKLOAD':'mixed',
        'DATABASE_URL':f'postgresql+asyncpg://fixture:fixture@{owner}-pg:5432/fixture',
        'WORKMATE_COORDINATION_REDIS_URL':f'redis://{owner}-redis:6379/0','REME_ENABLED':'false','WORKSPACE_ASSISTANT_ENABLED':'false',
        'WORKMATE_WORKSPACE_BASEDIR':'/shared/workspaces','ARTIFACT_STORAGE_DIR':'/shared/artifacts',
        'WORKMATE_INTERNAL_TOKEN':'capacity-fixture-internal','RUNTIME_INTERNAL_TOKEN':'capacity-fixture-internal',
        'ARTIFACT_STORAGE_BACKEND':'s3','ARTIFACT_STORAGE_WORKSPACE_CHECKPOINT_ENABLED':'false',
        'ARTIFACT_S3_ENDPOINT':f'http://{owner}-s3:9000','ARTIFACT_S3_BUCKET':'capacity-artifacts',
        'ARTIFACT_S3_ACCESS_KEY':'capacity-fixture','ARTIFACT_S3_SECRET_KEY':'capacity-fixture-secret',
        'SKILL_S3_ENDPOINT':f'http://{owner}-s3:9000','SKILL_S3_BUCKET':'capacity-skills',
        'SKILL_S3_ACCESS_KEY':'capacity-fixture','SKILL_S3_SECRET_KEY':'capacity-fixture-secret',
        'KNOWLEDGE_QDRANT_URL':'http://127.0.0.1:9','KNOWLEDGE_QDRANT_API_KEY':'fixture',
        'KNOWLEDGE_S3_ENDPOINT':f'http://{owner}-s3:9000','KNOWLEDGE_S3_BUCKET':'capacity-knowledge',
        'KNOWLEDGE_S3_ACCESS_KEY':'capacity-fixture','KNOWLEDGE_S3_SECRET_KEY':'capacity-fixture-secret'}


def compare_business(before,after):
    """Normalize legacy 12-hex Docker IDs only when full-ID matching is unique."""
    normalized={};failures={}
    for cid,facts in before.items():
        if re.fullmatch(r'[0-9a-f]{12}',cid):
            matches=[key for key in after if re.fullmatch(r'[0-9a-f]{64}',key) and key.startswith(cid)]
        else:matches=[cid] if cid in after else []
        if len(matches)!=1:
            failures[cid]={'reason':'missing or ambiguous container identity','candidates':matches};continue
        actual=matches[0];normalized[cid]=actual
        if after[actual]!=facts:
            failures[cid]={'reason':'container lifecycle changed','before':facts,'after':after[actual]}
    return {'preserved':not failures,'normalized_mapping':normalized,'failures':failures}


def shutdown_errors(exit_state,ready,receipt,require_pid):
    errors=[]
    if exit_state.get('ExitCode')!=0 or exit_state.get('OOMKilled') or exit_state.get('Running'):
        errors.append('SUT exit not clean')
    if not receipt.get('passed'):errors.append('SUT shutdown receipt missing or failed')
    if require_pid and (not isinstance(ready.get('pid'),int) or ready.get('pid')!=receipt.get('pid')):
        errors.append('SUT receipt PID identity mismatch')
    return errors


def copied_file(path):
    return not (path.is_symlink() or any(part.startswith('.') or part in ('__pycache__','node_modules') for part in path.parts)
                or path.suffix in ('.pyc','.pem','.key','.p12','.pfx'))


def source_snapshot(destination):
    for relative in ALLOW:
        for path in (ROOT/relative).rglob('*'):
            if path.is_file() and copied_file(path.relative_to(ROOT)) and not path.is_symlink():
                target=destination/path.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
    shutil.copy2(ROOT/'backend/alembic.ini',destination/'backend/alembic.ini')
    return {str(p.relative_to(destination)):hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.rglob('*') if p.is_file()}


def run_cmd(parts,**kwargs):
    return subprocess.run(parts,check=True,timeout=kwargs.pop('timeout',90),capture_output=True,**kwargs)


class Remote:
    def __init__(self,state,path):self.state,self.path=state,path;self.owner=state['owner'];self.root=validate_owner(self.owner)
    def save(self):self.path.write_text(json.dumps(self.state,indent=2)+'\n')
    def ssh(self,parts,**kw):return run_cmd(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',remote_target(self.state.get('host')),shlex.join(parts)],**kw)
    def docker(self,*parts):return self.ssh(['sudo','-n','docker',*parts]).stdout.decode().strip()
    def verify(self,name,network=False):
        labels=json.loads(self.docker(*(['network','inspect'] if network else ['inspect']),name,'--format','{{json .Labels}}' if network else '{{json .Config.Labels}}'))
        verify_labels(labels,self.owner)
    def inventory(self):
        # One SSH and one batched inspect; never emit Config.Env or full inspect.
        script = """import json,subprocess
cmd=['sudo','-n','docker']
ids=subprocess.check_output(cmd+['ps','-aq','--filter','label=com.docker.compose.project'],timeout=30,text=True).split()
result={}
if ids:
    fmt='{"id":{{json .Id}},"owner":{{json (index .Config.Labels "workmate.capacity.owner")}},"started":{{json .State.StartedAt}},"restarts":{{.RestartCount}}}'
    rows=subprocess.check_output(cmd+['inspect','--format',fmt,*ids],timeout=45,text=True)
    for line in rows.splitlines():
        item=json.loads(line)
        if not item['owner']:result[item['id']]={'started':item['started'],'restarts':item['restarts']}
print(json.dumps(result))
"""
        return json.loads(self.ssh(['python3','-c',script]).stdout)
    def sample(self):
        script="""import os,json,pathlib
r=pathlib.Path(%r);s=os.statvfs('/tmp');m={l.split(':')[0]:int(l.split()[1])*1024 for l in pathlib.Path('/proc/meminfo').read_text().splitlines()}
print(json.dumps({'available_bytes':m['MemAvailable'],'disk_free_bytes':s.f_bavail*s.f_frsize,'disk_total_bytes':s.f_blocks*s.f_frsize}))""" % self.root
        result=json.loads(self.ssh(['python3','-c',script]).stdout)
        # du includes PG, Redis AOF, S3, workspace, evidence; not source/dependencies.
        if self.state.get('remote_created'):
            result['data_bytes']=int(self.ssh(['sudo','-n','du','-sb',self.root+'/data']).stdout.split()[0])
        else:result['data_bytes']=0
        return result
    def inspect_owned(self):
        result={}
        for cid in self.docker('ps','-aq','--filter','label='+LABEL+'='+self.owner).split():
            info=json.loads(self.docker('inspect','--size',cid))[0]
            verify_labels(info['Config'].get('Labels') or {},self.owner)
            result[cid]=info
        return result
    def runtime_disk(self,infos):
        logs={};volumes={};writable={}
        def size(path):return int(self.ssh(['sudo','-n','du','-sb','--',path]).stdout.split()[0])
        for cid,info in infos.items():
            writable[cid]=max(0,info.get('SizeRw') or 0)
            # Parent includes rotated logs plus small Docker metadata: conservative.
            if info.get('LogPath'):
                path=str(Path(info['LogPath']).parent)
                if path not in logs:logs[path]=size(path)
            for mount in info.get('Mounts',[]):
                if mount.get('Type')=='volume' and mount['Name'] not in volumes:
                    volumes[mount['Name']]={'source':mount['Source'],'bytes':size(mount['Source'])}
        return {'container_log_directories_bytes':logs,'writable_layer_bytes':writable,'anonymous_volumes':volumes,
                'extra_bytes':sum(logs.values())+sum(writable.values())+sum(v['bytes'] for v in volumes.values())}
    def status(self,allow_stopped=False):
        infos=self.inspect_owned()
        report={'resources':self.sample(),'containers':{cid:i['State'] for cid,i in infos.items()},'owner':self.owner,
                'host_configs':{cid:i['HostConfig'] for cid,i in infos.items()},'runtime_disk':self.runtime_disk(infos)}
        report['resources']['bind_data_bytes']=report['resources']['data_bytes']
        report['resources']['data_bytes']+=report['runtime_disk']['extra_bytes']
        if self.state.get('remote_created'):
            for filename in ('ready.json','state.json'):
                try:report[filename]=json.loads(self.ssh(['sudo','-n','cat',self.root+'/data/evidence/'+filename]).stdout)
                except subprocess.CalledProcessError:report[filename]=None
        r=report['resources'];report['stop_reasons']=[]
        if r['available_bytes']<2*2**30:report['stop_reasons'].append('memory reserve')
        if r['disk_free_bytes']<max(20*2**30,r['disk_total_bytes']//10):report['stop_reasons'].append('disk reserve')
        if r['data_bytes']>=4*2**30:report['stop_reasons'].append('4GiB data budget')
        if not allow_stopped:
            for cid,info in infos.items():
                if not info['State'].get('Running'):report['stop_reasons'].append('container not running: '+cid)
            actual={i['Name'].lstrip('/') for i in infos.values()}
            for name in self.state.get('containers',[]):
                if name not in actual:report['stop_reasons'].append('container missing: '+name)
        report['passed']=not report['stop_reasons']
        (self.path.parent/'status.json').write_text(json.dumps(report,indent=2));return report
    def collect(self,allow_stopped=False):
        target=self.path.parent/'collected';target.mkdir(exist_ok=True)
        for name in self.docker('ps','-aq','--filter','label='+LABEL+'='+self.owner).split():
            self.verify(name)
            logs=self.ssh(['sudo','-n','docker','logs',name])
            (target/(name+'.log')).write_bytes(logs.stdout+logs.stderr)
        if self.state.get('remote_created'):
            self.ssh(['sudo','-n','test','-f',self.root+'/owner-'+self.owner])
            archive=self.ssh(['sudo','-n','tar','-czf','-','-C',self.root+'/data','evidence'],timeout=180).stdout
            (target/'evidence.tar.gz').write_bytes(archive)
        return self.status(allow_stopped=allow_stopped)
    def cleanup(self):
        errors=[];exit_state={};volumes={};pid_verified=False
        before=self.inspect_owned()
        for cid,info in before.items():
            for mount in info.get('Mounts',[]):
                if mount.get('Type')=='volume':volumes[mount['Name']]={'container_id':cid,'source':mount['Source']}
        (self.path.parent/'cleanup-before.json').write_text(json.dumps(before,indent=2))
        # Stop app gracefully before collecting role receipts. Dependencies remain available.
        app=self.owner+'-app'
        ids=self.docker('ps','-aq','--filter','label='+LABEL+'='+self.owner).split()
        for cid in ids:
            self.verify(cid)
            if self.docker('inspect',cid,'--format','{{.Name}}')=='/'+app:
                try:
                    self.docker('stop','--time','65',cid)
                    exit_state=json.loads(self.docker('inspect',cid,'--format','{{json .State}}'))
                except Exception as exc:errors.append('app stop: '+type(exc).__name__)
        try:
            collected=self.collect(allow_stopped=True)
            if self.state.get('phase')=='starting':
                shutdown=shutdown_errors(exit_state,collected.get('ready.json') or {},collected.get('state.json') or {},self.state.get('receipt_pid_required',False))
                errors.extend(shutdown)
                pid_verified=self.state.get('receipt_pid_required',False) and not shutdown
            errors.extend(collected.get('stop_reasons',[]))
        except Exception as exc:errors.append('collect: '+type(exc).__name__)
        for cid in ids:
            try:self.verify(cid);self.docker('rm','-f','-v',cid)
            except Exception as exc:errors.append('container: '+type(exc).__name__)
        existing_volumes=set(self.docker('volume','ls','-q').split())
        volume_left=sorted(set(volumes)&existing_volumes)
        if volume_left:errors.append('Anonymous volumes remain after rm-v')
        networks=self.docker('network','ls','-q','--filter','label='+LABEL+'='+self.owner).split()
        for nid in networks:
            try:self.verify(nid,True);self.docker('network','rm',nid)
            except Exception as exc:errors.append('network: '+type(exc).__name__)
        remaining=self.docker('ps','-aq','--filter','label='+LABEL+'='+self.owner).split()
        netleft=self.docker('network','ls','-q','--filter','label='+LABEL+'='+self.owner).split()
        if not errors and not remaining and not netleft and self.state.get('remote_created'):
            self.ssh(['sudo','-n','test','-f',self.root+'/owner-'+self.owner]);self.ssh(['sudo','-n','rm','-rf','--',self.root])
        after=self.inventory();comparison=compare_business(self.state.get('business_before',{}),after);preserved=comparison['preserved']
        report={'errors':errors,'anonymous_volumes_before':volumes,'remaining_volumes':volume_left,'app_exit_state':exit_state,
                'receipt_pid_verified':pid_verified,
                'legacy_receipt_pid_unavailable':not self.state.get('receipt_pid_required',False),'remaining_containers':remaining,'remaining_networks':netleft,'business_preserved':preserved,'business_comparison':comparison,'passed':not errors and not remaining and not netleft and preserved}
        (self.path.parent/'cleanup.json').write_text(json.dumps(report,indent=2));self.state['cleaned']=report['passed'];self.save();return report


def provision(args):
    remote_target(getattr(args,'remote',None))
    if not args.runtime_image or not args.gateway_image:
        raise ValueError('Explicit runtime and gateway images are required')
    owner=args.owner or 'capacity-'+uuid.uuid4().hex[:12];root=validate_owner(owner)
    directory=args.output.resolve()/owner;directory.mkdir(parents=True,mode=0o700,exist_ok=False)
    path=directory/'state.json';state={'owner':owner,'host':remote_target(getattr(args,'remote',None)),'remote_root':root,'workers':args.workers,'capacity':args.capacity,
        'receipt_pid_required':True,'resource_limits':BUDGETS,'real_model_budget':0,'data_budget_bytes':4*2**30,'containers':[],'phase':'preflight'}
    remote=Remote(state,path);remote.save()
    disk=shutil.disk_usage(directory)
    assert disk.free-max(20*2**30,disk.total//10)>4*2**30,'Local snapshot reserve'
    resources=remote.sample();assert resources['available_bytes']>2*2**30+sum(v['memory_mib'] for v in BUDGETS.values())*2**20 and resources['disk_free_bytes']-max(20*2**30,resources['disk_total_bytes']//10)>4*2**30,'Remote reserve'
    images={key:remote.docker('image','inspect',value,'--format','{{.Id}}') for key,value in {'app':args.runtime_image,'pg':args.postgres_image,'redis':args.redis_image,'s3':args.s3_image}.items()}
    gateway_image=run_cmd(['docker','image','inspect',args.gateway_image,'--format','{{.Id}}']).stdout.decode().strip()
    state['gateway_image']=gateway_image
    state.update(images=images,business_before=remote.inventory());remote.save()
    source=directory/'source';source.mkdir();manifest=source_snapshot(source)
    (directory/'source.json').write_text(json.dumps(manifest,indent=2))
    extra=directory/'extra-gateway';extra.mkdir();copy=owner+'-deps'
    try:
        run_cmd(['docker','create','--name',copy,'--label',LABEL+'='+owner,'--entrypoint','true',gateway_image])
        run_cmd(['docker','cp',copy+':/usr/local/lib/python3.12/site-packages/.',str(extra)],timeout=180)
    finally:
        labels=json.loads(run_cmd(['docker','inspect',copy,'--format','{{json .Config.Labels}}']).stdout);verify_labels(labels,owner)
        run_cmd(['docker','rm','-v',copy])
    archive=directory/'provision.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:tar.add(source,arcname='source');tar.add(extra,arcname='extra-gateway')
    required=sum(p.stat().st_size for d in (source,extra) for p in d.rglob('*') if p.is_file())+4*2**30
    assert resources['disk_free_bytes']-max(20*2**30,resources['disk_total_bytes']//10)>required,'Remote source and data reserve'
    remote.ssh(['mkdir','-m','700',root]);state['remote_created']=True;remote.save()
    remote.ssh(['touch',root+'/owner-'+owner])
    with archive.open('rb') as stream:remote.ssh(['tar','-xzf','-','-C',root],stdin=stream,timeout=300)
    remote.ssh(['mkdir','-p',*[root+'/data/'+x for x in ('pg','redis','s3','s3-logs','shared','evidence')]])
    remote.ssh(['chmod','777',root+'/data/shared',root+'/data/evidence'])
    remote.docker('network','create','--internal','--label',LABEL+'='+owner,owner)
    env=environment(owner)
    def resource(role):
        v=limits(role);return ['--log-driver','json-file','--log-opt','max-size=10m','--log-opt','max-file=2','--cpus',str(v['cpus']),'--memory',str(v['memory_mib'])+'m','--memory-swap',str(v['memory_mib'])+'m','--pids-limit',str(v['pids'])]
    def envargs(values):return [x for k,v in values.items() for x in ('--env',k+'='+str(v))]
    def mount(folder,target,readonly=False):return ['--mount',f'type=bind,src={root}/{folder},dst={target}'+(',readonly' if readonly else '')]
    def launch(role,tail,*,mounted=(),variables=None,image=None):
        name=owner+'-'+role;state['containers'].append(name);remote.save()
        return remote.docker('run','-d','--init','--name',name,'--label',LABEL+'='+owner,'--network',owner,
            *resource(role),*mounted,*envargs(variables or {}),*(['--user','0'] if role=='s3' else []),image or images[role],*tail)
    launch('pg',['postgres','-c','shared_preload_libraries=pg_stat_statements'],mounted=mount('data/pg','/var/lib/postgresql'),variables={'POSTGRES_USER':'fixture','POSTGRES_PASSWORD':'fixture','POSTGRES_DB':'fixture'})
    launch('redis',['redis-server','--save','','--appendonly','yes','--appendfsync','everysec','--maxmemory','384mb','--maxmemory-policy','noeviction'],mounted=mount('data/redis','/data'))
    launch('s3',['/data'],mounted=mount('data/s3','/data')+mount('data/s3-logs','/logs'),variables={'RUSTFS_ACCESS_KEY':'capacity-fixture','RUSTFS_SECRET_KEY':'capacity-fixture-secret','RUSTFS_CONSOLE_ENABLE':'false'})
    deadline=time.monotonic()+60
    while True:
        try:remote.docker('exec',owner+'-pg','pg_isready','-U','fixture');break
        except subprocess.CalledProcessError:
            if time.monotonic()>deadline:raise TimeoutError('PG startup')
            time.sleep(.5)
    mounts=mount('source','/source',True)+mount('source/scripts/reliability/capacity_support','/capacity',True)+mount('source/scripts/reliability/load_support','/support',True)+mount('extra-gateway','/extra-gateway',True)+mount('data/shared','/shared')+mount('data/evidence','/evidence')
    app=owner+'-app';state['containers'].append(app);remote.save()
    remote.docker('run','-d','--init','--name',app,'--label',LABEL+'='+owner,'--network',owner,*resource('app'),*mounts,*envargs(env),
        '--cap-add','SYS_ADMIN','--security-opt','seccomp=unconfined','--security-opt','apparmor=unconfined','--entrypoint','sleep',images['app'],'infinity')
    remote.docker('exec','--workdir','/source/backend',app,'python','-m','alembic','upgrade','head')
    remote.docker('exec',owner+'-pg','psql','-U','fixture','-d','fixture','-v','ON_ERROR_STOP=1','-c','CREATE EXTENSION IF NOT EXISTS pg_stat_statements')
    remote.docker('exec',app,'python','-c',"import boto3,time; s=boto3.client('s3',endpoint_url='http://"+owner+"-s3:9000',aws_access_key_id='capacity-fixture',aws_secret_access_key='capacity-fixture-secret'); time.sleep(3); [s.create_bucket(Bucket=b) for b in ('capacity-artifacts','capacity-skills','capacity-knowledge')]")
    # Bootstrap and SUT use the same budget sequentially. --init forwards stop.
    remote.verify(app);remote.docker('rm','-f','-v',app)
    remote.docker('run','-d','--init','--name',app,'--label',LABEL+'='+owner,'--network',owner,*resource('app'),*mounts,*envargs(env),
        '--cap-add','SYS_ADMIN','--security-opt','seccomp=unconfined','--security-opt','apparmor=unconfined','--entrypoint','python',images['app'],
        '/capacity/sut.py','--workers',str(args.workers),'--capacity',str(args.capacity),'--seed')
    state.update(phase='starting',app_ip=remote.docker('inspect',app,'--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'))
    state['tunnel_argv']=tunnel(state['app_ip'],args.local_port,remote=state['host']);state['tunnel_command']=shlex.join(state['tunnel_argv']);state['base_url']='http://127.0.0.1:'+str(args.local_port);remote.save()
    state['ips']={role:remote.docker('inspect',owner+'-'+role,'--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}') for role in BUDGETS}
    remote.save()
    return state


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('provision','status','collect','cleanup'))
    parser.add_argument('--state',type=Path);parser.add_argument('--output',type=Path,default=ROOT/'output/validation/capacity')
    parser.add_argument('--owner');parser.add_argument('--workers',type=int,choices=(1,2),default=1);parser.add_argument('--capacity',type=int,choices=(2,4),default=4)
    parser.add_argument('--local-port',type=int,default=18080);parser.add_argument('--postgres-image',default='postgres:18-alpine');parser.add_argument('--redis-image',default='redis:7-alpine');parser.add_argument('--s3-image',default='rustfs/rustfs:1.0.0-rc.6');parser.add_argument('--run',action='store_true')
    parser.add_argument('--remote', help='SSH target; alternatively set WORKMATE_TEST_SSH_TARGET')
    parser.add_argument('--runtime-image', help='Existing remote Runtime image')
    parser.add_argument('--gateway-image', help='Existing local Gateway dependency image')
    args=parser.parse_args(argv)
    if args.run:
        try: remote_target(args.remote)
        except ValueError as error: parser.error(str(error))
        if args.action=='provision' and (not args.runtime_image or not args.gateway_image):
            parser.error('--runtime-image and --gateway-image are required for provisioning')
    if args.owner:validate_owner(args.owner)
    if args.action!='provision' and not args.state:parser.error('--state required')
    if not args.run:print(json.dumps({'action':args.action,'run':False,'resource_limits':BUDGETS,'host':args.remote}));return
    if args.action=='provision':
        args.owner=args.owner or 'capacity-'+uuid.uuid4().hex[:12]
        try:result=provision(args)
        except Exception as exc:
            directory=args.output.resolve()/args.owner
            if directory.is_dir():
                error={'error_type':type(exc).__name__,'detail':str(exc)}
                if isinstance(exc,subprocess.CalledProcessError):
                    error.update(stdout=(exc.stdout or b'').decode(errors='replace'),stderr=(exc.stderr or b'').decode(errors='replace'))
                (directory/'provision-error.json').write_text(json.dumps(error,indent=2))
            print(json.dumps({'passed':False,'error_type':type(exc).__name__,'evidence':str(directory)}))
            raise SystemExit(1) from None
    else:
        state=json.loads(args.state.read_text());assert state['host']==remote_target(args.remote), 'Explicit target does not match recorded fixture host'
        remote=Remote(state,args.state);result=getattr(remote,args.action)()
    print(json.dumps(result,indent=2))
    if result.get('passed') is False:raise SystemExit(1)


if __name__=='__main__':main()
