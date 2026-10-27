"""Run the frozen CAPACITY text matrix, then collect and clean its dedicated SUT."""
from scripts.reliability.targets import remote_target

import argparse
import asyncio
import signal
import contextlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from .remote_fixture import Remote


def classification(summary):
    if not summary.get('sample_validity',{}).get('valid',False):return 'invalid_sample'
    runs=summary.get('runs',[])
    if len(runs)!=3:return 'incomplete'
    if any(not run.get('closure',{}).get('passed') for run in runs):return 'closure_failure'
    if any(not task.get('correct') for run in runs for task in run.get('tasks',[])):return 'correctness_failure'
    if any(len(run.get('tasks',[]))!=16 for run in runs):return 'incomplete'
    return 'passed' if all(run.get('budgets',{}).get('passed') for run in runs) else 'capacity_boundary'


async def supervise_sender(args,client_args):
    from .load_supervisor import supervise
    return await supervise(args,client_args)


async def run_sender(args,owner,destination,concurrency,timeout=None):
    budget=max(600,2000//concurrency)
    sender_args=argparse.Namespace(base_url=args.base_url,dsn=args.dsn,redis_url=args.redis_url,
        namespace=owner,monitor=args.monitor,output=destination,timeout=budget)
    client_args=['--mode','closed','--workload','text','--concurrency',str(concurrency),'--tasks','16','--repeat','3']
    # Cancellation enters supervise's freeze/Stop/kill/wait finally; no detached
    # supervisor subprocess can be killed while its new-session sender survives.
    return await asyncio.wait_for(supervise_sender(sender_args,client_args),
                                  timeout if timeout is not None else budget+65)


def campaign_passed(levels,cleanup):
    return len(levels)==4 and all(row['classification']=='passed' and row.get('sender_exit')==0
        and row.get('metrics_passed') for row in levels) and cleanup.get('passed') is True


async def campaign(args):
    args.output.mkdir(parents=True,exist_ok=False)
    state=json.loads(args.state.read_text());os.environ['WORKMATE_TEST_SSH_TARGET']=remote_target(state.get('host'));remote=Remote(state,args.state)
    report={'scope':'CAPACITY formal text matrix only; not overall CAPACITY acceptance','started_unix':time.time(),
            'owner':state['owner'],'levels':[],'finished':False,'passed':False,
            'script_root':str(Path(__file__).resolve().parents[2])}
    def save():(args.output/'campaign.json').write_text(json.dumps(report,indent=2,default=str))
    save()
    # Child -m imports must come from the same frozen tree as this campaign.
    old_cwd=Path.cwd();os.chdir(Path(__file__).resolve().parents[2])
    task=asyncio.current_task();loop=asyncio.get_running_loop();signalled=False
    def cancel_once():
        nonlocal signalled
        if not signalled:signalled=True;task.cancel()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,cancel_once)
    tunnel_process=None;tunnel_log=None
    try:
        if getattr(args,'own_tunnel',False):
            from .capacity_support.client import loopback_url
            from .remote_fixture import tunnel
            for value in (args.base_url,args.dsn,args.redis_url):loopback_url(value)
            command=tunnel(state['ips']['app'],urlsplit(args.base_url).port)
            command[-1:-1]=['-L',f"127.0.0.1:{urlsplit(args.dsn).port}:{state['ips']['pg']}:5432",
                           '-L',f"127.0.0.1:{urlsplit(args.redis_url).port}:{state['ips']['redis']}:6379"]
            tunnel_log=(args.output/'tunnel.log').open('wb')
            tunnel_process=await asyncio.create_subprocess_exec(*command,stdout=tunnel_log,stderr=tunnel_log)
            import httpx
            async with httpx.AsyncClient(timeout=2) as http,asyncio.timeout(20):
                while True:
                    if tunnel_process.returncode is not None:raise RuntimeError('Owned tunnel exited during startup')
                    try:
                        if (await http.get(args.base_url+'/health')).status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.2)
        for concurrency in (1,2,4,8):
            destination=args.output/f'concurrency-{concurrency}'
            with (args.output/f'concurrency-{concurrency}.log').open('w') as log,contextlib.redirect_stdout(log):
                receipt=await run_sender(args,state['owner'],destination,concurrency)
            path=destination/'client/summary.json'
            summary=json.loads(path.read_text()) if path.exists() else {}
            category=classification(summary)
            row={'concurrency':concurrency,'classification':category,'sender_exit':0 if receipt.get('passed') else 1,
                 'supervisor':receipt,'summary':str(path),'rounds':[{'budgets':r.get('budgets'),'throughput':r.get('completed_per_second'),
                     'tasks':len(r.get('tasks',[]))} for r in summary.get('runs',[])]}
            report['levels'].append(row);save()
            if path.exists():
                from .metrics import sample,task_ids,delivery
                measured=await sample(args.dsn,task_ids(summary));measured['delivery']=delivery(summary)
                (destination/'metrics.json').write_text(json.dumps(measured,indent=2,default=str))
                row['metrics_passed']=True;save()
            if category!='passed' or not receipt.get('passed') or not row.get('metrics_passed'):break
        report['finished']=True
    except BaseException as error:
        report['error']={'type':type(error).__name__,'detail':str(error)}
        # Never swallow process-level termination as a successful campaign.
    finally:
        # Sender supervisor has reaped its child before execution reaches here.
        try:(args.monitor/'monitor.stop').touch()
        except OSError as error:report['monitor_stop_error']=type(error).__name__
        try:report['cleanup']=remote.cleanup()
        except Exception as error:report['cleanup']={'passed':False,'error_type':type(error).__name__}
        if tunnel_process is not None:
            if tunnel_process.returncode is None:tunnel_process.terminate()
            try:await asyncio.wait_for(tunnel_process.wait(),5)
            except TimeoutError:tunnel_process.kill();await tunnel_process.wait()
            report['owned_tunnel_closed']=True
        if tunnel_log is not None:tunnel_log.close()
        report['finished_unix']=time.time()
        report['passed']=not report.get('error') and not report.get('monitor_stop_error') and campaign_passed(report['levels'],report['cleanup'])
        save();os.chdir(old_cwd)
        for sig in (signal.SIGTERM,signal.SIGINT):loop.remove_signal_handler(sig)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--monitor',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--dsn',required=True)
    parser.add_argument('--redis-url',required=True)
    parser.add_argument('--own-tunnel',action='store_true')
    args=parser.parse_args()
    for name in ('state','monitor','output'):setattr(args,name,getattr(args,name).resolve())
    report=asyncio.run(campaign(args))
    print(json.dumps({'scope':report['scope'],'finished':report['finished'],'passed':report['passed'],
                      'levels':[(row['concurrency'],row['classification']) for row in report['levels']]}))
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
