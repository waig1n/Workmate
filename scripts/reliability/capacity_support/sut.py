"""Bounded supervisor for the isolated CAPACITY Gateway and real Worker processes."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time

from common import EVIDENCE,write,fixture_configuration


def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers',type=int,choices=(1,2),required=True)
    parser.add_argument('--capacity',type=int,choices=(2,4),required=True)
    parser.add_argument('--seed',action='store_true',help='Seed a new isolated database before starting roles')
    return parser.parse_args(argv)


async def stop_child(process,seconds):
    if process.returncode is None: process.terminate()
    try: return await asyncio.wait_for(process.wait(),seconds),False
    except TimeoutError:
        process.kill()
        return await asyncio.wait_for(process.wait(),5),True


async def main(args):
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    assert not (EVIDENCE/'ready.json').exists(),'Use a fresh CAPACITY evidence directory'
    stop=asyncio.Event(); children={}; handles=[]; failures=[]; role_states={}
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT): loop.add_signal_handler(sig,stop.set)
    environment={**os.environ,'CAPACITY_CAPACITY':str(args.capacity),'WORKMATE_WORKER_CAPACITY':str(args.capacity),
        'WORKMATE_WORKER_STORAGE_NODE_ID':'node','WORKMATE_COORDINATION_NAMESPACE':os.environ['WORKMATE_LOAD_NAMESPACE'],
        'REME_ENABLED':'false','WORKSPACE_ASSISTANT_ENABLED':'false',
        'WORKMATE_CONFIG_FILE':fixture_configuration()}
    async def launch(name,script,*extra):
        handle=(EVIDENCE/(name+'.log')).open('wb'); handles.append(handle)
        process=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).with_name(script)),*extra,
            env=environment,stdout=handle,stderr=asyncio.subprocess.STDOUT)
        children[name]=process
        return process
    started=time.monotonic()
    try:
        if args.seed:
            seeded=await launch('seed','seed.py')
            assert await asyncio.wait_for(seeded.wait(),90)==0,'CAPACITY seed failed'
            children.pop('seed')
        await launch('gateway','gateway.py')
        for index in range(args.workers): await launch('worker-'+str(index),'worker.py',str(index))
        import httpx
        async with httpx.AsyncClient(timeout=2) as http:
            async with asyncio.timeout(90):
                while True:
                    assert not stop.is_set(),'Stopped before readiness'
                    assert all(p.returncode is None for p in children.values()),'CAPACITY role exited before readiness'
                    ready_workers=[]
                    for index in range(args.workers):
                        path=EVIDENCE/('worker-'+str(index)+'-ready.json')
                        if path.exists():
                            state=json.loads(path.read_text())
                            assert state['pid']==children['worker-'+str(index)].pid,'Stale worker readiness'
                            ready_workers.append(state)
                    try: healthy=(await http.get('http://127.0.0.1:8080/health')).status_code==200
                    except httpx.HTTPError: healthy=False
                    if healthy and len(ready_workers)==args.workers: break
                    await asyncio.sleep(.2)
        write('ready.json',{'pid':os.getpid(),'children':{name:p.pid for name,p in children.items()},
            'workers':args.workers,'capacity_per_worker':args.capacity,'total_slots':args.workers*args.capacity,
            'worker_identities':ready_workers,'ready_after_seconds':time.monotonic()-started,'real_model_calls':0})
        while not stop.is_set():
            for name,p in children.items():
                if p.returncode is not None: raise RuntimeError(f'{name} exited unexpectedly: {p.returncode}')
            try: await asyncio.wait_for(stop.wait(),.2)
            except TimeoutError: pass
    except BaseException as error:
        failures.append({'operation':'supervise','error_type':type(error).__name__,'detail':str(error)})
        raise
    finally:
        worker_names=[name for name in children if name.startswith('worker-')]
        async def finish(name,deadline):
            try:
                code,killed=await stop_child(children[name],deadline)
                if code!=0 or killed: failures.append({'role':name,'exit_code':code,'forced_kill':killed})
            except Exception as error: failures.append({'role':name,'cleanup_error':type(error).__name__})
        await asyncio.gather(*(finish(name,35) for name in worker_names))
        for name in children:
            if name not in worker_names: await finish(name,15)
        for name in children:
            path=EVIDENCE/(name+'-state.json')
            if not path.exists():
                failures.append({'role':name,'missing_state':True}); continue
            try:
                state=json.loads(path.read_text()); role_states[name]=state
                if state.get('failures'): failures.append({'role':name,'failures':state['failures']})
                if name.startswith('worker-') and state.get('ownership_remaining')!=0:
                    failures.append({'role':name,'ownership_remaining':state.get('ownership_remaining')})
            except Exception as error: failures.append({'role':name,'state_error':type(error).__name__})
        for handle in handles: handle.close()
        ownership=sum(state.get('ownership_remaining',0) or 0 for name,state in role_states.items() if name.startswith('worker-'))
        write('state.json',{'pid':os.getpid(),'passed':not failures,'failures':failures,'ownership_remaining':ownership,
            'children':{name:{'pid':p.pid,'exit_code':p.returncode} for name,p in children.items()},'real_model_calls':0})
    assert not failures,failures


if __name__=='__main__': asyncio.run(main(arguments()))
