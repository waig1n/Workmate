"""Production roles with only the model replaced; real S3 and real sandbox."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time


def origins(role):
    import importlib
    paths={}
    for module in ('app','workmate_runtime','workmate_product','workmate_contracts'):
        path=Path(importlib.import_module(module).__file__).resolve()
        assert str(path).startswith('/source/') and path.suffix=='.py', (module,str(path))
        paths[module]=str(path)
    Path('/evidence/'+role+'-origins.json').write_text(json.dumps(paths,indent=2))


def worker():
    from agentscope.credential import CredentialBase
    from agentscope.formatter import OpenAIChatFormatter
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.message import TextBlock, ToolCallBlock
    from pydantic import BaseModel
    from workmate_runtime.roles.settings import load_worker_role_settings
    from workmate_runtime.roles.worker import run_worker_server
    import workmate_runtime.assembly.agent as assembly
    from workmate_runtime.workspaces import WorkmateSandboxBash
    from functools import wraps
    flush_connections = lambda: None
    if os.environ.get('FAULT_DIAGNOSTICS') == '1':
        import faulthandler
        from runpy import run_path
        faulthandler.enable(all_threads=True)
        flush_connections = run_path('/source/scripts/reliability/database_fault_support/diagnostics.py')['install']()
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
        from sqlalchemy.pool import Pool
        @event.listens_for(Pool, 'invalidate')
        def invalidated(dbapi, record, error):
            task = asyncio.current_task()
            with Path('/evidence/connection-invalidation-caller.jsonl').open('a') as trace:
                trace.write(json.dumps({'time':time.monotonic(),
                    'driver_id':hex(id(getattr(dbapi,'driver_connection',None))),
                    'task':task.get_name() if task else None,
                    'coroutine':getattr(task.get_coro(),'__qualname__',None) if task else None,
                    'error_type':type(error).__name__})+'\n')
        @event.listens_for(Engine, 'before_cursor_execute')
        def before_sql(conn, cursor, statement, parameters, context, executemany):
            context._fault_started = time.monotonic()
        @event.listens_for(Engine, 'after_cursor_execute')
        def after_sql(conn, cursor, statement, parameters, context, executemany):
            elapsed = time.monotonic() - context._fault_started
            with Path('/evidence/sql-timing.jsonl').open('a') as trace:
                trace.write(json.dumps({'elapsed':elapsed,'time':time.monotonic(),
                    'sql':statement[:300], 'connection':id(conn)})+'\n')
        from workmate_runtime.execution.controller import ExecutionController
        from workmate_runtime.roles.worker import log_worker_failure
        supervise = ExecutionController._supervise
        @wraps(supervise)
        async def measured(self, *args, **kwargs):
            try: return await supervise(self, *args, **kwargs)
            except Exception as error:
                log_worker_failure('fault-diagnostic', error)
                raise
        ExecutionController._supervise = measured
    original_bash = WorkmateSandboxBash.call
    @wraps(original_bash)
    async def observed_bash(self, **kwargs):
        command = kwargs['command']
        phase = 'stale' if 'stale.txt' in command else ('crash' if 'not checkpointed' in command else 'save')
        with Path('/evidence/effects.jsonl').open('a') as trace:
            trace.write(json.dumps({'phase':phase, 'command':command})+'\n')
        async for event in original_bash(self, **kwargs): yield event
    WorkmateSandboxBash.call = observed_bash

    class Parameters(BaseModel):
        pass

    class Model(ChatModelBase):
        def __init__(self):
            super().__init__(CredentialBase(), 'fixture', Parameters(), stream=True)
            self.formatter = OpenAIChatFormatter()
            self.calls = 0

        async def _call_api(self, *args, **kwargs):
            self.calls += 1
            raw_messages=kwargs.get('messages', [])
            current=raw_messages
            for index,message in reversed(list(enumerate(raw_messages))):
                blocks=message.content if isinstance(getattr(message,'content',None),list) else []
                if any(getattr(block,'text','') in ('FAULT_SAVE','FAULT_CRASH') for block in blocks):
                    current=raw_messages[index:]; break
            results={getattr(block,'id','') for message in current
                for block in (message.content if isinstance(getattr(message,'content',None),list) else [])
                if getattr(block,'type',None)=='tool_result'}
            stage=3 if 'fault-present' in results else (2 if 'fault-tool' in results else 1)
            messages = json.dumps(current, default=str)
            local = os.environ['WORKMATE_WORKER_STORAGE_NODE_ID'] == 'fault-local'
            crash = 'FAULT_CRASH' in messages
            async def chunks():
                if stage == 1:
                    if local:
                        command = "test ! -e /workspace/working/unsaved.txt && test \"$(cat /workspace/working/durable.txt)\" = 'FAULT durable checkpoint' && test \"$(cat /workspace/output/report.txt)\" = 'FAULT original artifact' && cp /workspace/working/durable.txt /workspace/output/recovered.txt"
                    elif crash:
                        command = "printf 'not checkpointed\\n' > /workspace/working/unsaved.txt"
                    else:
                        command = "printf 'FAULT durable checkpoint\\n' > /workspace/working/durable.txt; printf 'FAULT original artifact\\n' > /workspace/output/report.txt"
                    yield ChatResponse(content=[ToolCallBlock(id='fault-tool', name='Bash', input=json.dumps({'command': command}))], is_last=True)
                elif crash and not local:
                    Path('/evidence/crash-ready.json').write_text(json.dumps({'pid': os.getpid()}))
                    while not Path('/evidence/network-restored.json').exists():
                        await asyncio.sleep(.1)
                    yield ChatResponse(content=[ToolCallBlock(id='fault-stale', name='Bash',
                        input=json.dumps({'command':"printf 'stale execution' > /workspace/output/stale.txt"}))], is_last=True)
                elif stage == 2:
                    name='recovered.txt' if local else 'report.txt'
                    yield ChatResponse(content=[ToolCallBlock(id='fault-present', name='present_files',
                        input=json.dumps({'files':['output/'+name]}))], is_last=True)
                else:
                    yield ChatResponse(content=[TextBlock(text='FAULT controlled task complete')], is_last=True)
            return chunks()

    assembly.build_selected_model = lambda *_: Model()
    async def serve():
        timer = None
        async def monitor():
            import asyncpg
            connection = await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://'))
            try:
                while True:
                    started=time.monotonic(); await asyncio.sleep(.25)
                    lag=time.monotonic()-started-.25
                    begun=time.monotonic()
                    error=None
                    try: await connection.fetchval('SELECT 1', timeout=3)
                    except Exception as exc: error=type(exc).__name__
                    with Path('/evidence/remote-timing.jsonl').open('a') as trace:
                        trace.write(json.dumps({'time':time.time(),'lag':lag,'sql_rtt':time.monotonic()-begun,'error':error})+'\n')
            finally: await connection.close()
        if os.environ.get('FAULT_DIAGNOSTICS')=='1': timer=asyncio.create_task(monitor())
        try: await run_worker_server(load_worker_role_settings())
        finally:
            if timer:
                timer.cancel(); await asyncio.gather(timer,return_exceptions=True)
            flush_connections()
    asyncio.run(serve())


if __name__ == '__main__':
    # Do not consult developer .env or the deployment configuration.
    origins(sys.argv[1])
    if sys.argv[1] == 'worker':
        worker()
    else:
        from app.config import Settings
        Settings.model_config['env_file'] = None
        from app.main import build_app
        import uvicorn
        uvicorn.run(build_app(), host='0.0.0.0', port=8080, log_level='warning')
