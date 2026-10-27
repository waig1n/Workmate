"""Controlled model; all execution, ownership and sandbox paths are production."""
import asyncio
import json
import os
from pathlib import Path
import signal
import faulthandler
import time
from functools import wraps
faulthandler.enable(all_threads=True)
from diagnostics import install
flush_diagnostics=install()

from common import guard,settings,fixture_blobs
guard('pg-tool-worker');fixture_blobs()
from agentscope.credential import CredentialBase
from agentscope.formatter import OpenAIChatFormatter
from agentscope.model import ChatModelBase,ChatResponse
from agentscope.message import TextBlock,ToolCallBlock
from pydantic import BaseModel
import workmate_runtime.assembly.agent as assembly
from workmate_runtime.assembly.agent import WorkerAgentAssembler
from workmate_runtime.roles.worker import serve_worker
from workmate_runtime.workspaces import WorkmateSandboxBash
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_product.persistence.repositories.workspace_residencies import ResidencyConflict
from proof import identity

E=Path('/evidence')
def timing(stage):
    with (E/'pg-worker-timing.jsonl').open('a') as file:
        file.write(json.dumps({'stage':stage,'monotonic':time.monotonic(),'time':time.time()})+'\n')
timing('worker_imported')
COMMAND="printf 'started\\n' >> /workspace/working/pg-started.txt; sleep 30; printf 'completed\\n' >> /workspace/working/pg-completed.txt"
prepare=WorkerAgentAssembler.prepare_start
async def observed_prepare(self,owner,snapshot,guard):
    timing('prepare_start_enter')
    result=await prepare(self,owner,snapshot,guard)
    timing('prepare_start_return')
    bound=self._bound[owner];lease=self.workspaces._ownership[bound.identity]
    assert self.workspaces.provider.name=='bubblewrap'
    (E/'pg-workspace.json').write_text(json.dumps({'path':str(lease.path),'task_id':owner.task_id,
        'session_id':owner.session_id,'worker_id':owner.worker_id,'epoch':owner.execution_epoch}))
    return result
WorkerAgentAssembler.prepare_start=observed_prepare
original=WorkmateSandboxBash.call
active_bash=False
attach=TaskResourceRegistry.attach_process
def observed_attach(self,owner,resource_id,process,**kwargs):
    attach(self,owner,resource_id,process,**kwargs)
    if active_bash:
        record=identity(process.pid)
        assert record is not None and record['pgrp']==process.pid
        record.update(resource_id=resource_id,task_id=owner.task_id,session_id=owner.session_id)
        with (E/'pg-tool-processes.jsonl').open('a') as file:file.write(json.dumps(record)+'\n')
TaskResourceRegistry.attach_process=observed_attach
@wraps(original)
async def observed(self,**kwargs):
    global active_bash
    assert kwargs['command']==COMMAND
    active_bash=True
    timing('bash_enter')
    with (E/'pg-tool-invocations.jsonl').open('a') as file:file.write(json.dumps({'command':COMMAND})+'\n')
    try:
        async for event in original(self,**kwargs):yield event
    finally:
        timing('bash_return')
        active_bash=False
        (E/'pg-tool-returned.json').write_text(json.dumps({'returned':True}))
WorkmateSandboxBash.call=observed
class Parameters(BaseModel):pass
class Model(ChatModelBase):
    def __init__(self):
        super().__init__(CredentialBase(),'fixture',Parameters(),stream=True)
        self.formatter=OpenAIChatFormatter()
    async def _call_api(self,*args,**kwargs):
        results=[block for message in kwargs.get('messages',[]) for block in
            (message.content if isinstance(getattr(message,'content',None),list) else [])
            if getattr(block,'type',None)=='tool_result']
        async def chunks():
            content=[TextBlock(text='tool returned')] if results else [ToolCallBlock(id='pg-tool',name='Bash',input=json.dumps({'command':COMMAND}))]
            yield ChatResponse(content=content,is_last=True)
        return chunks()
assembly.build_selected_model=lambda *_:Model()
async def main():
    shutdown=asyncio.Event();apps=[]
    for sig in (signal.SIGTERM,signal.SIGINT):asyncio.get_running_loop().add_signal_handler(sig,shutdown.set)
    def ready(app):
        timing('worker_ready')
        apps.append(app);(E/'pg-worker-ready.json').write_text(json.dumps({'pid':os.getpid(),'worker_id':app.service.worker_id}))
    configured=settings();configured.runtime.worker.lease_seconds=9
    try:await serve_worker(configured,shutdown,on_ready=ready)
    except BaseException as error:
        def leaves(error):
            if isinstance(error,BaseExceptionGroup):
                return [leaf for child in error.exceptions for leaf in leaves(child)]
            return [error]
        errors=leaves(error)
        expected=(shutdown.is_set() and len(errors)==1 and type(errors[0]) is ResidencyConflict
            and str(errors[0])=='Workspace registration or release lacks the exact live execution owner')
        (E/'pg-worker-exit.json').write_text(json.dumps({'classification':
            'expired_residency_release' if expected else 'unexpected',
            'errors':[{'type':type(e).__name__,'message':str(e)} for e in errors]}))
        raise
    finally:
        timing('worker_shutdown_finally')
        flush_diagnostics()
        (E/'pg-worker-state.json').write_text(json.dumps({'failures':apps[0].service.failures if apps else {},
            'ownership_remaining':len(apps[0].workspaces._ownership) if apps else None}))
asyncio.run(main())
