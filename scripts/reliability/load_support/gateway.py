from collections import deque
import asyncio,hashlib,json,os,socket,sys,time,signal
from contextlib import nullcontext
from pathlib import Path
from common import guard,JWT_SECRET,configure_database

guard('gateway')
os.environ.update(JWT_SECRET=JWT_SECRET,WORKSPACE_ASSISTANT_ENABLED='false',RUNTIME_INTERNAL_TOKEN='fixture-only-fixture',WORKMATE_WORKER_STORAGE_NODE_ID='node',WORKMATE_COORDINATION_NAMESPACE=os.environ['WORKMATE_LOAD_NAMESPACE'],WORKMATE_CONFIG_FILE='/support/config.yaml',ARTIFACT_STORAGE_DIR='/shared/artifacts')
if os.environ.get('WORKMATE_LOAD_WORKLOAD') in ('rag','mixed'):
    config=Path('/support/config.yaml').read_text()
    config=config.replace('product:\n','product:\n  knowledge:\n    enabled: true\n    embedding:\n      type: openai\n      model: fixture\n      dimensions: 2\n      api_key_env: WORKMATE_LOAD_MODEL_KEY\n')
    Path('/tmp/fixture-config.yaml').write_text(config)
    os.environ['WORKMATE_CONFIG_FILE']='/tmp/fixture-config.yaml'
from app.config import Settings
Settings.model_config['env_file']=None
import app.db.engine as database
from skill_fixture import FixtureStore
store=FixtureStore()
import workmate_product.skill_storage as blob
blob.store_from_settings=lambda _:store
configure_database(database, store)
from app.main import build_app
import uvicorn
endpoint=socket.socket(fileno=int(sys.argv[1])); assert endpoint.getsockname()[0]=='127.0.0.1'
async def main():
    web=build_app(); server=uvicorn.Server(uvicorn.Config(web,log_level='warning',timeout_graceful_shutdown=10))
    # Fixture signal bridge avoids Uvicorn re-raising SIGTERM before diagnostics flush.
    # Production FastAPI lifespan and Uvicorn graceful shutdown remain unchanged.
    server.capture_signals=nullcontext
    for sig in (signal.SIGTERM,signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig,lambda:setattr(server,'should_exit',True))
    done=asyncio.Event(); lag=deque(maxlen=240)
    async def monitor():
        while not done.is_set():
            before=time.monotonic_ns(); await asyncio.sleep(.25)
            lag.append({'ns':time.monotonic_ns(),'lag_ns':time.monotonic_ns()-before-250000000})
    task=asyncio.create_task(monitor())
    try: await server.serve(sockets=[endpoint])
    finally:
        done.set(); await task
        state={'lag':list(lag)}
        for key in ('projection_task','command_publisher_task','business_publisher_task'):
            t=getattr(web.state,key,None)
            state[key]={'done':t.done(),'cancelled':t.cancelled(),'exception':type(t.exception()).__name__ if t.done() and not t.cancelled() and t.exception() else None} if t else None
        Path('/evidence/gateway-state.json').write_text(json.dumps(state))
asyncio.run(main())
