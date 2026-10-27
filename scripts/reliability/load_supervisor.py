"""Supervise a local CAPACITY sender; resource stop signals prevent further admission."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time

from .capacity_support.client import loopback_url


async def stop_fixture_tasks(base_url,dsn):
    import asyncpg
    import httpx
    import jwt
    from .load_support.common import JWT_SECRET
    loopback_url(base_url);loopback_url(dsn)
    token=jwt.encode({'sub':'fixture-user','ver':0,'iat':int(time.time()),'exp':int(time.time())+300},JWT_SECRET,algorithm='HS256')
    db=await asyncpg.connect(dsn,timeout=10)
    try:
        ids=await db.fetch("SELECT t.id FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.user_id='fixture-user' AND c.agent_id IN ('capacity-text','fixture-agent') AND t.status NOT IN ('completed','failed','cancelled','interrupted','rejected')")
        results=[]
        async with httpx.AsyncClient(base_url=base_url,headers={'Authorization':'Bearer '+token},timeout=10) as client:
            for row in ids:
                response=await client.post('/api/tasks/'+row['id']+'/stop')
                results.append({'task_id':row['id'],'status':response.status_code})
        return results
    finally:await db.close()


async def supervise(args,client_args):
    protected={'--base-url','--dsn','--redis-url','--namespace','--output'}
    if any(value.startswith('--') and any(flag.startswith(value.split('=',1)[0]) for flag in protected) for value in client_args):
        raise ValueError('Client arguments must not override supervisor-owned endpoints/output')
    for url in (args.base_url,args.dsn,args.redis_url):loopback_url(url)
    if not 1<=args.timeout<=21600:raise ValueError('Bounded run timeout required')
    args.output.mkdir(parents=True,exist_ok=False)
    source=args.monitor/'host-samples.jsonl'
    command=[sys.executable,'-m','scripts.reliability.capacity_support.client','--base-url',args.base_url,
        '--dsn',args.dsn,'--redis-url',args.redis_url,'--namespace',args.namespace,'--output',str(args.output/'client'),*client_args]
    started=time.monotonic();abort=None;stopped=[];process=None;code=None;error=None;cancelled=None
    frozen=False

    async def freeze_and_stop():
        nonlocal frozen,stopped
        if process is None or process.returncode is not None:return
        try:
            os.killpg(process.pid,signal.SIGSTOP)
            frozen=True
        except ProcessLookupError:return
        # A frozen sender is never resumed: pending/new admissions must not race
        # the HTTP Stop inventory. An in-flight request is disclosed below.
        try:stopped=await asyncio.wait_for(stop_fixture_tasks(args.base_url,args.dsn),30)
        except Exception as stop_error:stopped=[{'error':type(stop_error).__name__}]

    try:
        assert args.monitor.is_dir(),'Start resource observer before sender'
        assert not (args.monitor/'STOP.json').exists(),'Resource stop already present'
        assert not (args.monitor/'monitor-summary.json').exists(),'Observer already stopped'
        assert source.exists() and time.time()-source.stat().st_mtime<40,'No recent resource sample'
        with (args.output/'client.log').open('wb') as log:
            process=await asyncio.create_subprocess_exec(*command,stdout=log,stderr=asyncio.subprocess.STDOUT,start_new_session=True)
            while process.returncode is None:
                if (args.monitor/'STOP.json').exists():abort='resource_stop'
                elif (args.monitor/'monitor-summary.json').exists():abort='monitor_ended'
                elif not source.exists() or time.time()-source.stat().st_mtime>45:abort='monitor_stale'
                elif time.monotonic()-started>args.timeout:abort='run_deadline'
                if abort:
                    await freeze_and_stop()
                    break
                await asyncio.sleep(.5)
            if not abort:code=await process.wait()
    except BaseException as caught:
        abort=abort or ('supervisor_cancelled' if isinstance(caught,asyncio.CancelledError) else 'supervisor_error')
        error={'type':type(caught).__name__,'message':str(caught)}
        if isinstance(caught,asyncio.CancelledError):cancelled=caught
        await freeze_and_stop()
    finally:
        if process is not None:
            if process.returncode is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            code=await process.wait()
    receipt=None;receipt_error=None
    if not abort and code==0:
        try:
            receipt=json.loads((args.output/'client'/'summary.json').read_text())
            if not isinstance(receipt,dict):raise ValueError('Client summary must be an object')
        except (OSError,ValueError) as caught:receipt_error=type(caught).__name__
    client_pass=bool(receipt and receipt.get('client_checks_passed') is True)
    result={'passed':abort is None and code==0 and client_pass and receipt.get('passed') is True,
            'client_checks_passed':abort is None and code==0 and client_pass,
            'remote_effects_pending':bool(receipt and receipt.get('remote_effects_pending')),
            'abort_reason':abort,'exit_code':code,'sender_frozen':frozen,
            'elapsed_seconds':time.monotonic()-started,'stopped_tasks':stopped,
            'error':error,'client_receipt_error':receipt_error,
            'stop_scope':'Dedicated fixture-user fixture Tasks; HTTP requests already in flight at freeze may finish admission and require post-stop inventory'}
    (args.output/'supervisor.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
    if cancelled is not None:raise cancelled
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('base-url','dsn','redis-url','namespace'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--monitor',type=Path,required=True)
    parser.add_argument('--timeout',type=int,default=1800)
    args,client=parser.parse_known_args()
    if client and client[0]=='--':client=client[1:]
    if not 1<=args.timeout<=21600:parser.error('Bounded run timeout required')
    for value in (args.base_url,args.dsn,args.redis_url):loopback_url(value)
    result=asyncio.run(supervise(args,client))
    raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
