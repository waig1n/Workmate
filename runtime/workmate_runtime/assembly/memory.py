"""Worker-owned ReMe namespaces: one live application per local vault.

The OS lock covers startup, cron/watchers, in-flight jobs and close/dump. A
handoff marker lets another local Worker request an idle cached application to
close. Database affinity prevents silently opening a different node's empty vault.
"""
import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
import fcntl
import logging
import os
from pathlib import Path
import time
import uuid

from agentscope.message import Msg, HintBlock
from agentscope.middleware import ReMeMiddleware
from agentscope.middleware._longterm_memory._reme._tools import _MemorySearchTool
from sqlalchemy import text
from workmate_runtime.middlewares import reme_workspace_dir

logger = logging.getLogger(__name__)


class MemoryUnavailable(RuntimeError):
    pass


async def bind_memory_node(sessions, user_id, agent_id, node_id):
    """Durable namespace placement, independent of Conversation placement."""
    async with sessions.begin() as db:
        await db.execute(text('''INSERT INTO runtime_memory_namespaces(user_id,agent_id,storage_node_id)
            VALUES (:user,:agent,:node) ON CONFLICT(user_id,agent_id) DO NOTHING'''),
            dict(user=user_id,agent=agent_id,node=node_id))
        actual=await db.scalar(text('''SELECT storage_node_id FROM runtime_memory_namespaces
            WHERE user_id=:user AND agent_id=:agent'''),dict(user=user_id,agent=agent_id))
        if actual != node_id:
            raise MemoryUnavailable('Memory namespace belongs to another storage node')


@dataclass
class _Namespace:
    path: Path
    fd: int
    middleware: ReMeMiddleware
    mode: str
    references: int = 0
    writes: set = field(default_factory=set)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class WorkerMemoryManager:
    def __init__(self,basedir,chat_model,embedding_model=None,*,bind_node,
                 max_instances=256,wait_seconds=1.0,poll_seconds=.05,startup_seconds=15):
        if not callable(bind_node) or max_instances<1 or wait_seconds<=0 or poll_seconds<=0 or startup_seconds<=0:
            raise ValueError('Memory lifecycle requires bounded waits and trusted placement')
        self.basedir,self.chat_model,self.embedding_model=str(basedir),chat_model,embedding_model
        self.bind_node=bind_node
        self.max_instances,self.wait_seconds,self.poll_seconds=max_instances,wait_seconds,poll_seconds
        self.startup_seconds=startup_seconds
        self.instances={}
        self.status={}
        self._lock=asyncio.Lock()
        self._monitor=None
        self._closed=False
        self._quarantined=[]

    def health(self):
        return {'quarantined':len(self._quarantined),'configured':self.chat_model is not None,'instances':len(self.instances),
            'namespace_states':dict(self.status),'pending_writes':sum(len(n.writes) for n in self.instances.values())}

    async def _stop_app(self,middleware):
        app=middleware._app
        if app is None:
            return
        components=list(app._started_components)
        tasks=set(getattr(middleware,'_workmate_owned_tasks',()))
        tasks.update(getattr(c,attr,None) for c in components for attr in ('_task','_embedding_backfill_task'))
        tasks.discard(None)
        middleware._workmate_owned_tasks=tasks
        # A cancelled start does not mark Application started, so close() alone
        # is a no-op. Drive its recorded reverse startup ledger directly then.
        if getattr(app,'_is_started',False):
            await app.close()
        else:
            await app._close()
        if any(not task.done() for task in tasks):
            raise MemoryUnavailable('Memory background shutdown is unproven')
        middleware._started=False

    async def _close_namespace(self,key,entry):
        # Called only under manager lock after all borrower/jobs references end.
        # If teardown fails, keep the OS lock: releasing would allow a second
        # writer while the first app may still own background work.
        await self._stop_app(entry.middleware)
        fcntl.flock(entry.fd,fcntl.LOCK_UN)
        os.close(entry.fd)
        self.instances.pop(key,None)

    async def _watch_handoffs(self):
        while True:
            await asyncio.sleep(self.poll_seconds)
            async with self._lock:
                for key,entry in tuple(self.instances.items()):
                    marker=entry.path/'.workmate-handoff'
                    if entry.references==0 and marker.exists():
                        try:
                            token=marker.read_text()
                            self._write_marker(entry.path/'.workmate-handoff-accepted',token)
                            await self._close_namespace(key,entry)
                            self.status[key]='handed_off'
                        except Exception:
                            self.status[key]='close_failed'
                            logger.exception('Memory namespace teardown failed; retaining ownership')

    async def _start(self,path,fd):
        last=None
        for embedding in ([self.embedding_model,None] if self.embedding_model is not None else [None]):
            middleware=ReMeMiddleware(workspace_dir=str(path),parameters=ReMeMiddleware.Parameters(
                chat_model=self.chat_model,embedding_model=embedding,mode='both'))
            try:
                await middleware._ensure_started()
                return _Namespace(path,fd,middleware,'vector' if embedding is not None else 'bm25')
            except BaseException as error:
                last=error
                cleanup=asyncio.create_task(self._stop_app(middleware))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
                except BaseException:
                    # The fd remains held until process shutdown if proof fails.
                    self._quarantined.append((fd,middleware))
                    raise
                if isinstance(error,asyncio.CancelledError):
                    raise
        raise MemoryUnavailable('Memory initialization failed') from last

    @staticmethod
    def _write_marker(path,token):
        temporary=path.with_name(path.name+'.'+uuid.uuid4().hex)
        try:
            temporary.write_text(token)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _marker_token(path):
        try:
            return path.read_text()
        except FileNotFoundError:
            return None

    async def acquire(self,user_id,agent_id):
        token=uuid.uuid4().hex
        key=reme_workspace_dir(self.basedir,user_id,agent_id)
        try:
            # Includes placement, contention on the manager lock and startup,
            # not merely the nonblocking flock polling loop. Timeout cancels
            # and joins startup cleanup before returning degraded availability.
            async with asyncio.timeout(self.wait_seconds+self.startup_seconds):
                return await self._acquire(user_id,agent_id,token)
        except TimeoutError as error:
            self.status[key]='initialization_timeout'
            raise MemoryUnavailable('Memory initialization timed out') from error
        finally:
            # A timed-out borrower must not make an idle owner retire later
            # with nobody remaining to take over its watcher/dream application.
            marker=Path(key)/'.workmate-handoff'
            if self._marker_token(marker)==token:
                marker.unlink(missing_ok=True)

    async def _acquire(self,user_id,agent_id,token):
        if self.chat_model is None:
            raise MemoryUnavailable('Memory chat model is not configured')
        key=reme_workspace_dir(self.basedir,user_id,agent_id)
        try:
            await self.bind_node(user_id,agent_id)
        except Exception as error:
            self.status[key]='placement_unavailable'
            raise MemoryUnavailable('Memory placement unavailable') from error
        deadline=time.monotonic()+self.wait_seconds
        handoff_accepted=False
        while True:
            async with self._lock:
                if self._closed:
                    raise MemoryUnavailable('Memory manager is closed')
                if self._monitor is None:
                    self._monitor=asyncio.create_task(self._watch_handoffs(),name='workmate-memory-handoffs')
                entry=self.instances.get(key)
                if entry is not None:
                    # Give waiting processes a chance once active calls finish.
                    if not (entry.path/'.workmate-handoff').exists():
                        self.instances.pop(key)
                        self.instances[key]=entry
                        entry.references+=1
                        return entry
                else:
                    if len(self.instances)>=self.max_instances:
                        idle=next(((k,n) for k,n in self.instances.items() if n.references==0),None)
                        if idle:
                            await self._close_namespace(*idle)
                    if len(self.instances)<self.max_instances:
                        path=Path(key).absolute()
                        if path.resolve()!=path:
                            raise MemoryUnavailable('Memory namespace path contains a symlink')
                        path.mkdir(parents=True,exist_ok=True)
                        if path.resolve()!=path:
                            raise MemoryUnavailable('Memory namespace path changed')
                        fd=os.open(path/'.workmate-owner.lock',os.O_CREAT|os.O_RDWR|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
                        try:
                            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        except BlockingIOError:
                            os.close(fd)
                            self._write_marker(path/'.workmate-handoff',token)
                        else:
                            try:
                                (path/'.workmate-handoff').unlink(missing_ok=True)
                                (path/'.workmate-handoff-accepted').unlink(missing_ok=True)
                                entry=await self._start(path,fd)
                            except BaseException:
                                if not any(held==fd for held,_ in self._quarantined):
                                    os.close(fd)
                                self.status[key]='initialization_failed'
                                raise
                            entry.references=1
                            self.instances[key]=entry
                            self.status[key]=entry.mode
                            return entry
            if time.monotonic()>=deadline:
                if (not handoff_accepted and
                        self._marker_token(Path(key)/'.workmate-handoff-accepted')==token):
                    # The old owner is already closing. Wait for that actual
                    # lifecycle completion instead of abandoning its handoff.
                    handoff_accepted=True
                    deadline=time.monotonic()+self.startup_seconds
                else:
                    self.status[key]='busy'
                    raise MemoryUnavailable('Memory namespace is busy; retrieval is unavailable')
            await asyncio.sleep(self.poll_seconds)

    async def release(self,entry):
        async with self._lock:
            if entry.references<=0:
                raise RuntimeError('Memory borrower released twice')
            entry.references-=1

    def write(self,entry,messages,session_id):
        # Copy before detaching; a resumed Core mutates its own context.
        frozen=[m.model_copy(deep=True) for m in messages]
        if self._closed:
            return
        entry.references+=1
        async def run():
            try:
                async with entry.write_lock:
                    await entry.middleware._write_back(frozen,session_id)
            finally:
                entry.writes.discard(asyncio.current_task())
                await self.release(entry)
        task=asyncio.create_task(run(),name=f'workmate-memory-write:{session_id}')
        entry.writes.add(task)

    async def close(self):
        self._closed=True
        if self._monitor is not None:
            self._monitor.cancel()
            await asyncio.gather(self._monitor,return_exceptions=True)
        tasks=[task for entry in self.instances.values() for task in entry.writes]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)
        async with self._lock:
            for fd,middleware in tuple(self._quarantined):
                await self._stop_app(middleware)
                fcntl.flock(fd,fcntl.LOCK_UN)
                os.close(fd)
                self._quarantined.remove((fd,middleware))
            for key,entry in tuple(self.instances.items()):
                if entry.references:
                    raise RuntimeError('Reply borrowers must finish before memory manager shutdown')
                await self._close_namespace(key,entry)

    def middleware(self,user_id,agent_id,*,task_input_ids,authorize,authorize_preparation):
        return WorkerMemoryMiddleware(self,user_id,agent_id,task_input_ids,authorize,authorize_preparation)


class _WorkerMemorySearch(_MemorySearchTool):
    is_state_injected=True
    async def __call__(self,query,limit=None,_agent_state=None):
        return await super().__call__(query,limit)


class WorkerMemoryMiddleware(ReMeMiddleware):
    """Per-Reply state; shared vault lifetime belongs exclusively to manager."""
    def __init__(self,manager,user_id,agent_id,task_input_ids,authorize,authorize_preparation):
        super().__init__(parameters=ReMeMiddleware.Parameters(mode='both'))
        self.manager,self.user_id,self.agent_id=manager,user_id,agent_id
        self.authorize,self.authorize_preparation=authorize,authorize_preparation
        if not isinstance(task_input_ids,(tuple,list)):
            raise ValueError('Memory requires explicit current Task input message IDs')
        self.task_input_ids=tuple(task_input_ids)
        if not self.task_input_ids or any(not isinstance(value,str) or not value for value in self.task_input_ids) or len(set(self.task_input_ids))!=len(self.task_input_ids):
            raise ValueError('Memory requires unique current Task input message IDs')
        self._written=False
        self._reply_active=False
        self._inside_reasoning=False
        self._owned_hints=set()

    def _build_memory_message(self,memories):
        message=super()._build_memory_message(memories)
        self._owned_hints.add(id(message))
        return message

    def _remove_hints(self,agent):
        agent.state.context[:]=[message for message in agent.state.context
            if id(message) not in self._owned_hints]
        self._owned_hints.clear()

    async def list_tools(self):
        return [_WorkerMemorySearch(self)]

    async def _search(self,query,*,limit=None):
        if not self._reply_active:
            raise MemoryUnavailable('Memory reply is not active')
        check=self.authorize if self._inside_reasoning else self.authorize_preparation
        await check()
        entry=await self.manager.acquire(self.user_id,self.agent_id)
        try:
            # Acquisition may wait for a peer; recheck ownership before I/O.
            await check()
            result=await entry.middleware._search(query,limit=limit)
            await check()
            return result
        finally:
            await self.manager.release(entry)

    async def _write_back(self,messages,session_id):
        # Upstream writes per invocation, which splits HITL exchanges and can
        # write before approval. This adapter owns the complete Task boundary.
        return

    def _completed_exchange(self,agent,reply_id):
        messages=[m for m in agent.state.context if isinstance(m,Msg)]
        users=[m for m in messages if m.role=='user' and m.id in self.task_input_ids]
        if tuple(m.id for m in users)!=self.task_input_ids:
            return []  # Missing/compacted anchors never authorize history fallback.
        answer=[m for m in messages if m.role=='assistant' and m.name==agent.name
                and m.id==reply_id]
        selected=[]
        for message in users+answer:
            clean=message.model_copy(deep=True)
            clean.content=[block for block in clean.content if not isinstance(block,HintBlock)]
            if clean.content:
                selected.append(clean)
        if not any(m.role=='assistant' and m.get_text_content() for m in selected):
            return []
        return selected

    async def _write_completed(self,messages,session_id):
        if not self._reply_active or asyncio.current_task().cancelling():
            return
        entry=None
        try:
            await self.authorize()
            entry=await self.manager.acquire(self.user_id,self.agent_id)
            await self.authorize()
            # write owns its own reference before this operation releases it.
            self.manager.write(entry,messages,session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Auxiliary memory contention must not turn a successful Reply into
            # a failed product Task. Never bypass the exclusive namespace lock.
            logger.warning('Memory write-back unavailable for session %s',session_id,exc_info=True)
        finally:
            if entry is not None:
                await self.manager.release(entry)

    async def on_reasoning(self,agent,input_kwargs,next_handler):
        self._inside_reasoning=True
        await self.authorize()
        try:
            async with aclosing(super().on_reasoning(agent,input_kwargs,next_handler)) as stream:
                async for event in stream:
                    yield event
        finally:
            self._remove_hints(agent)

    async def on_reply(self,agent,input_kwargs,next_handler):
        await self.authorize_preparation()
        self._reply_active=True
        self._inside_reasoning=False
        completed_reply=None
        try:
            async with aclosing(super().on_reply(agent,input_kwargs,next_handler)) as stream:
                async for event in stream:
                    if getattr(event,'type',None)=='REPLY_END':
                        completed_reply=(event.reply_id if event.finished_reason=='completed' else None)
                    yield event
            # Execute only after normal stream exhaustion, under live authority.
            # This is Core completion, not a guarantee of later Product delivery.
            if completed_reply and not self._written:
                exchange=self._completed_exchange(agent,completed_reply)
                if exchange:
                    self._written=True
                    await self._write_completed(exchange,agent.state.session_id)
        finally:
            self._reply_active=False
            self._remove_hints(agent)
