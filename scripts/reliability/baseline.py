"""Read-only, bounded two-host sampling; no remote writes or Docker mutations."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

# Only /proc, filesystem statistics, and an unauthenticated loopback health probe.
PROBE = r'''
import json,os,time,urllib.request
from pathlib import Path
m={k:int(v.strip().split()[0])*1024 for k,v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())}
s=os.statvfs('/')
r={'time_unix':time.time(),'available_bytes':m['MemAvailable'],'total_memory_bytes':m['MemTotal'],'disk_free_bytes':s.f_bavail*s.f_frsize,'disk_total_bytes':s.f_blocks*s.f_frsize,'load1':os.getloadavg()[0],'cpu_ticks':Path('/proc/stat').read_text().splitlines()[0],'diskstats':Path('/proc/diskstats').read_text(),'vmstat':{k:int(v) for k,v in (line.split() for line in Path('/proc/vmstat').read_text().splitlines()) if k in ('pswpin','pswpout','oom_kill')}}
try:
 with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=5) as x:
  r['health_status']=x.status
except Exception as e:r['health_status']=0;r['health_error']=type(e).__name__
print(json.dumps(r))
'''


def disk_budget(free, image_increment, data_growth, reserve):
    """Bytes, evaluated per host; budget is planning evidence, not an enforced quota."""
    if any(type(v) is not int or v < 0 for v in (free,image_increment,data_growth,reserve)):
        raise ValueError('Disk quantities must be nonnegative integer bytes')
    required=image_increment+data_growth+reserve
    return {'passed':free>=required,'free_bytes':free,'required_bytes':required,
            'headroom_bytes':free-required}


def summarize(rows, duration, interval, hosts):
    errors=[]
    if not rows or rows[0]['elapsed_seconds'] > interval:
        errors.append('missing_initial_sample')
    if not rows or rows[-1]['elapsed_seconds'] < duration:
        errors.append('incomplete_duration')
    for left,right in zip(rows,rows[1:]):
        gap=right['elapsed_seconds']-left['elapsed_seconds']
        if gap<=0 or gap>interval+30:
            errors.append('sampling_gap')
    summaries={}
    for host in hosts:
        valid=[]
        for row in rows:
            value=row.get('hosts',{}).get(host,{})
            if 'error' in value or any(k not in value for k in ('health_status','available_bytes','disk_free_bytes','load1')):
                errors.append(host+':missing_or_failed_sample')
                continue
            valid.append(value)
            if value['health_status']!=200:errors.append(host+':health_failed')
            if value['available_bytes']<2*2**30:errors.append(host+':low_memory')
            if value['disk_free_bytes']<20*2**30:errors.append(host+':low_disk')
        if valid:
            summaries[host]={'minimum_available_gib':min(x['available_bytes'] for x in valid)/2**30,
                'minimum_free_disk_gib':min(x['disk_free_bytes'] for x in valid)/2**30,
                'maximum_load1':max(x['load1'] for x in valid)}
    return {'passed':not errors,'requested_seconds':duration,'samples':len(rows),
            'observed_seconds':rows[-1]['elapsed_seconds'] if rows else 0,
            'errors':sorted(set(errors)),'hosts':summaries}


def probe(remote=None):
    command=([sys.executable,'-'] if remote is None else
        ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',remote,'python3 -'])
    try:
        result=subprocess.run(command,input=PROBE,capture_output=True,text=True,timeout=20,check=True)
        return json.loads(result.stdout)
    except (subprocess.SubprocessError,OSError,ValueError) as error:
        return {'error':type(error).__name__}  # Never persist arbitrary SSH stderr.


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--remote', help='Optional SSH target; omit for local-only sampling')
    parser.add_argument('--duration-seconds',type=int,default=3600)
    parser.add_argument('--interval-seconds',type=int,default=60)
    parser.add_argument('--run',action='store_true')
    args=parser.parse_args(argv)
    if args.remote and not re.fullmatch(r'[a-zA-Z0-9_.]+@[a-zA-Z0-9_.-]+',args.remote):
        parser.error('Expected user@host without SSH options')
    if not 1<=args.duration_seconds<=86400 or not 1<=args.interval_seconds<=60:
        parser.error('Invalid bounded duration or interval')
    if not args.run:
        print(json.dumps({'run':False,'seconds':args.duration_seconds,'remote':args.remote}))
        return 0
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False)
    started=time.monotonic();rows=[]
    try:
        while True:
            elapsed=time.monotonic()-started
            row={'elapsed_seconds':elapsed,'hosts':{'local':probe(),**({'remote':probe(args.remote)} if args.remote else {})}}
            rows.append(row)
            with (args.output/'samples.jsonl').open('a') as stream:
                stream.write(json.dumps(row)+'\n')
            (args.output/'progress.json').write_text(json.dumps(summarize(rows,args.duration_seconds,args.interval_seconds,(('local','remote') if args.remote else ('local',))),indent=2))
            if elapsed>=args.duration_seconds:break
            # No individual sleep exceeds 60s. No writes occur on the remote host.
            next_at=min(args.duration_seconds,len(rows)*args.interval_seconds)
            time.sleep(max(0,min(60,next_at-(time.monotonic()-started))))
    finally:
        result=summarize(rows,args.duration_seconds,args.interval_seconds,(('local','remote') if args.remote else ('local',)))
        (args.output/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
