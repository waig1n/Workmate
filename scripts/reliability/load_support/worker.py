from collections import deque
import asyncio,json,os,signal,sys,time,uuid,re,shlex
from pathlib import Path
from common import guard,settings,fixture_blobs,chunk,parameters

guard('worker-'+sys.argv[1]); fixture_blobs()
from agentscope.credential import CredentialBase
from agentscope.formatter import OpenAIChatFormatter
from agentscope.model import ChatModelBase,ChatResponse
from agentscope.message import TextBlock, ToolCallBlock
from file_workload import file_content
from approval import fixture_state
from mix import fixture_kind
from functools import wraps
from workmate_runtime.workspaces import WorkmateSandboxWrite,WorkmateSandboxBash
original_write = WorkmateSandboxWrite.call
@wraps(original_write)
async def observed_write(self, **kwargs):
    with Path("/evidence/write-effects-"+sys.argv[1]+".jsonl").open("a") as trace:
        trace.write(json.dumps({"file_path":kwargs["file_path"]})+"\n")
    return await original_write(self, **kwargs)
WorkmateSandboxWrite.call = observed_write
original_bash = WorkmateSandboxBash.call
@wraps(original_bash)
async def observed_bash(self, **kwargs):
    match=re.search(r'Fixture_SKILL_KEY=([0-9a-f]{32})',kwargs['command'])
    if match:
        with Path('/evidence/write-effects-'+sys.argv[1]+'.jsonl').open('a') as trace:
            trace.write(json.dumps({'file_path':'/workspace/output/'+match[1]+'.txt','tool':'Bash','fixture':'skill'})+'\n')
    async for chunk in original_bash(self, **kwargs):
        yield chunk
WorkmateSandboxBash.call = observed_bash
from pydantic import BaseModel
import workmate_runtime.assembly.agent as assembly
from workmate_runtime.roles.worker import serve_worker
if os.environ.get('WORKMATE_LOAD_WORKLOAD') in ('rag','mixed'):
    import workmate_runtime.roles.worker as worker_role
    from rag_fixture import resources
    worker_role.configured_knowledge_resources=resources

class Parameters(BaseModel): pass
class Model(ChatModelBase):
    def __init__(self):
        # The file fixture deliberately supplies up to 1 MiB as Write input.
        # It exercises file transport, not provider context compression.
        super().__init__(CredentialBase(),'fixture',Parameters(),stream=True,context_size=1048576)
        self.formatter=OpenAIChatFormatter()
        self.key=uuid.uuid4().hex
        self.calls=0
    async def _call_api(self,*args,**kwargs):
        async def chunks():
            key,resumed=fixture_state(kwargs.get('messages',[]),self.key)
            count,interval=parameters(); marks=[]; body=''
            kind=fixture_kind(kwargs.get('messages',[]),os.environ.get('WORKMATE_LOAD_WORKLOAD','text'))
            self.calls+=1
            if kind=='rag' and not resumed:
                yield ChatResponse(content=[ToolCallBlock(id='write-'+key,name='search_knowledge',input='{"query":"annual leave"}')],is_last=True)
                return
            if kind in ('file','approval','skill','mcp') and not resumed:
                size=1024 if os.environ['WORKMATE_LOAD_MODE']=='smoke' else 1048576
                if kind=='mcp':
                    yield ChatResponse(content=[ToolCallBlock(id='write-'+key,name='mcp__fixture__generate',input=json.dumps({'key':key,'size':size}))],is_last=True)
                    return
                if kind=='skill':
                    code="import glob,runpy,sys,os; paths=glob.glob('/workspace/skills/*/fixture-skill/*/scripts/run.py'); assert paths; p=max(paths,key=os.path.getmtime); sys.argv=[p,"+repr(key)+","+repr(str(size))+"]; runpy.run_path(p,run_name='__main__')"
                    command='Fixture_SKILL_KEY='+key+' python3 -c '+shlex.quote(code)
                    yield ChatResponse(content=[ToolCallBlock(id='write-'+key,name='Bash',input=json.dumps({'command':command}))],is_last=True)
                    return
                yield ChatResponse(content=[ToolCallBlock(id='write-'+key,name='Write',input=json.dumps({
                    'file_path':'/workspace/output/'+key+'.txt','content':file_content(key,size)}))],is_last=True)
                return
            report={'key':key,'pid':os.getpid(),'worker':sys.argv[1],'marks':marks,'chunks':count,'interval':interval}
            try:
                nominal=time.monotonic_ns()
                for i in range(count):
                    # No catch-up bursts: delay after downstream has consumed prior yield.
                    if i: await asyncio.sleep(interval)
                    value=chunk(key,i); body+=value
                    row={'index':i,'nominal_ns':nominal+int(i*interval*1e9),'emit_ns':time.monotonic_ns()}; marks.append(row)
                    with Path('/evidence/emission-'+key+'.jsonl').open('a') as trace: trace.write(json.dumps(row)+'\n')
                    yield ChatResponse(content=[TextBlock(text=value)],is_last=False)
                    row['resumed_ns']=time.monotonic_ns()
                report['final_emit_ns']=time.monotonic_ns()
                yield ChatResponse(content=[TextBlock(text=body)],is_last=True)
                report['final_resumed_ns']=time.monotonic_ns()
            finally:
                report['closed_ns']=time.monotonic_ns()
                Path('/evidence/model-'+key+'.json').write_text(json.dumps(report))
        return chunks()
assembly.build_selected_model=lambda *_:Model()

async def main():
    shutdown=asyncio.Event(); lag=deque(maxlen=240); apps=[]
    for sig in (signal.SIGTERM,signal.SIGINT): asyncio.get_running_loop().add_signal_handler(sig,shutdown.set)
    async def clock():
        while not shutdown.is_set():
            before=time.monotonic_ns(); await asyncio.sleep(.25)
            lag.append({'ns':time.monotonic_ns(),'lag_ns':time.monotonic_ns()-before-250000000})
    def ready(app):
        apps.append(app)
        Path('/evidence/worker-'+sys.argv[1]+'-ready.json').write_text(json.dumps({'pid':os.getpid(),'worker_id':app.service.worker_id}))
    timer=asyncio.create_task(clock())
    try: await serve_worker(settings(),shutdown,on_ready=ready)
    finally:
        shutdown.set(); await timer
        Path('/evidence/worker-'+sys.argv[1]+'-state.json').write_text(json.dumps({'lag':list(lag),'failures':apps[0].service.failures if apps else {'startup':'not ready'},'ownership_remaining':len(apps[0].workspaces._ownership) if apps else None}))
    assert apps and not apps[0].service.failures
asyncio.run(main())
