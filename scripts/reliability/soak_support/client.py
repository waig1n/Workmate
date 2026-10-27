"""Continuous isolated SOAK client. No service launches, external model calls or retries."""
import argparse
import asyncio
from collections import defaultdict,deque
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import time


def arrival_rate(elapsed,base,burst):
    return burst if elapsed>=21600 and elapsed%21600<600 else base


class ConversationReuse:
    def __init__(self,fraction):
        self.fraction=fraction;self.admissions=0;self.reused=0;self.available=defaultdict(deque)
    def take(self,case):
        self.admissions+=1
        if self.available[case] and self.reused<math.floor(self.admissions*self.fraction):
            self.reused+=1
            return self.available[case].popleft(),2
        return None
    def complete(self,case,cid,turn):
        if turn==1:self.available[case].append(cid)


def failure_class(row):
    if row.get('correct'):return None
    if row.get('http_status')==429 and row.get('accepted') is False:return 'explained_rejection'
    return 'unknown_effect'


def monitor_failure(directory,stale_seconds):
    if (directory/'STOP.json').exists():return 'resource_stop'
    if (directory/'monitor-summary.json').exists():return 'monitor_ended'
    try:age=time.time()-(directory/'host-samples.jsonl').stat().st_mtime
    except OSError:return 'monitor_missing'
    return 'monitor_stale' if age>stale_seconds else None


def atomic_json(path,value):
    temporary=path.with_name(path.name+'.tmp');temporary.write_text(json.dumps(value,indent=2,default=str));temporary.replace(path)


def retain_traces(directory,row,index):
    receipts=[]
    for path in sorted(directory.glob('*sse-*.jsonl')):
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(65536),b''):digest.update(block)
        keep=(not row.get('correct') or row.get('invalid') or index%20==0
              or (row.get('first_body_seconds') or 0)>10
              or row.get('elapsed_seconds',0)-row.get('human_wait_seconds',0)>60)
        receipts.append(dict(name=path.name,bytes=path.stat().st_size,sha256=digest.hexdigest(),retained=keep))
        if not keep:path.unlink()
    row['trace_receipts']=receipts


async def drive_arrivals(duration,rate,burst_rate,max_inflight,operation,completed,monitor,on_abort,*,arrival_start=None,preparation_seconds=0):
    """Independent arrival clock; never batch-barrier, catch up, or retry work."""
    start=time.monotonic() if arrival_start is None else arrival_start
    planned=start;pending=set();index=0;abort=None
    async def one(number,due):
        try:row=await operation(number,due)
        except asyncio.CancelledError:
            await completed(dict(index=number,correct=False,error_type='CancelledBySupervisor',planned_monotonic=due))
            raise
        except Exception as error:row=dict(index=number,correct=False,error_type=type(error).__name__,error=str(error))
        row.setdefault('index',number);row.setdefault('planned_monotonic',due)
        await completed(row)
        return row
    async def freeze(reason):
        # Join cancelled request producers before enumerating/pause/Stop cleanup.
        for task in pending:
            if not task.done():task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        pending.clear()
        await on_abort(reason)
    try:
        while time.monotonic()<start+duration:
            done={task for task in pending if task.done()}
            for task in done:
                row=task.result()
                if failure_class(row)=='unknown_effect':abort='unknown_effect'
                if row.get('invalid'):abort='invalid_sender_sample'
            pending-=done
            abort=abort or monitor()
            if abort:break
            now=time.monotonic()
            if planned<start+duration and now>=planned-preparation_seconds:
                reason='send_lateness' if now-planned>.5 else 'outstanding_limit' if len(pending)>=max_inflight else None
                if reason:
                    await completed(dict(index=index,correct=False,invalid=True,error_type=reason,
                                         planned_monotonic=planned,send_lateness_seconds=now-planned))
                    abort=reason;break
                pending.add(asyncio.create_task(one(index,planned)));index+=1
                planned+=1/arrival_rate(planned-start,rate,burst_rate)
            await asyncio.sleep(min(.1,max(0,(planned-preparation_seconds if planned<start+duration else start+duration)-time.monotonic())))
        if abort:await freeze(abort)
        # Every case has the existing bounded 120-second operation deadline.
        async with asyncio.timeout(130):
            while pending:
                done,pending=await asyncio.wait(pending,timeout=.1,return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    row=task.result()
                    if failure_class(row)=='unknown_effect' and abort is None:
                        abort='unknown_effect';await freeze(abort)
                reason=monitor()
                if reason and abort is None:abort=reason;await freeze(abort)
        return dict(abort_reason=abort,admitted_slots=index,arrival_start_monotonic=start,measured_seconds=max(0,time.monotonic()-start))
    finally:
        for task in pending:task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('base-url','dsn','redis-url','namespace','output','monitor'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--duration',type=float,required=True)
    parser.add_argument('--rate',type=float,default=.028)
    parser.add_argument('--burst-rate',type=float,default=.0428)
    parser.add_argument('--max-inflight',type=int,default=4)
    parser.add_argument('--reuse',type=float,default=.5)
    parser.add_argument('--monitor-stale-seconds',type=float,default=45)
    parser.add_argument('--warmup-count',type=int,choices=(20,),default=None)
    return parser


async def run(args):
    from scripts.reliability.capacity_support import client as capacity
    from scripts.reliability.load_supervisor import stop_fixture_tasks
    from scripts.reliability.soak_support.schedule import execute_scheduled_case,cleanup_scheduled_sources
    from scripts.reliability.load_support.common import JWT_SECRET
    import asyncpg
    import httpx
    import jwt
    from redis.asyncio import Redis
    for value in (args.base_url,args.dsn,args.redis_url):capacity.loopback_url(value)
    if not 0<args.duration<=86400 or args.rate!=.028 or args.burst_rate!=.0428 or args.max_inflight!=4 or args.reuse!=.5:
        raise ValueError('SOAK frozen duration/rate/burst/inflight/reuse policy required')
    if not args.namespace or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in args.namespace):raise ValueError('Isolated namespace required')
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    monitor=Path(args.monitor)
    args.workload='mixed';args.case=None
    token=jwt.encode(dict(sub='fixture-user',ver=0,iat=int(time.time()),exp=int(time.time()+args.duration+1800)),JWT_SECRET,algorithm='HS256')
    headers={'Authorization':'Bearer '+token}
    report=dict(passed=False,client_checks_passed=False,state='starting',runs=[],tasks=[],failures=[],closures=[],
                requested_seconds=args.duration,rate=args.rate,burst_rate=args.burst_rate,max_inflight=4,reuse_fraction=.5,
                limitations=['Remote Write/Stop effect evidence must be independently joined before overall acceptance',
                             'Body delivery includes controlled generation; only sender-local monotonic intervals are comparable'])
    reuse=ConversationReuse(args.reuse);started=time.monotonic();halt=asyncio.Event();fatal=[];closure_tasks=[];scheduled_windows=set()
    report['mode']='warmup' if getattr(args,'warmup_count',None) else 'load'
    report['counts_toward_soak']=not bool(getattr(args,'warmup_count',None))
    cancelled=None
    samples=deque(maxlen=600);sample_counter=0;sample_cpu_start=time.process_time();all_rows=[]
    redis=Redis.from_url(args.redis_url,decode_responses=True)
    async def progress():
        nonlocal sample_counter
        previous=time.monotonic()
        while not halt.is_set():
            now=time.monotonic();lag=max(0,now-previous-1);samples.append(lag);sample_counter+=1
            lag95=capacity.percentile(list(samples),.95)
            if len(samples)>=20 and lag95>.1:fatal.append('client_loop_lag_p95')
            row=dict(monotonic=now,cpu_seconds=time.process_time()-sample_cpu_start,
                     max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,loop_lag_seconds=lag)
            path=output/'client-samples.jsonl'
            if path.exists() and path.stat().st_size>=4*2**20:
                older=output/'client-samples.1.jsonl';older.unlink(missing_ok=True);path.replace(older)
            with path.open('a') as stream:stream.write(json.dumps(row)+'\n')
            atomic_json(output/'progress.json',dict(state=report['state'],elapsed_seconds=now-started,
                        time_unix=time.time(),phase='load',completed=len(all_rows),correct=sum(r.get('correct',False) for r in all_rows),
                        failures=fatal[-10:],last_sample_monotonic=now,loop_lag_p95_seconds=lag95))
            previous=now
            try:await asyncio.wait_for(halt.wait(),1)
            except TimeoutError:pass
    sampler=asyncio.create_task(progress())
    def monitor_check():
        return fatal[0] if fatal else monitor_failure(monitor,args.monitor_stale_seconds)
    async def abort(reason):
        report['state']='stopping';report['failures'].append(reason)
        try:
            async with httpx.AsyncClient(base_url=args.base_url,headers=headers,timeout=15) as control:
                report['source_cleanup']=await asyncio.wait_for(cleanup_scheduled_sources(control,output),30)
        except Exception as error:
            report['source_cleanup_error']=type(error).__name__
            fatal.append('schedule_source_cleanup_failed')
        try:report['stop_requests']=await asyncio.wait_for(stop_fixture_tasks(args.base_url,args.dsn),30)
        except Exception as error:report['stop_request_error']=type(error).__name__
        atomic_json(output/'summary.json',report)
    async def closure():
        connection=None
        try:
            connection=await asyncpg.connect(args.dsn,timeout=10,command_timeout=15)
            result=await capacity.drain(connection,redis,args.namespace,timeout=300)
        except Exception as error:
            result=dict(passed=False,error_type=type(error).__name__)
        finally:
            if connection is not None:await connection.close()
        result['observed_monotonic']=time.monotonic();report['closures'].append(result)
        if not result['passed']:fatal.append('queue_closure_timeout')
        return result
    def refresh():
        report['tasks']=all_rows;report['runs']=[dict(tasks=all_rows,budgets=capacity.budgets([row for row in all_rows if row.get('correct')],workload='mixed'))]
        explained=sum(failure_class(r)=='explained_rejection' for r in all_rows)
        report['explained_technical_failure_fraction']=explained/len(all_rows) if all_rows else None
        report['planned_stop_count']=sum(bool(r.get('correct')) and r.get('workload')=='stop' for r in all_rows)
        report['reuse_count']=reuse.reused
        atomic_json(output/'summary.json',report)
    async def completed(row):
        all_rows.append(row)
        if row.get('invalid'):fatal.append('invalid_sender_sample')
        if row.get('correct') and row.get('source')!='schedule':
            reuse.complete(row['workload'],row['conversation_id'],row['conversation_turn'])
        atomic_json(output/f"task-{row['index']}.json",row)
        with (output/'index.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(index=row['index'],task_id=row.get('id'),key=row.get('key'),
                              correct=row.get('correct'),workload=row.get('workload'),path=f"task-{row['index']}.json"))+'\n')
        if len(all_rows)%20==0:
            refresh()
            if not any(not task.done() for task in closure_tasks):closure_tasks.append(asyncio.create_task(closure()))
    try:
        reason=monitor_check()
        if reason:raise RuntimeError(reason)
        async with httpx.AsyncClient(base_url=args.base_url,headers=headers,timeout=15) as http:
            async def operation(index,planned):
                case=capacity.case_for(index,'mixed')
                launched=time.monotonic()
                offset=max(0,planned-report['arrival_start_monotonic'])
                effective_rate=arrival_rate(offset,args.rate,args.burst_rate)
                window=int(offset//21600)
                scheduled=case=='text' and window not in scheduled_windows
                directory=output/f'trace-{index}';directory.mkdir()
                if scheduled:
                    scheduled_windows.add(window)
                    row=await execute_scheduled_case(args,http,headers,output,index,admission_not_before=None if getattr(args,'warmup_count',None) else planned)
                    row['source']='schedule';row['conversation_turn']=1
                    # Schedule's deliberate two-second due lead is not sender delay.
                    source_sent=row.get('source_created_monotonic',row.get('source_submitted_monotonic'))
                    if source_sent is None:
                        row.update(correct=False,error_type='MissingScheduleSubmissionClock')
                        row['send_lateness_seconds']=0
                    else:row['send_lateness_seconds']=max(0,source_sent-planned)
                else:
                    selected=reuse.take(case);cid,turn=selected if selected else (None,1)
                    row=await capacity.execute_case(args,http,headers,directory,index,case_override=case,conversation_id=cid,admission_not_before=None if getattr(args,'warmup_count',None) else planned)
                    row.update(conversation_turn=turn,send_lateness_seconds=max(0,row.get('submitted_monotonic',time.monotonic())-planned))
                row.update(planned_monotonic=planned,launch_monotonic=launched,launch_lateness_seconds=max(0,launched-planned),
                           phase_offset_seconds=offset,arrival_phase='burst' if effective_rate==args.burst_rate else 'base',
                           planned_rate=effective_rate,preparation_seconds=(max(0,row.get('ready_monotonic',row.get('source_ready_monotonic'))-launched) if row.get('ready_monotonic',row.get('source_ready_monotonic')) is not None else None),
                           deliberate_wait_seconds=row.get('deliberate_wait_seconds',0),
                           send_lateness_scope='planned-to-source-POST' if scheduled else 'planned-to-Task-POST including Conversation/SSE preparation')
                if getattr(args,'warmup_count',None):row['send_lateness_scope']='closed-loop-preparation-not-open-arrival'
                elif row['send_lateness_seconds']>.5:row['invalid']=True
                retain_traces(directory,row,index)
                return row
            report['state']='running'
            report['preparation_lead_seconds']=0 if getattr(args,'warmup_count',None) else 10
            report['arrival_start_monotonic']=time.monotonic()+report['preparation_lead_seconds']
            report['arrival_start_time_unix']=time.time()+report['preparation_lead_seconds']
            if getattr(args,'warmup_count',None):
                for index in range(args.warmup_count):
                    reason=monitor_check()
                    if reason:
                        await abort(reason);break
                    row=await operation(index,time.monotonic());await completed(row)
                    if failure_class(row)=='unknown_effect' or row.get('invalid'):
                        await abort('warmup_failure');break
                report['schedule']=dict(abort_reason=report['failures'][0] if report['failures'] else None,admitted_slots=len(all_rows),arrival_start_monotonic=report['arrival_start_monotonic'],measured_seconds=time.monotonic()-started)
            else:
                report['schedule']=await drive_arrivals(args.duration,args.rate,args.burst_rate,4,operation,completed,monitor_check,abort,arrival_start=report['arrival_start_monotonic'],preparation_seconds=10)
            report['source_cleanup']=await cleanup_scheduled_sources(http,output)
        for task in closure_tasks:await task
        await closure()
        refresh()
        good=[r for r in all_rows if r.get('correct')]
        explained=sum(failure_class(r)=='explained_rejection' for r in all_rows)
        report['client_checks_passed']=bool(all_rows) and not fatal and not report['schedule']['abort_reason'] and all(r.get('correct') or failure_class(r)=='explained_rejection' for r in all_rows) and explained/len(all_rows)<.005 and capacity.budgets(good,workload='mixed')['passed'] and all(c['passed'] for c in report['closures'])
        report['remote_effects_pending']=any(r.get('workload') in ('file','approval','skill','stop') for r in good)
        report['passed']=report['client_checks_passed'] and not report['remote_effects_pending']
        report['state']='completed' if report['client_checks_passed'] else 'failed'
    except BaseException as error:
        if isinstance(error,asyncio.CancelledError):cancelled=error
        report.update(state='failed',error_type=type(error).__name__,error=str(error))
        await abort(type(error).__name__)
        report['state']='failed'
    finally:
        halt.set();await sampler
        for task in closure_tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*closure_tasks,return_exceptions=True)
        await redis.aclose()
        report['elapsed_seconds']=time.monotonic()-started
        report['client_sampling']=dict(samples=sample_counter,last_window_samples=len(samples),last_window_loop_lag_p95_seconds=capacity.percentile(list(samples),.95),disk_cap_bytes=8*2**20)
        refresh()
        atomic_json(output/'progress.json',dict(state=report['state'],phase='cooldown',time_unix=time.time(),elapsed_seconds=report['elapsed_seconds'],completed=len(all_rows)))
    if cancelled is not None:raise cancelled
    return report


def main():
    report=asyncio.run(run(build_parser().parse_args()))
    print(json.dumps(dict(passed=report['passed'],client_checks_passed=report['client_checks_passed'])))
    raise SystemExit(0 if report['client_checks_passed'] else 1)


if __name__=='__main__':main()
