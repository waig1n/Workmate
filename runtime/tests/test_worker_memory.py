"""Real ReMe BM25 lifecycle and process-safe namespace ownership."""
import asyncio
from types import SimpleNamespace

import pytest
from agentscope.message import AssistantMsg,UserMsg
from workmate_runtime.assembly.memory import WorkerMemoryManager,MemoryUnavailable
from test_core_engine import ScriptModel


async def placement(*args):
    pass


def manager(tmp_path,**kwargs):
    return WorkerMemoryManager(tmp_path,ScriptModel([]),bind_node=placement,wait_seconds=.25,**kwargs)


async def test_real_reme_namespace_handoff_preserves_bm25_index(tmp_path):
    first,second=manager(tmp_path),manager(tmp_path)
    try:
        entry=await first.acquire('u','a')
        assert entry.mode=='bm25'
        app=entry.middleware._app
        assert {'dream_cron','auto_memory','search'}<=set(app.context.jobs)
        # Actual local cards and native index job; no fake ReMe app/search.
        daily=entry.path/'daily'
        daily.mkdir(exist_ok=True)
        (daily/'preference.md').write_text('The user prefers chrysanthemum tea every morning.')
        await app.run_job('reindex')
        assert any('chrysanthemum' in hit for hit in await entry.middleware._search('chrysanthemum'))
        with pytest.raises(MemoryUnavailable,match='busy'):
            await second.acquire('u','a')
        assert first.instances and second.health()['namespace_states']
        await first.release(entry)
        second.wait_seconds=8
        acquired=await second.acquire('u','a')
        assert not first.instances
        assert any('chrysanthemum' in hit for hit in await acquired.middleware._search('chrysanthemum'))
        await second.release(acquired)
    finally:
        await first.close()
        await second.close()


async def test_namespace_write_reference_blocks_handoff_until_joined(tmp_path,monkeypatch):
    first,second=manager(tmp_path),manager(tmp_path)
    gate=asyncio.Event()
    observed=[]
    try:
        entry=await first.acquire('u','a')
        async def write(messages,session_id):
            observed.append((messages,session_id))
            await gate.wait()
        monkeypatch.setattr(entry.middleware,'_write_back',write)
        message=AssistantMsg(name='assistant',content='immutable original')
        first.write(entry,[message],'session')
        message.content=[]
        await first.release(entry)
        await asyncio.sleep(0)
        with pytest.raises(MemoryUnavailable):
            await second.acquire('u','a')
        assert observed[0][0][0].get_text_content()=='immutable original'
        gate.set()
        await asyncio.gather(*entry.writes)
        second.wait_seconds=8
        acquired=await second.acquire('u','a')
        await second.release(acquired)
    finally:
        gate.set()
        await first.close()
        await second.close()


async def test_placement_mismatch_does_not_create_a_new_vault(tmp_path):
    async def denied(*args):
        raise MemoryUnavailable('different node')
    memory=WorkerMemoryManager(tmp_path,ScriptModel([]),bind_node=denied)
    try:
        with pytest.raises(MemoryUnavailable,match='placement'):
            await memory.acquire('u','a')
        assert not list(tmp_path.iterdir())
    finally:
        await memory.close()


async def test_unavailable_memory_tool_returns_error_not_empty_matches(tmp_path):
    memory=manager(tmp_path)
    mw=memory.middleware('u','a',task_input_ids=('fixture-input',),authorize=placement,authorize_preparation=placement)
    tool=(await mw.list_tools())[0]
    result=await tool(query='anything',_agent_state=object())
    assert result.state=='error'
    assert 'not active' in result.content[0].text
    await memory.close()


async def test_real_auto_memory_receives_only_new_exchange_and_writes_daily_card(tmp_path):
    import json
    from agentscope.message import TextBlock,ToolCallBlock
    model=ScriptModel([[ToolCallBlock(id='memory-write',name='daily_write',input=json.dumps({
        'name':'tea-preference','description':'User preference','session_id':'session',
        'content':'User prefers chrysanthemum tea.'}))],[TextBlock(text='Recorded')],[TextBlock(text='No new durable facts')]])
    memory=WorkerMemoryManager(tmp_path,model,bind_node=placement)
    from test_core_engine import make_engine,collect
    from agentscope.state import AgentState
    old=UserMsg(name='user',content='old conversation content')
    state=AgentState(session_id='session',context=[old])
    try:
        for turn in range(2):
            user=UserMsg(name='user',content='I prefer chrysanthemum tea.' if turn==0 else 'Thank you.')
            mw=memory.middleware('u','a',task_input_ids=(user.id,),authorize=placement,authorize_preparation=placement)
            engine=make_engine([[TextBlock(text='Acknowledged.')]],state=state,middlewares=[mw])
            await collect(engine,user)
            state=engine.snapshot().to_state()
            entry=next(iter(memory.instances.values()))
            await asyncio.gather(*entry.writes)
        rows=[json.loads(line) for line in (entry.path/'session'/'dialog'/'session.jsonl').read_text().splitlines()]
        assert len(rows)==4 and old.id not in {row['id'] for row in rows}
        assert any('chrysanthemum' in file.read_text() for file in (entry.path/'daily').rglob('*.md'))
        await entry.middleware._app.run_job('reindex')
        assert any('chrysanthemum' in hit for hit in await entry.middleware._search('chrysanthemum'))
    finally:
        await memory.close()


async def test_vector_initialization_failure_falls_back_to_real_bm25(tmp_path,monkeypatch):
    from agentscope.middleware import ReMeMiddleware
    from agentscope.embedding import OpenAIEmbeddingModel
    from agentscope.credential import OpenAICredential
    embedding=OpenAIEmbeddingModel(credential=OpenAICredential(api_key='fixture'),model='fixture',dimensions=8)
    original=ReMeMiddleware._ensure_started
    calls=[]
    async def start(self):
        calls.append(self._parameters.embedding_model)
        if self._parameters.embedding_model is not None:
            raise RuntimeError('fixture vector backend failure')
        await original(self)
    monkeypatch.setattr(ReMeMiddleware,'_ensure_started',start)
    memory=WorkerMemoryManager(tmp_path,ScriptModel([]),embedding,bind_node=placement)
    try:
        entry=await memory.acquire('u','a')
        assert entry.mode=='bm25' and calls==[embedding,None]
        await memory.release(entry)
    finally:
        await memory.close()


async def test_real_reme_tool_and_transient_hint_survive_two_core_assemblies(tmp_path,monkeypatch):
    from agentscope.message import TextBlock,ToolCallBlock
    from test_worker_agent_assembly import LocalGuard,OWNER,context,Sessions
    from test_worker_workspaces import manager as workspace_manager
    from workmate_runtime.assembly.agent import WorkerAgentAssembler
    from workmate_runtime.config import RuntimeSettings
    workspace=workspace_manager(tmp_path/'workspaces')
    memory=manager(tmp_path/'memory')
    ctx=context()
    async def read(*args,**kwargs): return ctx
    async def no_attestation(*args): pass
    monkeypatch.setattr('workmate_runtime.assembly.agent.read_execution_context',read)
    monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox',no_attestation)
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',lambda *_:ScriptModel([
        [ToolCallBlock(id='search',name='memory_search',input='{"query":"chrysanthemum"}')],
        [TextBlock(text='The user prefers tea.')]]))
    assembler=WorkerAgentAssembler(Sessions(),RuntimeSettings(reme_enabled=True),workspace,memory_manager=memory)
    entry=await memory.acquire('user','agent')
    (entry.path/'daily').mkdir(exist_ok=True)
    (entry.path/'daily'/'tea.md').write_text('User prefers chrysanthemum tea.')
    await entry.middleware._app.run_job('reindex')
    await memory.release(entry)
    # Count actual injected messages while preserving real search and formatting.
    from workmate_runtime.assembly.memory import WorkerMemoryMiddleware
    build=WorkerMemoryMiddleware._build_memory_message
    hints=[]
    def capture(self,memories):
        message=build(self,memories)
        hints.append(message)
        return message
    monkeypatch.setattr(WorkerMemoryMiddleware,'_build_memory_message',capture)
    # Keep auxiliary extraction deterministic; no new durable facts needed.
    memory.chat_model.responses=iter([[TextBlock(text='No new durable facts')]]*4)
    try:
        first=await assembler.prepare_start(OWNER,None,LocalGuard())
        _=[event async for event in first.engine.reply_stream(first.inputs)]
        snapshot=first.engine.snapshot()
        assert all(message.get('name')!='memory' for message in snapshot.state.get('context',[]))
        from dataclasses import replace
        from workmate_runtime.transport.postgres import ExecutionOwner
        from workmate_product.application.execution_context import ProductInputMessage
        next_owner=ExecutionOwner(OWNER.session_id,'next-task',OWNER.worker_id,2)
        ctx=replace(ctx,task_id='next-task',initial_spec=ctx.initial_spec.model_copy(update={'task_id':'next-task'}),
            input_messages=(ProductInputMessage('next-message',{'text':'Remember my tea?'}),))
        second=await assembler.prepare_start(next_owner,snapshot,LocalGuard(next_owner))
        _=[event async for event in second.engine.reply_stream(second.inputs)]
        assert hints
        assert all(message.get('name')!='memory' for message in second.engine.snapshot().state.get('context',[]))
    finally:
        await memory.close()
        await workspace.close()


async def test_cancelled_start_joins_real_backgrounds_before_releasing_lock(tmp_path,monkeypatch):
    from agentscope.middleware import ReMeMiddleware
    original=ReMeMiddleware._ensure_started
    started=[]
    async def slow(self):
        await original(self)
        started.append(self)
        await asyncio.Event().wait()
    monkeypatch.setattr(ReMeMiddleware,'_ensure_started',slow)
    memory=manager(tmp_path,startup_seconds=.1)
    try:
        with pytest.raises(MemoryUnavailable,match='timed out'):
            await memory.acquire('u','a')
        assert started and not started[0]._app._started_components
        assert all(task.done() for task in started[0]._workmate_owned_tasks)
        monkeypatch.setattr(ReMeMiddleware,'_ensure_started',original)
        memory.startup_seconds=15
        entry=await memory.acquire('u','a')
        await memory.release(entry)
    finally:
        await memory.close()


async def test_namespace_rejects_symlink_ancestor(tmp_path):
    real=tmp_path/'real'
    real.mkdir()
    (tmp_path/'linked').symlink_to(real,target_is_directory=True)
    memory=manager(tmp_path/'linked')
    try:
        with pytest.raises(MemoryUnavailable,match='symlink'):
            await memory.acquire('u','a')
        assert not list(real.iterdir())
    finally:
        await memory.close()


async def test_separate_os_process_cannot_open_live_namespace_lock(tmp_path):
    import sys
    memory=manager(tmp_path)
    entry=await memory.acquire('u','a')
    path=str(entry.path/'.workmate-owner.lock')
    script='''import fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDWR)
try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: sys.exit(7)
os.close(fd)
'''
    async def probe():
        process=await asyncio.create_subprocess_exec(sys.executable,'-c',script,path)
        return await process.wait()
    try:
        assert await probe()==7
        await memory.release(entry)
        await memory.close()
        assert await probe()==0
    finally:
        if memory.instances:
            await memory.release(entry)
            await memory.close()


async def test_idle_lru_eviction_closes_old_app_before_new_namespace(tmp_path):
    memory=manager(tmp_path,max_instances=2)
    try:
        first=await memory.acquire('u1','a')
        await memory.release(first)
        second=await memory.acquire('u2','a')
        await memory.release(second)
        again=await memory.acquire('u1','a')
        assert again is first
        await memory.release(again)
        third=await memory.acquire('u3','a')
        assert first in memory.instances.values() and second not in memory.instances.values()
        assert not second.middleware._app._started_components
        await memory.release(third)
    finally:
        await memory.close()


async def test_timed_out_handoff_keeps_idle_owner_cron_running(tmp_path):
    first,second=manager(tmp_path),manager(tmp_path)
    try:
        entry=await first.acquire('u','a')
        with pytest.raises(MemoryUnavailable,match='busy'):
            await second.acquire('u','a')
        assert not (entry.path/'.workmate-handoff').exists()
        await first.release(entry)
        await asyncio.sleep(.15)
        assert entry in first.instances.values()
        assert not entry.middleware._app.context.jobs['dream_cron']._task.done()
    finally:
        await first.close()
        await second.close()


async def test_accepted_handoff_waits_for_actual_close_past_busy_deadline(tmp_path,monkeypatch):
    first,second=manager(tmp_path,poll_seconds=.005),manager(tmp_path,poll_seconds=.005)
    close=first._close_namespace
    async def delayed_close(*args):
        await asyncio.sleep(.2)
        await close(*args)
    monkeypatch.setattr(first,"_close_namespace",delayed_close)
    try:
        entry=await first.acquire('u','a')
        await first.release(entry)
        # Native watcher close is slower than the ordinary contention budget.
        second.wait_seconds=.1
        acquired=await second.acquire('u','a')
        assert not first.instances
        await second.release(acquired)
    finally:
        await first.close()
        await second.close()


async def test_peer_can_search_while_first_reply_is_still_running(tmp_path):
    first, second = manager(tmp_path), manager(tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()
    mw = first.middleware('u', 'a', task_input_ids=('fixture-input',), authorize=placement, authorize_preparation=placement)
    agent = SimpleNamespace(state=SimpleNamespace(session_id='long-reply', context=[]))
    async def reply(**kwargs):
        # Exercise the actual search rather than only allocating a middleware.
        await mw._search('chrysanthemum')
        entered.set()
        await finish.wait()
        yield 'done'
    async def run():
        return [event async for event in mw.on_reply(agent, {}, reply)]
    running = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 15)
        peer = await second.acquire('u', 'a')
        try:
            assert not running.done()
            assert await peer.middleware._search('chrysanthemum') == []
        finally:
            await second.release(peer)
    finally:
        finish.set()
        await running
        await first.close()
        await second.close()


async def test_cancelled_memory_search_releases_operation_reference(tmp_path, monkeypatch):
    memory = manager(tmp_path)
    entered = asyncio.Event()
    entry = await memory.acquire('u', 'a')
    await memory.release(entry)
    async def search(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(entry.middleware, '_search', search)
    mw = memory.middleware('u', 'a', task_input_ids=('fixture-input',), authorize=placement, authorize_preparation=placement)
    agent = SimpleNamespace(state=SimpleNamespace(session_id='cancelled-search', context=[]))
    async def reply(**kwargs):
        await mw._search('tea')
        yield 'unreachable'
    async def run():
        return [event async for event in mw.on_reply(agent, {}, reply)]
    running = asyncio.create_task(run())
    try:
        await entered.wait()
        # A Reply-wide reference plus an operation reference would pin the vault.
        assert entry.references == 1
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        assert entry.references == 0
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await memory.close()


@pytest.mark.parametrize('failure', ['busy', 'revoked'])
async def test_writeback_failure_does_not_fail_reply_or_leak_reference(tmp_path, monkeypatch, failure):
    memory = manager(tmp_path)
    entry = await memory.acquire('u', 'a')
    await memory.release(entry)
    calls = 0
    async def authorize():
        nonlocal calls
        calls += 1
        if failure == 'revoked' and calls > 1:
            raise PermissionError('owner revoked during acquire')
    if failure == 'busy':
        async def busy(*args):
            raise MemoryUnavailable('busy')
        monkeypatch.setattr(memory, 'acquire', busy)
    mw = memory.middleware('u', 'a', task_input_ids=('fixture-input',), authorize=authorize, authorize_preparation=placement)
    mw._reply_active = True
    try:
        await mw._write_completed([AssistantMsg(name='assistant', content='answer')], 's')
        assert entry.references == 0 and not entry.writes
    finally:
        await memory.close()


async def test_stop_does_not_wait_for_memory_acquisition(tmp_path, monkeypatch):
    memory = manager(tmp_path)
    mw = memory.middleware('u', 'a', task_input_ids=('fixture-input',), authorize=placement, authorize_preparation=placement)
    mw._reply_active = True
    acquired = []
    async def acquire(*args):
        acquired.append(True)
        await asyncio.Event().wait()
    monkeypatch.setattr(memory, 'acquire', acquire)
    entered = asyncio.Event()
    async def run():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await mw._write_completed([AssistantMsg(name='assistant', content='partial')], 's')
    running = asyncio.create_task(run())
    await entered.wait()
    running.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 1)
        assert not acquired
    finally:
        await memory.close()


async def test_real_core_cold_approval_writes_only_completed_task_exchange(tmp_path, monkeypatch):
    from agentscope.event import ConfirmResult, UserConfirmResultEvent
    from agentscope.message import TextBlock, ToolCallBlock
    from agentscope.state import AgentState
    from test_core_engine import ConfirmTool, make_engine, collect
    from workmate_runtime.engines.agentscope import CoreSnapshot

    writes=[]
    first=manager(tmp_path)
    second=manager(tmp_path)
    for owner in (first,second):
        monkeypatch.setattr(owner,'write',lambda entry,messages,sid: writes.append((messages,sid)))
    user=UserMsg(id='task-input',name='user',content='Remember my tea preference and write it.')
    old=UserMsg(id='old-input',name='user',content='Unrelated old history')
    def middleware(owner):
        mw=owner.middleware('u','a',task_input_ids=(user.id,),authorize=placement,authorize_preparation=placement)
        return mw
    try:
        tool=ConfirmTool()
        engine=make_engine([[ToolCallBlock(id='approval-write',name=tool.name,input='{}')]],
            tool=tool,state=AgentState(session_id='memory-session',context=[old]),middlewares=[middleware(first)])
        events=await collect(engine,user)
        required=next(e for e in events if e.type=='REQUIRE_USER_CONFIRM')
        assert writes==[] and tool.calls==0
        snapshot=CoreSnapshot.model_validate_json(engine.snapshot().model_dump_json())
        await first.close()
        resumed_tool=ConfirmTool()
        restored=make_engine([[TextBlock(text='Your tea preference is recorded.')]],
            tool=resumed_tool,state=snapshot.to_state(),middlewares=[middleware(second)])
        events=await collect(restored,UserConfirmResultEvent(reply_id=required.reply_id,
            confirm_results=[ConfirmResult(confirmed=True,tool_call=required.tool_calls[0])]))
        assert events[-1].finished_reason=='completed' and resumed_tool.calls==1
        assert len(writes)==1
        messages,sid=writes[0]
        assert sid=='memory-session'
        assert messages[0].id==user.id and old.id not in {m.id for m in messages}
        assert any(m.role=='assistant' and 'tea preference' in m.get_text_content() for m in messages)
        assert all(m.name!='memory' for m in messages)
        assert any(block.type=='tool_result' for m in messages for block in m.content)
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize('outcome',['completed','stop','error','missing_anchor','revoked','early_close'])
async def test_real_core_memory_requires_normal_authorized_completion(tmp_path,monkeypatch,outcome):
    from agentscope.event import UserInterruptEvent
    from agentscope.message import TextBlock,ToolCallBlock
    from test_core_engine import ConfirmTool,make_engine,collect
    memory=manager(tmp_path)
    writes=[]
    monkeypatch.setattr(memory,'write',lambda entry,messages,sid:writes.append(messages))
    revoked=False
    async def authorized():
        if revoked: raise PermissionError('Task authority revoked')
    user=UserMsg(id='new-task-input',name='user',content='Remember this preference')
    mw=memory.middleware('u','a',task_input_ids=('absent' if outcome=='missing_anchor' else user.id,),
        authorize=authorized,authorize_preparation=placement)
    try:
        if outcome=='stop':
            tool=ConfirmTool()
            engine=make_engine([[ToolCallBlock(id='ask',name=tool.name,input='{}')]],tool=tool,middlewares=[mw])
            events=await collect(engine,user)
            assert not writes
            request=next(e for e in events if e.type=='REQUIRE_USER_CONFIRM')
            await collect(engine,UserInterruptEvent(reply_id=request.reply_id))
            assert tool.calls==0
        else:
            engine=make_engine([] if outcome=='error' else [[TextBlock(text='Remembered')]],middlewares=[mw])
            stream=engine.reply_stream(user)
            try:
                async for event in stream:
                    if event.type=='REPLY_END':
                        if outcome=='revoked': revoked=True
                        if outcome=='early_close': break
            except RuntimeError as error:
                if outcome!='error' or 'async generator raised StopIteration' not in str(error):
                    raise
            finally:
                await stream.aclose()
        assert len(writes)==(1 if outcome=='completed' else 0)
    finally:
        await memory.close()


async def test_real_core_multiple_approval_segments_write_one_exchange(tmp_path,monkeypatch):
    from agentscope.event import ConfirmResult,UserConfirmResultEvent
    from agentscope.message import TextBlock,ToolCallBlock
    from test_core_engine import ConfirmTool,make_engine,collect
    memory=manager(tmp_path)
    writes=[]
    monkeypatch.setattr(memory,'write',lambda entry,messages,sid:writes.append(messages))
    user=UserMsg(id='task-input',name='user',content='Remember this after both approvals')
    tool=ConfirmTool()
    mw=memory.middleware('u','a',task_input_ids=(user.id,),authorize=placement,authorize_preparation=placement)
    engine=make_engine([[ToolCallBlock(id='one',name=tool.name,input='{}')],
        [ToolCallBlock(id='two',name=tool.name,input='{}')],[TextBlock(text='Both written')]],tool=tool,middlewares=[mw])
    try:
        events=await collect(engine,user)
        for _ in range(2):
            assert not writes
            required=next(e for e in events if e.type=='REQUIRE_USER_CONFIRM')
            events=await collect(engine,UserConfirmResultEvent(reply_id=required.reply_id,
                confirm_results=[ConfirmResult(confirmed=True,tool_call=required.tool_calls[0])]))
        assert tool.calls==2 and len(writes)==1
        assert writes[0][0].id==user.id
        assert len([b for m in writes[0] for b in m.content if b.type=='tool_result'])==2
    finally:
        await memory.close()


def test_completed_exchange_keeps_all_reply_fragments_without_hints_or_old_history():
    from agentscope.message import HintBlock,TextBlock
    from workmate_runtime.assembly.memory import WorkerMemoryMiddleware
    user=UserMsg(id='input',name='user',content='Current input')
    first=AssistantMsg(id='reply',name='assistant',content=[TextBlock(text='First'),HintBlock(hint='memory hint')])
    second=AssistantMsg(id='reply',name='assistant',content='Second')
    context=[UserMsg(id='old',name='user',content='Old input'),
             AssistantMsg(id='old-reply',name='assistant',content='Old answer'),user,first,
             AssistantMsg(id='reply',name='other-agent',content='Other agent'),second]
    mw=WorkerMemoryMiddleware(None,'u','a',('input',),placement,placement)
    selected=mw._completed_exchange(SimpleNamespace(name='assistant',state=SimpleNamespace(context=context)),'reply')
    assert [m.get_text_content() for m in selected]==['Current input','First','Second']
    assert all(block.type!='hint' for m in selected for block in m.content)
    assert selected[0] is not user
    assert any(block.type=='hint' for block in first.content)


@pytest.mark.parametrize('ids',[None,(),('same','same'),('',),'unscoped'])
def test_memory_requires_explicit_unique_task_input_ids(ids):
    from workmate_runtime.assembly.memory import WorkerMemoryMiddleware
    with pytest.raises(ValueError,match='Task input message IDs'):
        WorkerMemoryMiddleware(None,'u','a',ids,placement,placement)
