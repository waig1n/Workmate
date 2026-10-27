"""Read-only CAPACITY host/resource sampling and independent side-effect evidence join."""
from scripts.reliability.targets import remote_target

import argparse
import os
import errno
import stat
from concurrent.futures import ThreadPoolExecutor
import json
import inspect
from pathlib import Path
import re
import subprocess
import time

from .baseline import PROBE


def safe_stderr(value):
    if isinstance(value,bytes):value=value.decode('utf-8','replace')
    value=value or ''
    value=re.sub(r'(?i)(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)([^\s]+)',r'\1\2[REDACTED]',value)
    value=re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+:[^\s/@]+@',r'\1[REDACTED]@',value)
    value=re.sub(r'(?i)Bearer\s+\S+','Bearer [REDACTED]',value)
    value=''.join(c if c in '\n\t' or ord(c)>=32 else '?' for c in value)
    return value if len(value)<=800 else value[:200]+'\n...[truncated]...\n'+value[-580:]


def diagnostic(error,stage,category):
    return dict(stage=stage,command_category=category,error_type=type(error).__name__,
                returncode=getattr(error,'returncode',None),stderr_summary=safe_stderr(getattr(error,'stderr','')))


class SamplingCommandError(RuntimeError):
    def __init__(self,error,stage,category):
        self.detail=diagnostic(error,stage,category)
        super().__init__(category)


def sampling_output(command,stage,category):
    started=time.monotonic()
    try:
        return subprocess.run(command,text=True,capture_output=True,timeout=15,check=True).stdout
    except (subprocess.SubprocessError,OSError) as error:
        failure=SamplingCommandError(error,stage,category)
        failure.detail['elapsed_seconds']=time.monotonic()-started
        raise failure from error


def atomic_json(path,value):
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,indent=2))
    temporary.replace(path)


def apparent_tree_usage(root,optional=False):
    """Read apparent bytes without following symlinks; count vanished children.

    A sample spans a changing tree, not a filesystem snapshot. Required root
    absence/replacement and permission/I/O errors remain measurement failures.
    """
    root=os.path.abspath(root)
    try:initial=os.stat(root,follow_symlinks=False)
    except FileNotFoundError:
        if optional:return dict(total_bytes=0,disappeared_entries=0,present=False)
        raise
    if not stat.S_ISDIR(initial.st_mode):raise ValueError('Measurement root must be a real directory')
    seen=set();total=0;disappeared=0
    def add(info):
        nonlocal total
        identity=(info.st_dev,info.st_ino)
        if identity not in seen:
            seen.add(identity);total+=info.st_size
    def walk_error(error):
        nonlocal disappeared
        name=error.filename
        descendant=(isinstance(name,str) and name not in ('','.','..',root) and
                    (os.path.commonpath((root,os.path.abspath(name)))==root if os.path.isabs(name)
                     else os.path.basename(name)==name))
        if error.errno==errno.ENOENT and descendant:disappeared+=1
        else:raise error
    for directory,dirs,files,fd in os.fwalk(root,follow_symlinks=False,onerror=walk_error):
        add(os.fstat(fd))
        for name in dirs+files:
            try:info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            except OSError as error:
                if error.errno==errno.ENOENT:
                    disappeared+=1;continue
                raise
            add(info)
    final=os.stat(root,follow_symlinks=False)
    if (initial.st_dev,initial.st_ino)!=(final.st_dev,final.st_ino):raise ValueError('Measurement root changed during scan')
    return dict(total_bytes=total,disappeared_entries=disappeared,present=True)


def owned_usage(rows,root,measure):
    """Count dynamic owner directories, writable layers, logs and volumes once."""
    paths={str(Path(root)/name) for name in ('data','evidence','shared')}
    writable=0
    for row in rows:
        size=row.get('SizeRw')
        if type(size) is not int or size<0:
            raise ValueError('Missing container writable-layer measurement')
        writable+=size
        if row.get('LogPath'): paths.add(str(Path(row['LogPath']).parent))
        for mount in row.get('Mounts',[]):
            if mount.get('Type')=='volume': paths.add(mount['Source'])
    selected=[]
    for path in sorted(paths,key=lambda item:len(Path(item).parts)):
        if not any(Path(parent)==Path(path) or Path(parent) in Path(path).parents for parent in selected):selected.append(path)
    measurements={path:measure(path) for path in selected}
    measured={path:(value['total_bytes'] if isinstance(value,dict) else value) for path,value in measurements.items()}
    return {'paths_bytes':measured,'path_measurements':measurements,'writable_bytes':writable,'total_bytes':sum(measured.values())+writable}


def violations(sample):
    reasons=[]
    for host,row in sample['hosts'].items():
        if 'error' in row or row.get('sample_errors'):
            reasons.append(host+':sample_error')
        if not all(key in row for key in ('available_bytes','disk_free_bytes','disk_total_bytes','health_status')):
            if host+':sample_error' not in reasons:reasons.append(host+':sample_error')
            continue
        if row['available_bytes']<2*2**30: reasons.append(host+':memory_floor')
        if row['disk_free_bytes']<max(20*2**30,row['disk_total_bytes']//10): reasons.append(host+':disk_floor')
        if row['health_status']!=200: reasons.append(host+':business_health')
        if row.get('owned_data_bytes',0)>4*2**30: reasons.append(host+':data_cap')
        if any(c.get('oom_killed') for c in row.get('containers',[])): reasons.append(host+':container_oom')
        if any(c.get('status','running')!='running' for c in row.get('containers',[])): reasons.append(host+':container_not_running')
        if any(c.get('exit_code',0)!=0 for c in row.get('containers',[])): reasons.append(host+':container_exit')
        if row.get('owned_containers_expected') and not row.get('containers'): reasons.append(host+':containers_missing')
        if row.get('expected_container_names'):
            actual=[c.get('name','').lstrip('/') for c in row.get('containers',[])]
            if sorted(actual)!=sorted(row['expected_container_names']):reasons.append(host+':container_inventory_mismatch')
    return reasons


def probe(owner,remote):
    if not re.fullmatch(r'capacity-[0-9a-f]{12}',owner): raise ValueError('Invalid isolated owner')
    source=PROBE.replace('print(json.dumps(r))',
        "r['network_interfaces']=Path('/proc/net/dev').read_text()\nprint(json.dumps(r))")
    if remote:
        source+='\nimport subprocess,re\n'
        scanner='import os,stat,errno,json,sys\n'+inspect.getsource(apparent_tree_usage)+"\nprint(json.dumps(apparent_tree_usage(sys.argv[1],optional=sys.argv[2]=='optional')))"
        source+='SCANNER='+repr(scanner)+'\n'
        for helper in (safe_stderr,diagnostic,SamplingCommandError,sampling_output,owned_usage):
            source+=inspect.getsource(helper)+'\n'
        source+="""
owner=OWNER
d=['sudo','-n','docker']
stage='container_inventory'
try:
 ids=sampling_output(d+['ps','-aq','--filter','label=workmate.capacity.owner='+owner],stage,'docker_inventory').split()
 stage='container_inspection'
 rows=json.loads(sampling_output(d+['inspect','--size',*ids],stage,'docker_inspect')) if ids else []
 print(json.dumps({'owned_containers_expected':True,'expected_container_names':[owner+'-'+role for role in ('app','pg','redis','s3')],'containers':[{'name':r['Name'],'status':r['State']['Status'],'exit_code':r['State']['ExitCode'],'oom_killed':r['State']['OOMKilled'],'restart_count':r['RestartCount']} for r in rows]}),flush=True)
 def measure(path):
  optional=path in ('/tmp/'+owner+'/evidence','/tmp/'+owner+'/shared')
  value=json.loads(sampling_output(['sudo','-n','python3','-c',SCANNER,path,'optional' if optional else 'required'],'owned_usage','apparent_tree_scan'))
  if not isinstance(value.get('total_bytes'),int) or value['total_bytes']<0:raise ValueError('Invalid scanner size')
  return value
 stage='owned_usage'
 usage=owned_usage(rows,'/tmp/'+owner,measure)
 print(json.dumps({'owned_data_bytes':usage['total_bytes'],'owned_usage':usage}),flush=True)
 stage='container_stats'
 stats=sampling_output(d+['stats','--no-stream','--format','{{json .}}',*ids],stage,'docker_stats') if ids else ''
 print(json.dumps({'stats':stats}),flush=True)
except Exception as error:
 detail=error.detail if isinstance(error,SamplingCommandError) else diagnostic(error,stage,'parse_or_measure')
 print(json.dumps({'error':type(error).__name__,'sample_errors':[detail]}),flush=True)
""".replace('OWNER',repr(owner))
    source=source.replace('print(json.dumps(r))','print(json.dumps(r),flush=True)')
    command=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',remote_target(),'python3','-'] if remote else ['python3','-']
    started=time.monotonic();row={};errors=[];output=''
    try:
        result=subprocess.run(command,input=source,text=True,capture_output=True,timeout=25,check=True)
        output=result.stdout
    except (subprocess.SubprocessError,OSError) as error:
        output=getattr(error,'stdout',None) or getattr(error,'output',None) or ''
        errors.append(diagnostic(error,'remote_transport' if remote else 'local_probe','ssh' if remote else 'python_probe'))
    if isinstance(output,bytes):output=output.decode('utf-8','replace')
    parsed=0
    for line in output.splitlines():
        try:
            value=json.loads(line)
            if not isinstance(value,dict):raise ValueError('Expected sample object')
            details=value.pop('sample_errors',[])
            if not isinstance(details,list) or any(not isinstance(item,dict) or not isinstance(item.get('error_type'),str) for item in details):raise ValueError('Invalid error records')
            errors.extend(details);row.update(value);parsed+=1
        except (ValueError,TypeError) as error:errors.append(diagnostic(error,'sample_decode','json_line'))
    # A complete remote sample includes base host, container list, usage and stats.
    if parsed<(4 if remote else 1) and not errors:
        errors.append(dict(stage='sample_decode',command_category='missing_rows',error_type='IncompleteSample',returncode=None,stderr_summary=''))
    required_host=all(type(row.get(key)) is int for key in ('available_bytes','disk_free_bytes','disk_total_bytes','health_status'))
    required_owned=(type(row.get('owned_data_bytes')) is int and row.get('owned_data_bytes',-1)>=0
                    and isinstance(row.get('owned_usage'),dict) and isinstance(row.get('containers'),list)
                    and isinstance(row.get('stats'),str) and isinstance(row.get('expected_container_names'),list))
    if not errors and (not required_host or (remote and not required_owned)):
        errors.append(dict(stage='sample_decode',command_category='missing_fields',error_type='IncompleteSample',returncode=None,stderr_summary='Required host/container/usage/stats fields missing or invalid'))
    if errors:
        row['sample_errors']=errors;row['error']=errors[0]['error_type']
    row['probe_roundtrip_seconds']=time.monotonic()-started
    return row


def join_effects(report,evidence):
    """Do not promote pending fixture checks without independent tool evidence."""
    attempts=[]
    for path in Path(evidence).glob('write-effects-*.jsonl'):
        attempts.extend(json.loads(line) for line in path.read_text().splitlines())
    stops=[]
    for path in Path(evidence).glob('stop-effects-*.jsonl'):
        stops.extend(json.loads(line) for line in path.read_text().splitlines())
    results=[]
    for run in report.get('runs',[]):
        for task in run.get('tasks',[]):
            if task.get('workload') not in ('file','file20','boundary','approval','skill','stop'):continue
            key=task['key']
            count=sum(row.get('file_path','').endswith('/'+key+'.txt') for row in attempts)
            result={'key':key,'task_id':task.get('id'),'attempts':count,'passed':count==1}
            if task.get('workload')=='stop':
                effects=[row for row in stops if row.get('key')==key]
                result['stop_effects']=effects
                result['passed']=result['passed'] and len(effects)==1 and effects[0].get('started_count')==1 and effects[0].get('completed_exists') is False and effects[0].get('result_exists') is False
            results.append(result)
    return {'passed':all(row['passed'] for row in results),'results':results}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--duration',type=int,default=3600)
    args=parser.parse_args(argv)
    if not 1<=args.duration<=86400:parser.error('Bounded duration required')
    args.output.mkdir(parents=True,exist_ok=True)
    started=time.monotonic(); failures=[]; initial_oom={}
    with (args.output/'host-samples.jsonl').open('a') as trace,ThreadPoolExecutor(max_workers=2) as pool:
        while time.monotonic()-started<args.duration and not (args.output/'monitor.stop').exists():
            futures={name:pool.submit(probe,args.owner,remote) for name,remote in [('local',False),('remote',True)]}
            hosts={}
            for name,future in futures.items():
                try:
                    hosts[name]=future.result()
                    if not isinstance(hosts[name],dict):raise ValueError('Host probe must return object')
                except Exception as error:hosts[name]={'error':type(error).__name__,'sample_errors':[diagnostic(error,'probe_execution','python_probe')]}
            sample={'elapsed_seconds':time.monotonic()-started,'hosts':hosts}
            try:errors=violations(sample)
            except (TypeError,KeyError,ValueError) as error:
                errors=['monitor:invalid_sample']
                sample['sample_errors']=[diagnostic(error,'validation','sample_schema')]
            for name,row in sample['hosts'].items():
                vmstat=row.get('vmstat',{})
                if not isinstance(vmstat,dict):
                    errors.append(name+':invalid_vmstat');continue
                oom=vmstat.get('oom_kill')
                if oom is not None and type(oom) is not int:
                    errors.append(name+':invalid_oom_counter');continue
                if oom is not None:
                    initial_oom.setdefault(name,oom)
                    if oom>initial_oom[name]:errors.append(name+':host_oom')
            sample['violations']=errors;trace.write(json.dumps(sample)+'\n');trace.flush()
            if errors:
                failures.extend(errors)
                atomic_json(args.output/'STOP.json',{'reasons':errors,'sample':sample})
                break
            time.sleep(10)
    atomic_json(args.output/'monitor-summary.json',{'passed':not failures,'failures':failures,'elapsed_seconds':time.monotonic()-started})
    return 1 if failures else 0


if __name__=='__main__':raise SystemExit(main())
