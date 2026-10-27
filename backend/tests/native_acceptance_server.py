"""Isolated browser acceptance: real auth/PG/Redis/Core, fixture model and sandbox.

Run from the repository root. Use a Python environment with the Runtime and
Product test dependencies installed (including AgentScope, pytest, asyncpg and
uvicorn). Set WORKMATE_ACCEPTANCE_PYTHON to that interpreter;
runtime/.venv/bin/python is one possible interpreter. This script inserts
runtime/, runtime/tests/,
backend/, backend/tests/, packages/contracts/src and packages/product/src into
sys.path, so no additional PYTHONPATH is needed.

1. Start a dedicated PostgreSQL instance on 127.0.0.1:55439 with a local postgres
   role, then create the isolated database (never point this harness at another
   environment or run the legacy Product dispatcher against this database):

       createdb -h 127.0.0.1 -p 55439 -U postgres workmate_native_acceptance

   PostgreSQL must permit this local test connection without a password. The
   harness explicitly creates its isolated Product tables before role startup;
   this is not migration/deployment acceptance.

2. Start a dedicated Redis instance (never reuse the deployment/business Redis):

       redis-server --bind 127.0.0.1 --port 55440 --save '' --appendonly no

   Keep this process running in its own terminal. The harness accepts only this
   dedicated loopback port and an explicit acceptance namespace. It never reads
   WORKMATE_COORDINATION_REDIS_URL from the developer environment or .env.

3. Check the isolated target configuration, then start the API and native Worker:

       export WORKMATE_ACCEPTANCE_REDIS_URL=redis://127.0.0.1:55440/0
       export WORKMATE_ACCEPTANCE_REDIS_NAMESPACE=acceptance-browser
       "$WORKMATE_ACCEPTANCE_PYTHON" backend/tests/native_acceptance_server.py --check

       "$WORKMATE_ACCEPTANCE_PYTHON" backend/tests/native_acceptance_server.py

   They listen on 127.0.0.1:18180 and use only workmate_native_acceptance. The
   script does not read .env; its generated configuration, artifacts and
   Workspace fixtures live in /tmp/workmate-native-acceptance.

4. In another shell, from frontend/, build and run Next.js with the same
   explicit Product URL for both REST rewrites and the streaming proxy:

       PRODUCT_API_URL=http://127.0.0.1:18180 npm run build
       PRODUCT_API_URL=http://127.0.0.1:18180 npm run start -- --hostname 127.0.0.1 --port 13100

   Open http://127.0.0.1:13100/login. Login is acceptance@example.com with
   password Acceptance-only-2026! (local fixture credentials only). Submit a
   message to the default assistant, wait for completion, refresh the page and
   reopen the conversation to verify persisted history without duplicate text.

   Additional control paths (use the standard approval profile):
   - Send "[approval] Verify approval"; the real Core permission engine parks on
     acceptance_confirm. Confirm or reject in the browser, then refresh history.
     Confirmation runs only a fixed-text tool; rejection must not invoke it.
     API logs print acceptance_confirm instance_calls/process_total only when
     the tool executes; rejected calls must not increment the process total.
   - Send "[slow] Verify cancellation"; the model waits up to 120 seconds. Click Stop
     while running. For queue coverage, submit another ordinary message while
     the slow Task runs, then Stop it and verify the queued Task completes.
   - Attach a UTF-8 .txt file and send "[artifact] version-one". Production
     materializes the upload; the fixture model reads that Task's input and
     writes output/report.md. Preview and download the resulting Artifact.
     In the same conversation attach different text and send "[artifact]
     version-two"; inspect/download both task versions, including after refresh.
     The first Artifact must retain its first bytes after the same workspace
     filename is overwritten. Inputs are limited to 1 MiB UTF-8 text per file.
   No Product events, Artifacts or approval responses are synthesized by the harness.

5. Stop Next and the harness with Ctrl-C and wait for Worker shutdown BEFORE
   dropping the isolated database or deleting its files:

       dropdb -h 127.0.0.1 -p 55439 -U postgres workmate_native_acceptance
       rm -rf /tmp/workmate-native-acceptance

   Stop the dedicated PostgreSQL and Redis instances only if no other acceptance
   process uses them. The harness never runs Redis FLUSH or deployment cleanup.
   A restart without cleanup preserves the fixture identity, permissions and
   conversation history; Redis retention is bounded and PG snapshots restore it.

The deterministic model emits complete text in a non-final ChatResponse and
repeats it in the final response: pinned Core emits text events only from the
non-final frame and uses the final frame for completed state. Sandbox attestation
and Workspace are fixtures. Fixed builtin Skill materialization and controlled attachment/report file I/O
run locally through fixture backend/model seams; the model invokes only the explicit no-side-effect fixture tool. Passing this harness proves
neither operating-system isolation nor real model/provider/tool execution.
"""
from pathlib import Path
import os
import sys


def acceptance_redis_config(environ):
    """Reject implicit deployment targets before imports, file writes or I/O."""
    import re
    from urllib.parse import urlsplit
    raw=environ.get('WORKMATE_ACCEPTANCE_REDIS_URL', '')
    namespace=environ.get('WORKMATE_ACCEPTANCE_REDIS_NAMESPACE', '')
    try:
        target=urlsplit(raw)
        valid=(target.scheme=='redis' and target.hostname in {'127.0.0.1','localhost','::1'}
            and target.port==55440 and target.path in {'','/0'}
            and not target.username and not target.password and not target.query and not target.fragment)
    except ValueError:
        valid=False
    if not valid:
        raise ValueError('WORKMATE_ACCEPTANCE_REDIS_URL must target dedicated local Redis on port 55440, DB 0')
    if not re.fullmatch(r'acceptance-[A-Za-z0-9_-]{1,48}',namespace):
        raise ValueError('WORKMATE_ACCEPTANCE_REDIS_NAMESPACE must be an explicit acceptance- namespace')
    return raw,namespace


try:
    ACCEPTANCE_REDIS_URL,ACCEPTANCE_NAMESPACE=acceptance_redis_config(os.environ)
except ValueError as error:
    raise SystemExit(str(error)) from error
if '--check' in sys.argv[1:]:
    import json
    print(json.dumps({'postgres':'127.0.0.1:55439/workmate_native_acceptance',
        'redis':ACCEPTANCE_REDIS_URL,'namespace':ACCEPTANCE_NAMESPACE,
        'env_file':False,'network_checked':False}))
    raise SystemExit(0)

# Catalog construction must never inherit a developer's GitHub credential.
os.environ.pop('GITHUB_TOKEN',None)
ROOT=Path(__file__).resolve().parents[2]
for relative in ('runtime','runtime/tests','backend','backend/tests','packages/contracts/src','packages/product/src'):
    sys.path.insert(0,str(ROOT/relative))
DATA=Path('/tmp/workmate-native-acceptance')
DATA.mkdir(parents=True,exist_ok=True)
CONFIG=DATA/'workmate.yaml'
CONFIG.write_text('''product:
  models:
    default: acceptance
    profiles:
      acceptance:
        type: openai
        model: acceptance-fixture
        api_key_env: WORKMATE_ACCEPTANCE_MODEL_KEY
''')
os.environ.update(DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:55439/workmate_native_acceptance',
    JWT_SECRET='local-browser-acceptance-secret-at-least-32-characters',
    REGISTRATION_MODE='closed',WORKMATE_WORKER_STORAGE_NODE_ID='acceptance',
    WORKMATE_COORDINATION_REDIS_URL=ACCEPTANCE_REDIS_URL,WORKMATE_COORDINATION_NAMESPACE=ACCEPTANCE_NAMESPACE,
    WORKMATE_CONFIG_FILE=str(CONFIG),WORKMATE_ACCEPTANCE_MODEL_KEY='fixture-only',
    ARTIFACT_STORAGE_DIR=str(DATA/'artifacts'),RUNTIME_INTERNAL_TOKEN='fixture-only',
    RUNTIME_BASE_URL='http://127.0.0.1:18999',SKILLHUB_CN_ENABLED='false')
from app.config import Settings, get_settings
Settings.model_config['env_file']=None

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from app.auth import hash_password
from app.db import get_sessionmaker, get_engine
from workmate_product.persistence.models import User
from app.services.bootstrap import ensure_user_scopes
import app.main as native
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.execution.composition import compose_worker
from test_worker_workspaces import StubWorkspace
from test_core_engine import ScriptModel, ConfirmTool
import workmate_runtime.assembly.agent as assembly
import workmate_runtime.assembly.sandbox_proof as proof

acceptance_logger=logging.getLogger('workmate.acceptance.fixture')
acceptance_tool_executions=0


class AcceptanceConfirmTool(ConfirmTool):
    name: str = 'acceptance_confirm'
    description: str = 'Side-effect-free acceptance tool: returns fixed text after confirmation, without writing files or accessing external systems.'
    safety_ask = True

    async def __call__(self, **kwargs):
        from agentscope.tool import ToolChunk
        global acceptance_tool_executions
        self.calls += 1
        acceptance_tool_executions += 1
        acceptance_logger.info('acceptance_confirm executed: instance_calls=%d process_total=%d',
            self.calls,acceptance_tool_executions)
        yield ToolChunk(content=[TextBlock(text='Acceptance confirmation executed without external side effects.')],is_last=True)


original_toolkit=assembly.Toolkit
acceptance_tools=[]
def fixture_toolkit(*args,**kwargs):
    tool=AcceptanceConfirmTool()
    acceptance_tools.append(tool)
    kwargs['tools']=[*kwargs.get('tools',[]),tool]
    return original_toolkit(*args,**kwargs)
assembly.Toolkit=fixture_toolkit


_assembling_model=ContextVar('acceptance_assembling_model',default=None)


@contextmanager
def fixture_file(workspace,relative,*,write=False):
    """Only regular files below this exact fixture Workspace; no symlinks."""
    import stat
    parts=relative.split('/')
    if (len(parts)<2 or parts[0] not in {'input','output'}
            or any(p in {'','.','..'} or '\\' in p for p in parts)):
        raise RuntimeError('Unsafe acceptance file path')
    root=Path(workspace.host_workdir)
    if not root.is_relative_to(DATA/'workspaces') or root.resolve()!=root:
        raise RuntimeError('Foreign acceptance Workspace')
    fds=[]
    try:
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        fds.append(fd)
        for part in parts[:-1]:
            if write:
                try: os.mkdir(part,dir_fd=fd)
                except FileExistsError: pass
            fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            fds.append(fd)
        flags=os.O_WRONLY|os.O_CREAT if write else os.O_RDONLY
        fd=os.open(parts[-1],flags|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=fd)
        fds.append(fd)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise RuntimeError('Acceptance files must be unlinked regular files')
        if write: os.ftruncate(fd,0)
        yield fd
    finally:
        for fd in reversed(fds): os.close(fd)


async def fixture_attachment_write(workspace,owner,path,stream):
    prefix='/workspace/input/'+owner.task_id+'/'
    if not path.startswith(prefix) or '/' in path[len(prefix):]:
        raise RuntimeError('Only current Task attachment paths are accepted')
    with fixture_file(workspace,path.removeprefix('/workspace/'),write=True) as fd:
        async for data in stream:
            while data: data=data[os.write(fd,data):]


class AcceptanceModel(ScriptModel):
    def report(self,prompt):
        import re
        # Only paths emitted by production attachment materialization for the
        # currently bound Task may be read; user-supplied paths are rejected.
        paths=re.findall(r'^- (input/[^\n]+)$',prompt,re.MULTILINE)
        # Match the localized attachment marker emitted by the production materializer.
        report=['# Acceptance report',prompt.split('[平台附件]')[0].strip()]
        for relative in paths:
            if not relative.startswith('input/'+self.fixture_owner.task_id+'/'):
                raise RuntimeError('Attachment belongs to another Task')
            with fixture_file(self.fixture_workspace,relative) as fd:
                data=os.read(fd,1024*1024+1)
            if len(data)>1024*1024: raise RuntimeError('Acceptance text attachment too large')
            report.extend(['## '+relative.rsplit('/',1)[-1],data.decode('utf-8')])
        with fixture_file(self.fixture_workspace,'output/report.md',write=True) as fd:
            data=('\n\n'.join(report)+'\n').encode()
            while data:
                data=data[os.write(fd,data):]
        acceptance_logger.info('acceptance report generated: attachment_count=%d',len(paths))


    async def _call_api(self,*args,**kwargs):
        import json
        messages=[m.model_dump(mode='json') if hasattr(m,'model_dump') else m
            for m in kwargs.get('messages',[])]
        # Core messages retain roles and tool-result blocks. Ignore previous Tasks'
        # markers by looking only at the latest user message and its successors.
        latest=max((i for i,m in enumerate(messages) if m.get('role')=='user'),default=-1)
        blocks=messages[latest].get('content','') if latest>=0 else ''
        prompt=('\n'.join(b.get('text','') for b in blocks if isinstance(b,dict))
            if isinstance(blocks,list) else str(blocks))
        settled=any(m.get('role')=='tool' or any(isinstance(b,dict) and b.get('type')=='tool_result'
            for b in (m.get('content') if isinstance(m.get('content'),list) else [])) for m in messages[latest+1:])
        async def chunks():
            if '[approval]' in prompt and not settled:
                import uuid
                content=[ToolCallBlock(id='acceptance-'+uuid.uuid4().hex,name='acceptance_confirm',input='{}')]
            else:
                if '[slow]' in prompt:
                    # Cancellable model wait: native Stop must join the real Core
                    # task; subsequent queued Tasks should then be admitted.
                    await asyncio.sleep(120)
                if '[artifact]' in prompt:
                    self.report(prompt)
                full=('Acceptance reply: the approval flow has finished.' if '[approval]' in prompt else
                    'Acceptance reply: message received. The native execution path persists the conversation and its history.')
                content=[TextBlock(text=full)]
            # Core emits deltas only for non-final frames. The final frame
            # repeats the complete response for persistent Core state.
            for last in (False,True):
                await asyncio.sleep(.15)
                yield ChatResponse(content=content,is_last=last)
        return chunks()
def fixture_model(*args):
    model=AcceptanceModel([])
    _assembling_model.set(model)
    return model
assembly.build_selected_model=fixture_model
actual_attest=proof.attest_task_sandbox
async def fixture_attest(binding,owner,sessions):
    original=binding.backend.exec_shell
    async def probe(*args,**kwargs):
        return SimpleNamespace(exit_code=0,stdout=b'pid:[1]')
    binding.backend.exec_shell=probe
    try:
        await actual_attest(binding,owner,sessions)
    finally:
        binding.backend.exec_shell=original
proof.attest_task_sandbox=fixture_attest

app=native.build_app()
product_lifespan=app.router.lifespan_context
@asynccontextmanager
async def lifespan(web):
    async with AsyncExitStack() as stack:
        from redis.asyncio import Redis
        from redis.asyncio.retry import Retry
        from redis.backoff import NoBackoff
        from workmate_product.coordination.command_queue import RedisCommandQueue
        from workmate_product.realtime.composition import create_realtime_log
        settings=get_settings()
        queue_client=await stack.enter_async_context(Redis.from_url(
            settings.coordination_redis_url,decode_responses=True,
            socket_connect_timeout=5,socket_timeout=5))
        realtime_client=await stack.enter_async_context(Redis.from_url(
            settings.coordination_redis_url,decode_responses=True,
            retry=Retry(NoBackoff(),0),retry_on_timeout=False,
            socket_connect_timeout=5,socket_timeout=5))
        queue=RedisCommandQueue(queue_client,namespace=settings.coordination_namespace)
        realtime_log=create_realtime_log(realtime_client,
            namespace=settings.coordination_namespace,policy=settings.realtime)
        from workmate_product.persistence.models import Base
        async with get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await stack.enter_async_context(product_lifespan(web))
        async with get_sessionmaker().begin() as db:
            user=await db.get(User,'native-browser-acceptance')
            if user is None:
                user=User(id='native-browser-acceptance',email='acceptance@example.com',
                    normalized_email='acceptance@example.com',display_name='Acceptance User',
                    password_hash=hash_password('Acceptance-only-2026!'))
                db.add(user)
                await db.flush()
                await ensure_user_scopes(db,user,first_user=True)
            else:
                # Keep the existing identity/scopes across restarts, including
                # the earlier fixture address rejected by EmailStr at login.
                user.email=user.normalized_email='acceptance@example.com'
                user.password_hash=hash_password('Acceptance-only-2026!')
        worker=compose_worker(get_engine(),RuntimeSettings(reme_enabled=False,
            coordination_redis_url=settings.coordination_redis_url,coordination_namespace=settings.coordination_namespace,
            command_queue=settings.command_queue,realtime=settings.realtime),storage_node_id='acceptance',
            workspace_dir=DATA/'workspaces',artifact_dir=DATA/'artifacts',max_file_bytes=1024*1024,
            max_files=10,workspace_factory=StubWorkspace,drain_seconds=0,
            command_queue=queue,realtime_log=realtime_log)
        from workmate_runtime.assembly.skills import _MATERIALIZE_SCRIPT
        constructor=worker.workspaces._task_backend
        def backend(*args):
            instance=constructor(*args)
            workspace,owner=args
            model=_assembling_model.get()
            if model is None: raise RuntimeError('Fixture model binding missing')
            model.fixture_workspace,model.fixture_owner=workspace,owner
            async def write_stream(path,stream):
                await fixture_attachment_write(workspace,owner,path,stream)
            instance.write_stream=write_stream
            async def execute(command,raw):
                if command[2:4]!=['-c',_MATERIALIZE_SCRIPT]:
                    raise RuntimeError('Acceptance harness only permits builtin Skill materialization')
                destination=DATA/'materialized'/command[5].removeprefix('/workspace/')
                process=await asyncio.create_subprocess_exec(sys.executable,'-I','-c',command[3],
                    command[4],str(destination),command[6],stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                _,error=await process.communicate(raw)
                if process.returncode:
                    raise RuntimeError('Acceptance Skill materialization failed')
                return SimpleNamespace(exit_code=0)
            instance._exec_with_input=execute
            return instance
        worker.workspaces._task_backend=backend
        shutdown=asyncio.Event()
        running=asyncio.create_task(worker.serve(shutdown))
        try:
            async with asyncio.timeout(20):
                await worker.service.ready.wait()
            yield
        finally:
            shutdown.set()
            await running
app.router.lifespan_context=lifespan

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=18180)
