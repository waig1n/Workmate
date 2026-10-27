"""Frozen SOAK warmup -> 2h pre-run -> conditional 24h, on one isolated SUT."""
from scripts.reliability.targets import remote_target

import argparse
import asyncio
from datetime import datetime,timezone
import json
import math
import os
from pathlib import Path
import signal
import sys
import tarfile
import time
from urllib.parse import urlsplit

from .remote_fixture import Remote,tunnel
from .resource_monitor import join_effects
from .soak_support.client import atomic_json

DATA_CAP=8*2**30
BASE_RATE=.028
BURST_RATE=.0428


async def command_delivery_metrics(dsn,ids):
    """Only fixture Task IDs; counts and timestamps, never command payloads."""
    from . import metrics
    metrics.validate_dsn(dsn)
    connection=await metrics.connect(dsn,timeout=5,command_timeout=10)
    try:
        async with connection.transaction(readonly=True):
            return [dict(row) for row in await connection.fetch(
                "SELECT c.task_id,c.kind,d.status,count(d.command_id)::int AS count,"
                "min(c.created_at) AS command_created_at,min(d.created_at) AS delivery_created_at,"
                "max(d.published_at) AS last_published_at,sum(d.attempt_count)::bigint AS attempts "
                "FROM runtime_commands c LEFT JOIN command_deliveries d ON d.command_id=c.id "
                "WHERE c.task_id=ANY($1::varchar[]) GROUP BY c.task_id,c.kind,d.status "
                "ORDER BY c.task_id,c.kind,d.status",ids)]
    finally:await connection.close()


async def save_task_evidence(dsn,summary,destination):
    """Persist required pre-cleanup diagnostics even when the workload gate fails."""
    from . import metrics
    destination.mkdir(parents=True,exist_ok=True)
    result={'passed':False}
    try:
        rows=summary.get('tasks',[])
        if len(rows)>5000:raise ValueError('Task evidence exceeds frozen bound')
        wrapped={'runs':[{'tasks':rows}]};ids=metrics.task_ids(wrapped)
        if not ids:raise ValueError('No Task IDs available for required evidence')
        async with asyncio.timeout(35):
            result['postgres']=await metrics.sample(dsn,ids)
            result['command_deliveries']=await command_delivery_metrics(dsn,ids)
        result['delivery']=metrics.delivery(wrapped)
        start_commands={row['task_id'] for row in result['command_deliveries'] if row.get('kind')=='StartTask'}
        start_deliveries={row['task_id'] for row in result['command_deliveries']
            if row.get('kind')=='StartTask' and row.get('status') is not None and row.get('count',0)>0}
        result['missing_start_command_task_ids']=sorted(set(ids)-start_commands)
        result['missing_start_delivery_task_ids']=sorted(set(ids)-start_deliveries)
        result['passed']=not (result['postgres']['missing_task_ids'] or
            result['missing_start_command_task_ids'] or result['missing_start_delivery_task_ids'])
        if not result['passed']:result['error_type']='MissingTaskEvidence'
    except Exception as error:
        result['error_type']=type(error).__name__
    # PG datetime/Decimal values are evidence, not strings supplied by a model.
    atomic_json(destination/'task-metrics.json',json.loads(json.dumps(result,default=str)))
    return {'passed':result['passed'],'path':str(destination/'task-metrics.json'),
            'error_type':result.get('error_type')}


def roles(sample):
    return sample['processes']['roles']


def recovery_gate(baseline,current):
    failures=[];before=roles(baseline);after=roles(current)
    if not before or set(before)!=set(after):failures.append('role_evidence_missing_or_changed')
    for name,initial in before.items():
        final=after.get(name,{})
        if (initial.get('pid'),initial.get('start_ticks'))!=(final.get('pid'),final.get('start_ticks')):
            failures.append(name+':identity_changed');continue
        if final['rss_bytes']>initial['rss_bytes']*1.2:failures.append(name+':rss_over_120_percent')
        if final['fd_count']>initial['fd_count']+4:failures.append(name+':fd_growth')
    if len(current['processes'].get('descendants',[]))>len(baseline['processes'].get('descendants',[]))+2:
        failures.append('descendant_growth')
    if current['processes'].get('orphan_candidates'):failures.append('orphan_candidates_require_review')
    if current['postgres']['active_tasks']!=0:failures.append('active_tasks_after_cooldown')
    if current['postgres']['connections']>baseline['postgres']['connections']+4:failures.append('connection_growth')
    return {'passed':not failures,'failures':failures,'rss_limit_ratio':1.2,'fd_growth_allowance':4}


def growth_gate(samples,current,remaining_seconds=88200):
    """Freeze the maximum full/last-hour/last-half-hour slope and 25% margin."""
    if len(samples)<2:return {'passed':False,'failures':['insufficient_growth_samples']}
    intervals=[b['time_unix']-a['time_unix'] for a,b in zip(samples,samples[1:])]
    if samples[-1]['time_unix']-samples[0]['time_unix']<7140 or any(gap<=0 or gap>45 for gap in intervals):
        return {'passed':False,'failures':['incomplete_two_hour_growth_coverage']}
    end=samples[-1];rates=[]
    size=lambda row:row['hosts']['remote']['owned_data_bytes']
    for window in (7200,3600,1800):
        eligible=[row for row in samples if row['time_unix']<=end['time_unix']-window]
        first=eligible[-1] if eligible else samples[0]
        seconds=end['time_unix']-first['time_unix']
        if seconds<=0:return {'passed':False,'failures':['invalid_growth_interval']}
        rates.append(max(0,size(end)-size(first))/seconds)
    # Three 10-minute 80%-rate windows add ~1.1% work to a base-rate day.
    peak_factor=1+3*600*(BURST_RATE-BASE_RATE)/(86400*BASE_RATE)
    increment=max(rates)*remaining_seconds*1.25*peak_factor
    projected=size(current)+increment;host=current['hosts']['remote']
    reserve=max(20*2**30,host['disk_total_bytes']//10)
    failures=[]
    if projected>DATA_CAP:failures.append('projected_data_cap')
    if host['disk_free_bytes']-increment<reserve:failures.append('projected_disk_reserve')
    return {'passed':not failures,'failures':failures,'bytes_per_second_candidates':rates,
        'projected_data_bytes':projected,'projected_increment_bytes':increment,'data_cap_bytes':DATA_CAP,
        'reserve_bytes':reserve,'engineering_margin':1.25,'peak_factor':peak_factor}


def phase_gate(summary,baseline=None,*,sender_exit=0,expected_schedules=1):
    tasks=summary.get('tasks',[]);failures=[]
    if sender_exit!=0:failures.append('sender_nonzero_exit')
    if not summary.get('client_checks_passed'):failures.append('client_checks')
    if not tasks:failures.append('no_tasks')
    if any(not row.get('correct') for row in tasks):failures.append('task_failure')
    if sum(row.get('source')=='schedule' and bool(row.get('correct')) for row in tasks)!=expected_schedules:failures.append('verified_schedule_count_mismatch')
    if any(not row.get('passed') for row in summary.get('closures',[])):failures.append('queue_closure')
    if not summary.get('closures'):failures.append('missing_queue_closure')
    # Human approval latency is retained raw and separately excluded from the
    # engineering platform interval; this rule is frozen before SOAK starts.
    by_case={}
    for row in tasks:
        if row.get('correct') and row.get('workload')!='stop':by_case.setdefault(row['workload'],[]).append(row)
    from .capacity_support.client import percentile
    timings={}
    for case,rows in by_case.items():
        first=percentile([max(0,row['first_body_seconds']-row.get('human_wait_seconds',0)) for row in rows],.95)
        end=percentile([max(0,row['elapsed_seconds']-row.get('human_wait_seconds',0)) for row in rows],.95)
        timings[case]={'first_p95':first,'completion_p95':end,'count':len(rows)}
        if first>10 or end>60:failures.append(case+':latency_budget')
    if baseline:
        for case,row in timings.items():
            old=baseline.get('timings',{}).get(case)
            if old and (row['first_p95']>old['first_p95']*1.2 or row['completion_p95']>old['completion_p95']*1.2):
                failures.append(case+':latency_degraded_over_20_percent')
    return {'passed':not failures,'failures':failures,'timings':timings}


def formal_transition_gate(prerun):
    required=('gate','effects','recovery','growth_projection','latency_trend','task_evidence')
    missing=[key for key in required if prerun.get(key,{}).get('passed') is not True]
    if prerun.get('sender_exit_code')!=0:missing.append('sender_exit_code')
    return {'passed':not missing,'failures':missing}


def observer_gate(directory,code):
    failures=[]
    if code!=0:failures.append('observer_exit_nonzero')
    if (directory/'STOP.json').exists():failures.append('observer_stop_present')
    try:
        receipt=json.loads((directory/'monitor-summary.json').read_text())
        if not isinstance(receipt,dict) or receipt.get('passed') is not True or receipt.get('failures'):failures.append('observer_summary_failed')
    except (OSError,ValueError,TypeError):failures.append('observer_summary_missing_or_invalid')
    return {'passed':not failures,'failures':failures,'exit_code':code}


async def terminate_child(child,*,grace=5,signal_number=signal.SIGTERM):
    """Always reap the exact child; escalate once after a bounded grace period."""
    forced=False
    if child.returncode is None:
        try:child.send_signal(signal_number)
        except ProcessLookupError:pass
    try:await asyncio.wait_for(child.wait(),grace)
    except TimeoutError:
        forced=True
        try:child.kill()
        except ProcessLookupError:pass
        await asyncio.wait_for(child.wait(),10)
    return {'exit_code':child.returncode,'forced_kill':forced}


def latency_trend(summary):
    from .capacity_support.client import percentile
    start=summary.get('schedule',{}).get('arrival_start_monotonic')
    if start is None:return {'passed':False,'cases':{},'failures':['missing_arrival_clock']}
    selected=[]
    for row in summary.get('tasks',[]):
        if not row.get('correct') or row.get('workload')=='stop':continue
        planned=row.get('planned_monotonic')
        if planned is None:return {'passed':False,'cases':{},'failures':['missing_planned_clock']}
        elapsed=planned-start
        if elapsed>=21600 and elapsed%21600<600:continue
        selected.append(row)
    trend={};bad=[]
    for case in {row['workload'] for row in selected}:
        rows=sorted([row for row in selected if row['workload']==case],key=lambda r:r['planned_monotonic'])
        if len(rows)<4:bad.append(case+':insufficient_trend_samples');continue
        count=min(20,len(rows)//2)
        measure=lambda values:percentile([r['elapsed_seconds']-r.get('human_wait_seconds',0) for r in values],.95)
        initial,ending=measure(rows[:count]),measure(rows[-count:])
        trend[case]={'initial_p95':initial,'ending_p95':ending,'sample_count_per_window':count}
        if ending>initial*1.2:bad.append(case)
    if not trend:bad.append('missing_trend_cases')
    return {'passed':not bad,'cases':trend,'failures':bad,'burst_samples_excluded':True}


class SOAKRemote(Remote):
    def status(self,allow_stopped=False):
        report=super().status(allow_stopped=allow_stopped)
        report['stop_reasons']=[r for r in report['stop_reasons'] if r!='4GiB data budget']
        if report['resources']['data_bytes']>DATA_CAP:report['stop_reasons'].append('SOAK 8GiB data budget')
        report['passed']=not report['stop_reasons']
        (self.path.parent/'status.json').write_text(json.dumps(report,indent=2)) if self.path else None
        return report


async def execute(args):
    from .capacity_support.client import loopback_url
    for value in (args.base_url,args.dsn,args.redis_url):loopback_url(value)
    args.output.mkdir(parents=True,exist_ok=False)
    state=json.loads(args.state.read_text());os.environ['WORKMATE_TEST_SSH_TARGET']=remote_target(state.get('host'));state.update(validation_stage='SOAK',data_budget_bytes=DATA_CAP)
    remote=SOAKRemote(state,args.state);remote.save()
    report={'stage':'SOAK','status':'starting','passed':False,'owner':state['owner'],'started_at':datetime.now(timezone.utc).isoformat(),
        'phases':{},'frozen_policy':{'rate':BASE_RATE,'burst_rate':BURST_RATE,'inflight':4,'data_cap_bytes':DATA_CAP,
        'prerun_seconds':7200,'formal_seconds':86400,'cooldown_seconds':1800,'real_paid_model_budget':0},
        'limitations':['CAPACITY full mixed/worker-expansion matrix is incomplete; rate is 52.25% of 20-Task mixed selfcheck throughput',
                       'Source-mounted acceptance; controlled model/embedding, not supplier or compiled-release acceptance']}
    old_cwd=Path.cwd();os.chdir(Path(__file__).resolve().parents[2])
    monitor=args.output/'monitor';monitor.mkdir();children={};logs=[];current_client=None
    def save():atomic_json(args.output/'campaign.json',report)
    def phase(value):
        report['status']=value;save();atomic_json(monitor/'phase.json',{'phase':value})
    def sample():
        if (monitor/'STOP.json').exists():raise RuntimeError('Resource monitor stopped: '+(monitor/'STOP.json').read_text()[:1500])
        value=json.loads((monitor/'progress.json').read_text())
        if value.get('sample',{}).get('violations'):raise RuntimeError('Resource monitor reported violations')
        if time.time()-value['time_unix']>45:raise RuntimeError('Resource monitor stale')
        return value['sample']
    async def sleep_checked(seconds):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            sample()
            if any(p.returncode is not None for p in children.values()):raise RuntimeError('SOAK observation/tunnel process exited')
            await asyncio.sleep(min(5,max(0,until-time.monotonic())))
    async def launch(name,command):
        handle=(args.output/(name+'.log')).open('wb');logs.append(handle)
        child=await asyncio.create_subprocess_exec(*command,stdout=handle,stderr=handle)
        children[name]=child;return child
    async def run_client(name,duration,warmup=False):
        nonlocal current_client
        destination=args.output/name
        command=[sys.executable,'-m','scripts.reliability.soak_support.client','--base-url',args.base_url,
            '--dsn',args.dsn,'--redis-url',args.redis_url,'--namespace',state['owner'],'--output',str(destination),
            '--monitor',str(monitor),'--duration',str(duration)]
        if warmup:command+=['--warmup-count','20']
        handle=(args.output/(name+'.log')).open('wb');logs.append(handle)
        current_client=await asyncio.create_subprocess_exec(*command,stdout=handle,stderr=handle)
        deadline=time.monotonic()+duration+600
        while current_client.returncode is None:
            sample()
            if any(p.returncode is not None for p in children.values()):raise RuntimeError('SOAK observation/tunnel process exited')
            if time.monotonic()>deadline:raise TimeoutError('SOAK client deadline')
            await asyncio.sleep(1)
        code=await current_client.wait();current_client=None
        summary=json.loads((destination/'summary.json').read_text())
        report['phases'][name]={'sender_exit_code':code,'summary':str(destination/'summary.json')};save()
        evidence=await save_task_evidence(args.dsn,summary,destination)
        report['phases'][name]['task_evidence']=evidence;save()
        if not evidence['passed']:raise RuntimeError('Required Task timing evidence unavailable')
        return summary
    def collect_and_join(name,summary):
        remote.collect()
        archive=args.state.parent/'collected/evidence.tar.gz'
        destination=args.output/('evidence-'+name);destination.mkdir()
        with tarfile.open(archive) as tar:tar.extractall(destination,filter='data')
        result=join_effects({'runs':[{'tasks':summary['tasks']}]},destination/'evidence')
        atomic_json(args.output/name/'effects.json',result)
        if not result['passed']:raise RuntimeError('Independent tool-effect invariant failed')
        return result
    loop=asyncio.get_running_loop();task=asyncio.current_task();signalled=False
    def cancel_once():
        nonlocal signalled
        if not signalled:signalled=True;task.cancel()
    for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,cancel_once)
    save()
    try:
        resources=remote.sample();reserve=max(20*2**30,resources['disk_total_bytes']//10)
        if resources['disk_free_bytes']-reserve<DATA_CAP+2*2**30:raise RuntimeError('SOAK static plus 8GiB data reserve unavailable')
        command=tunnel(state['ips']['app'],urlsplit(args.base_url).port)
        command[-1:-1]=['-L',f"127.0.0.1:{urlsplit(args.dsn).port}:{state['ips']['pg']}:5432",'-L',f"127.0.0.1:{urlsplit(args.redis_url).port}:{state['ips']['redis']}:6379"]
        await launch('tunnel',command)
        import httpx
        async with httpx.AsyncClient(timeout=2) as http,asyncio.timeout(90):
            while True:
                receipt=remote.status()
                if receipt.get('state.json'):raise RuntimeError('SUT exited during startup')
                if receipt.get('ready.json'):
                    try:
                        if (await http.get(args.base_url+'/health')).status_code==200:break
                    except httpx.HTTPError:pass
                await asyncio.sleep(1)
        phase('warmup')
        await launch('observer',[sys.executable,'-m','scripts.reliability.soak_support.observe','--owner',state['owner'],
            '--dsn',args.dsn,'--redis-url',args.redis_url,'--output',str(monitor),'--duration','110000'])
        async with asyncio.timeout(60):
            while not (monitor/'progress.json').exists():
                if (monitor/'STOP.json').exists():raise RuntimeError('Monitor initialization failed')
                await asyncio.sleep(.5)
        warmed=await run_client('warmup',900,True)
        warm_gate=phase_gate(warmed,sender_exit=report['phases']['warmup']['sender_exit_code']);report['phases']['warmup']['gate']=warm_gate;save()
        if not warm_gate['passed']:raise RuntimeError('SOAK warmup did not pass')
        report['phases']['warmup']['effects']=collect_and_join('warmup',warmed);save()
        phase('warm_idle');await sleep_checked(90);baseline=sample()
        atomic_json(args.output/'warm-idle-baseline.json',baseline)
        for name,duration in (('prerun',7200),('formal',86400)):
            if name=='formal':
                transition=formal_transition_gate(report['phases'].get('prerun',{}));report['formal_transition']=transition;save()
                if not transition['passed']:raise RuntimeError('Pre-run gates do not authorize formal window')
            phase(name);started=time.time();summary=await run_client(name,duration)
            gate=phase_gate(summary,sender_exit=report['phases'][name]['sender_exit_code'],expected_schedules=4 if name=='formal' else 1)
            if summary.get('schedule',{}).get('measured_seconds',0)<duration:gate['passed']=False;gate['failures'].append('incomplete_continuous_window')
            report['phases'][name]['gate']=gate;save()
            if not gate['passed']:raise RuntimeError(name+' workload gate did not pass')
            report['phases'][name]['effects']=collect_and_join(name,summary);save()
            phase('cooldown');await sleep_checked(1800);current=sample()
            recovery=recovery_gate(baseline,current);report['phases'][name]['recovery']=recovery;save()
            if not recovery['passed']:raise RuntimeError(name+' cooldown resource recovery did not pass')
            data=[json.loads(line) for line in (monitor/'host-samples.jsonl').read_text().splitlines()]
            active=[row for row in data if row['time_unix']>=started and row.get('phase')==name]
            if name=='prerun':
                growth=growth_gate(active,current);report['phases'][name]['growth_projection']=growth;save()
                if not growth['passed']:raise RuntimeError('24h growth forecast exceeds frozen budget')
            trend=latency_trend(summary)
            report['phases'][name]['latency_trend']=trend;save()
            if not trend['passed']:raise RuntimeError(name+' latency trend gate did not pass')
        report.update(status='completed',passed=True)
    except BaseException as error:
        report.update(status='failed',passed=False,error={'type':type(error).__name__,'detail':str(error)[:2000]})
    finally:
        cleanup_errors=[]
        # Reap the sender before final source/Task cleanup; never replay work.
        if current_client is not None:
            try:report['sender_shutdown']=await terminate_child(current_client,grace=100,signal_number=signal.SIGINT)
            except Exception as error:cleanup_errors.append('sender_shutdown:'+type(error).__name__)
        # Retain partial/failing phase evidence before source/Task/database cleanup.
        for name in ('warmup','prerun','formal'):
            destination=args.output/name
            if destination.exists() and not (destination/'task-metrics.json').exists():
                try:
                    path=destination/'summary.json'
                    if path.stat().st_size>16*2**20:raise ValueError('Summary exceeds evidence bound')
                    partial=json.loads(path.read_text())
                    evidence=await save_task_evidence(args.dsn,partial,destination)
                    report['phases'].setdefault(name,{})['task_evidence']=evidence
                    if not evidence['passed']:cleanup_errors.append(name+':task_evidence_missing')
                except Exception as error:
                    cleanup_errors.append(name+':task_evidence:'+type(error).__name__)
        try:
            import httpx,jwt
            from .load_support.common import JWT_SECRET
            from .soak_support.schedule import cleanup_scheduled_sources
            token=jwt.encode({'sub':'fixture-user','ver':0,'iat':int(time.time()),'exp':int(time.time())+600},JWT_SECRET,algorithm='HS256')
            async with httpx.AsyncClient(base_url=args.base_url,headers={'Authorization':'Bearer '+token},timeout=15) as http:
                for name in ('warmup','prerun','formal'):
                    if (args.output/name).exists():
                        try:await asyncio.wait_for(cleanup_scheduled_sources(http,args.output/name),30)
                        except Exception as error:cleanup_errors.append(name+':source_cleanup:'+type(error).__name__)
        except Exception as error:cleanup_errors.append('source_cleanup_setup:'+type(error).__name__)
        try:
            from .load_supervisor import stop_fixture_tasks
            report['final_stop_requests']=await asyncio.wait_for(stop_fixture_tasks(args.base_url,args.dsn),35)
            if any(row.get('status') not in (200,202) for row in report['final_stop_requests']):cleanup_errors.append('final_stop_rejected')
        except Exception as error:cleanup_errors.append('final_stop:'+type(error).__name__)
        try:(monitor/'monitor.stop').touch()
        except OSError as error:cleanup_errors.append('monitor_stop:'+type(error).__name__)
        observer=children.get('observer')
        if observer:
            try:
                try:await asyncio.wait_for(observer.wait(),35)
                except TimeoutError:
                    report['observer_forced_shutdown']=await terminate_child(observer,grace=5)
                    cleanup_errors.append('observer_shutdown_timeout')
            except Exception as error:cleanup_errors.append('observer_shutdown:'+type(error).__name__)
            report['observer_gate']=observer_gate(monitor,observer.returncode)
            if not report['observer_gate']['passed']:cleanup_errors.append('observer_final_gate')
        else:cleanup_errors.append('observer_not_started')
        try:report['cleanup']=remote.cleanup()
        except Exception as error:report['cleanup']={'passed':False,'error_type':type(error).__name__}
        if not report['cleanup']['passed']:cleanup_errors.append('remote_cleanup_failed')
        for name,child in children.items():
            if child.returncode is None:
                try:report.setdefault('child_shutdown',{})[name]=await terminate_child(child)
                except Exception as error:cleanup_errors.append(name+':shutdown:'+type(error).__name__)
        for handle in logs:
            try:handle.close()
            except OSError as error:cleanup_errors.append('log_close:'+type(error).__name__)
        report['cleanup_errors']=cleanup_errors
        if cleanup_errors:report['passed']=False
        if not report['passed']:report['status']='failed'
        report['finished_at']=datetime.now(timezone.utc).isoformat();save()
        os.chdir(old_cwd)
        for sig in (signal.SIGINT,signal.SIGTERM):loop.remove_signal_handler(sig)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('state','output'):parser.add_argument('--'+name,type=Path,required=True)
    for name in ('base-url','dsn','redis-url'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    for name in ('state','output'):setattr(args,name,getattr(args,name).resolve())
    report=asyncio.run(execute(args));print(json.dumps({'status':report['status'],'passed':report['passed']}))
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
