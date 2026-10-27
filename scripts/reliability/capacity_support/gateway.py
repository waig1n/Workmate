"""Real Gateway bound only inside the dedicated internal Docker network."""
import asyncio
from collections import deque
from contextlib import nullcontext
import os
import signal
import time
from common import guard,write,JWT_SECRET


def install_stop_probe(web,*,root='/shared/workspaces'):
    """CAPACITY-only read-only marker observation; never installed by product build_app."""
    import jwt
    from fastapi import Header,HTTPException,Path
    from common import stop_effects

    @web.get('/__capacity/stop/{key}',include_in_schema=False)
    async def stop_probe(key: str=Path(pattern=r'^[0-9a-f]{32}$'),authorization: str=Header(default='')):
        try:
            if not authorization.startswith('Bearer '): raise ValueError('Bearer required')
            claims=jwt.decode(authorization[7:],JWT_SECRET,algorithms=['HS256'],options={'require':['exp','sub','ver']})
            if claims['sub']!='fixture-user' or claims['ver']!=0: raise ValueError('Wrong fixture identity')
        except (jwt.InvalidTokenError,ValueError):
            raise HTTPException(status_code=401,detail='Invalid fixture authentication') from None
        return stop_effects(key,root)


async def main():
    guard('gateway')
    os.environ.update(JWT_SECRET=JWT_SECRET,WORKMATE_COORDINATION_NAMESPACE=os.environ['WORKMATE_LOAD_NAMESPACE'])
    from app.config import Settings
    Settings.model_config['env_file']=None
    from app.main import build_app
    import uvicorn
    web=build_app(); install_stop_probe(web); server=uvicorn.Server(uvicorn.Config(web,host='0.0.0.0',port=8080,
        log_level='warning',timeout_graceful_shutdown=10))
    server.capture_signals=nullcontext
    for sig in (signal.SIGTERM,signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig,lambda:setattr(server,'should_exit',True))
    done=asyncio.Event(); lag=deque(maxlen=240)
    async def monitor():
        while not done.is_set():
            before=time.monotonic_ns(); await asyncio.sleep(.25)
            lag.append({'ns':time.monotonic_ns(),'lag_ns':time.monotonic_ns()-before-250000000})
    timer=asyncio.create_task(monitor())
    try: await server.serve()
    finally:
        done.set(); await timer
        failures={}; state={'lag':list(lag),'pid':os.getpid()}
        for key in ('projection_task','command_publisher_task','business_publisher_task'):
            task=getattr(web.state,key,None)
            error=task.exception() if task and task.done() and not task.cancelled() else None
            state[key]={'done':task.done(),'cancelled':task.cancelled(),'exception':type(error).__name__ if error else None} if task else None
            if error: failures[key]=type(error).__name__
        state['failures']=failures; write('gateway-state.json',state)
        assert not failures,failures


if __name__=='__main__': asyncio.run(main())
