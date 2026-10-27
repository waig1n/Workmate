"""SOAK read-only observation. Never inspect Python task, coroutine, or greenlet state."""
from scripts.reliability.targets import remote_target

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from scripts.reliability.resource_monitor import probe,violations,apparent_tree_usage,diagnostic


# SOAK's pre-run frozen retention budget; the CAPACITY acceptance budget remains unchanged.
SOAK_DATA_CAP_BYTES=8*2**30


def soak_violations(sample):
    errors=[error for error in violations(sample) if not error.endswith(':data_cap')]
    errors.extend(host+':data_cap' for host,row in sample['hosts'].items()
        if row.get('owned_data_bytes',0)>SOAK_DATA_CAP_BYTES)
    return errors


def swap_errors(sample,previous,state):
    errors=[]
    for host,row in sample['hosts'].items():
        old=previous['hosts'][host]
        delta=row.get('swap_pages_delta',{})
        ongoing=(sum(delta.values())>0 and row.get('available_bytes',0)<old.get('available_bytes',0))
        if not ongoing:
            state.pop(host,None);continue
        state.setdefault(host,previous['time_unix'])
        if sample['time_unix']-state[host]>=60:errors.append(host+':sustained_swap_memory_decline')
    return errors


def atomic_json(path,value):
    path=Path(path);temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2));os.replace(temporary,path)


def loopback(value,schemes):
    parsed=urlsplit(value)
    if parsed.scheme not in schemes or parsed.hostname not in ('127.0.0.1','::1','localhost'):
        raise ValueError('Observer endpoints must be loopback URLs')
    return value


def parse_stat(text,page_size):
    prefix,fields=text.rsplit(') ',1);items=fields.split()
    return dict(pid=int(prefix.split(' ',1)[0]),ppid=int(items[1]),state=items[0],
        cpu_ticks=int(items[11])+int(items[12]),start_ticks=int(items[19]),rss_bytes=int(items[21])*page_size)


def process_snapshot():
    """Read kernel files only. Vanished transient descendants are recorded explicitly."""
    ready=json.loads(Path('/evidence/ready.json').read_text())
    roles={**ready['children'],'supervisor':ready['pid']};rows={};vanished=[]
    for folder in Path('/proc').iterdir():
        if not folder.name.isdigit():continue
        try:
            row=parse_stat((folder/'stat').read_text(),os.sysconf('SC_PAGE_SIZE'))
            row['fd_count']=len(list((folder/'fd').iterdir()))
            after=parse_stat((folder/'stat').read_text(),os.sysconf('SC_PAGE_SIZE'))
            if (row['pid'],row['start_ticks'])!=(after['pid'],after['start_ticks']):
                raise ValueError('Process identity changed during measurement')
            rows[row['pid']]=row
        except (FileNotFoundError,ProcessLookupError):vanished.append(int(folder.name))
    selected={}
    for name,pid in roles.items():
        if pid not in rows:raise ValueError('Fixed role missing')
        selected[name]=rows[pid]
    descendants=set(roles.values())
    while True:
        expanded=descendants|{pid for pid,row in rows.items() if row['ppid'] in descendants}
        if expanded==descendants:break
        descendants=expanded
    # Orphan candidates include reparented sandbox processes; exclude this observer and init.
    observers={os.getpid(),os.getppid(),1}
    orphans=[row for pid,row in rows.items() if pid not in descendants|observers]
    return {'roles':selected,'descendants':[rows[pid] for pid in descendants if pid not in roles.values()],
        'orphan_candidates':orphans,'vanished_pids':vanished,'clock_ticks_per_second':os.sysconf('SC_CLK_TCK')}


def processes(owner):
    source='import json,os\nfrom pathlib import Path\n'+inspect.getsource(parse_stat)+'\n'+inspect.getsource(process_snapshot)+'\nprint(json.dumps(process_snapshot()))\n'
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',remote_target(),
        'sudo','-n','docker','exec','-i',owner+'-app','python','-'],input=source,text=True,capture_output=True,timeout=20,check=True)
    return json.loads(result.stdout)


async def database_metrics(dsn,redis_url,namespace):
    import asyncpg
    import redis.asyncio as redis
    conn=await asyncpg.connect(dsn,timeout=5,command_timeout=5)
    try:
        async with conn.transaction(readonly=True):
            pg=dict(await conn.fetchrow("SELECT (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()) AS connections, (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock') AS lock_waits, pg_database_size(current_database()) AS database_bytes, pg_wal_lsn_diff(pg_current_wal_lsn(),'0/0')::bigint AS wal_bytes"))
            pg['waits']=[dict(row) for row in await conn.fetch("SELECT coalesce(wait_event_type,'none') AS type,count(*) AS count FROM pg_stat_activity WHERE datname=current_database() GROUP BY wait_event_type")]
            pg['active_tasks']=await conn.fetchval("SELECT count(*) FROM tasks WHERE status IN ('created','running','awaiting_permission','cancelling')")
            pg['outboxes']={}
            for table in ('command_deliveries','realtime_business_outbox'):
                row=await conn.fetchrow(f"SELECT count(*) AS count,coalesce(extract(epoch FROM now()-min(created_at)),0)::float8 AS oldest_age_seconds FROM {table} WHERE status IN ('pending','publishing')")
                pg['outboxes'][table]=dict(row)
    finally:await conn.close()
    client=redis.from_url(redis_url,decode_responses=True,socket_timeout=5,socket_connect_timeout=5)
    try:
        memory=await client.info('memory');streams=[];prefix='workmate:'+namespace+':'
        async for key in client.scan_iter(match=prefix+'*',count=100):
            if not key.startswith(prefix):raise ValueError('Foreign namespace')
            if await client.type(key)!='stream':continue
            groups=await client.xinfo_groups(key)
            streams.append({'key':key,'length':await client.xlen(key),'groups':[
                {'pending':int(group['pending']),'lag':group.get('lag'),'consumers':int(group['consumers'])} for group in groups]})
        return {'pg':pg,'redis':{'used_memory':int(memory['used_memory']),'streams':streams,
            'pending':sum(g['pending'] for s in streams for g in s['groups']),
            'lag':sum(g['lag'] or 0 for s in streams for g in s['groups']),
            'unknown_lag_groups':sum(g['lag'] is None for s in streams for g in s['groups'])}}
    finally:await client.aclose()


def components(owner):
    # Same race-aware no-symlink scanner; allocated blocks are reported separately,
    # never added to the existing apparent-byte 4 GiB total a second time.
    scanner=inspect.getsource(apparent_tree_usage).replace('total+=info.st_size','total+=info.st_blocks*512')
    source='import os,stat,errno,json,time\n'+scanner
    source+='\nroot='+repr('/tmp/'+owner+'/data')+'\n'
    source+="result={name:apparent_tree_usage(root+'/'+name,optional=(name=='shared/workspaces')) for name in ('pg','redis','s3','s3-logs','shared','shared/workspaces','evidence')}\n"
    source+="print(json.dumps({'measured_at':time.time(),'measurement':'allocated_bytes','directories':result}))\n"
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',remote_target(),'sudo','-n','python3','-'],
        input=source,text=True,capture_output=True,timeout=20,check=True)
    return json.loads(result.stdout)


def role_errors(sample,identities):
    current=sample.get('processes',{}).get('roles')
    if not current:return ['process_sample_missing']
    return ['role_identity_changed:'+name for name,old in identities.items()
        if name not in current or any(current[name][key]!=old[key] for key in ('pid','start_ticks'))]


def add_deltas(sample,previous):
    elapsed=sample['time_unix']-previous['time_unix']
    if elapsed<=0:raise ValueError('Nonpositive sampling interval')
    for host,row in sample['hosts'].items():
        old=previous['hosts'][host]
        before=[int(x) for x in old.get('cpu_ticks','cpu').split()[1:]]
        after=[int(x) for x in row.get('cpu_ticks','cpu').split()[1:]]
        if len(before)>=4 and len(after)==len(before):
            # guest ticks are already included in user/nice: use first eight.
            total=sum(after[:8])-sum(before[:8]);idle=after[3]-before[3]
            if len(after)>4:idle+=after[4]-before[4]
            row['cpu_busy_fraction']=(total-idle)/total if total>0 else 0.0
    clock=sample['processes'].get('clock_ticks_per_second')
    for name,row in sample['processes'].get('roles',{}).items():
        old=previous['processes'].get('roles',{}).get(name)
        if clock and old and (row['pid'],row['start_ticks'])==(old['pid'],old['start_ticks']):
            row['cpu_cores']=(row['cpu_ticks']-old['cpu_ticks'])/clock/elapsed


def guarded(function,*args,stage="sample",category="read_only_probe"):
    try:return function(*args)
    except Exception as error:
        return {'error':type(error).__name__,'sample_errors':[diagnostic(error,stage,category)]}


def collect(owner,dsn,redis_url,component_cache=None):
    with ThreadPoolExecutor(max_workers=5) as pool:
        component=pool.submit(guarded,components,owner,stage="components",category="ssh_allocated_directory_scan") if component_cache is None else None
        futures=[pool.submit(guarded,probe,owner,remote,stage="host_remote" if remote else "host_local",category="host_resources") for remote in (False,True)]
        process=pool.submit(guarded,processes,owner,stage="processes",category="docker_exec_proc")
        database=pool.submit(guarded,lambda:asyncio.run(asyncio.wait_for(database_metrics(dsn,redis_url,owner),20)),stage="database",category="postgres_redis_metrics")
        result={'hosts':dict(zip(('local','remote'),[f.result() for f in futures])),
            'processes':process.result(),'database':database.result()}
        result['components']=component.result() if component else component_cache
        result['postgres']=result['database'].get('pg',{})
        result['redis']=result['database'].get('redis',{})
        proc=result['processes']
        if 'roles' in proc:
            def role(row):
                children=[];pids={row['pid']}
                while True:
                    expanded=pids|{item['pid'] for item in proc['descendants'] if item['ppid'] in pids}
                    if expanded==pids:break
                    pids=expanded
                children=[item for item in proc['descendants'] if item['pid'] in pids]
                return {**row,'fds':row['fd_count'],'descendants':children}
            proc['gateway']=role(proc['roles']['gateway'])
            proc['workers']=[dict(role(row),name=name) for name,row in proc['roles'].items() if name.startswith('worker-')]
        return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('owner','dsn','redis-url','output'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--duration',type=int,required=True)
    parser.add_argument('--interval',type=float,default=10)
    args=parser.parse_args(argv)
    if not re.fullmatch(r'capacity-[0-9a-f]{12}',args.owner):parser.error('Invalid dedicated owner')
    if not 1<=args.duration<=110000 or not 1<=args.interval<=30:parser.error('Invalid bounded duration/interval')
    loopback(args.dsn,{'postgresql','postgres'});loopback(args.redis_url,{'redis','rediss'})
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();identities={};failures=[];index=0;previous=None;component_cache=None;component_time=0;deadline=started;swap_state={}
    try:
        while time.monotonic()-started<args.duration and not (output/'monitor.stop').exists():
            cycle=time.monotonic();phase=json.loads((output/'phase.json').read_text())['phase']
            if phase not in ('warmup','warm_idle','prerun','cooldown','formal','load'):raise ValueError('Unknown observation phase')
            sample=collect(args.owner,args.dsn,args.redis_url,component_cache if cycle-component_time<60 else None);index+=1
            if sample.get('components') is not component_cache:
                component_cache=sample.get('components');component_time=cycle
            sample.update(time_unix=time.time(),elapsed_seconds=time.monotonic()-started,phase=phase,sample_index=index)
            errors=soak_violations(sample)
            if not sample.get('components') or 'error' in sample['components']:errors.append('component_sample_missing')
            if 'error' in sample['database'] or not all(k in sample['database'] for k in ('pg','redis')):errors.append('database_sample_missing')
            errors+=role_errors(sample,identities)
            if not identities and not role_errors(sample,{}):identities=sample['processes']['roles']
            if time.monotonic()-cycle>30:errors.append('sampling_cycle_over_30s')
            if previous:
                add_deltas(sample,previous)
                for host in ('local','remote'):
                    old=previous['hosts'][host].get('vmstat',{});new=sample['hosts'][host].get('vmstat',{})
                    if new.get('oom_kill',0)>old.get('oom_kill',0):errors.append(host+':host_oom')
                    sample['hosts'][host]['swap_pages_delta']={key:new.get(key,0)-old.get(key,0) for key in ('pswpin','pswpout')}
                errors+=swap_errors(sample,previous,swap_state)
            sample['orphan_review']={'hour':int(sample['elapsed_seconds']//3600),'candidate_count':len(sample['processes'].get('orphan_candidates',[])),
                'interpretation':'Kernel namespace processes outside fixed role trees; candidates need classification, not automatic proof of leaked sandbox.'}
            sample['violations']=errors
            with (output/'host-samples.jsonl').open('a') as trace:trace.write(json.dumps(sample)+'\n')
            atomic_json(output/'progress.json',{'time_unix':sample['time_unix'],'phase':phase,'sample_index':index,'sample':sample})
            if errors:
                failures+=errors;atomic_json(output/'STOP.json',{'reasons':errors,'time_unix':time.time()});break
            previous=sample
            deadline+=args.interval
            if deadline<time.monotonic():deadline=time.monotonic()
            time.sleep(max(0,deadline-time.monotonic()))
    except Exception as error:
        failures.append('observer:'+type(error).__name__)
        atomic_json(output/'STOP.json',{'reasons':failures,'time_unix':time.time()})
    finally:
        if not index:failures.append('no_samples')
        atomic_json(output/'monitor-summary.json',{'passed':not failures,'failures':failures,'samples':index,'data_cap_bytes':SOAK_DATA_CAP_BYTES,'elapsed_seconds':time.monotonic()-started})
    return int(bool(failures))


if __name__=='__main__':raise SystemExit(main())
