"""Two composed Workers share files but never share execution ownership."""
from polling_worker import polling_composition
import asyncio
import json
from pathlib import Path

import pytest
from agentscope.message import TextBlock
from workmate_contracts import Command
from workmate_product.application.commands import accept_start
from workmate_product.application.execution_context import record_execution_spec
from workmate_product.config.worker import WorkspaceLifecyclePolicy
from workmate_product.persistence.models import Task, TaskMessage
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.execution.composition import compose_worker
from test_core_engine import ScriptModel
from test_execution_context import database, record, MODEL
from test_worker_workspaces import StubWorkspace


async def wait_for(predicate):
    async with asyncio.timeout(15):
        while not await predicate():
            await asyncio.sleep(.01)


@pytest.mark.parametrize("first_outcome", ["completed", "rejected"])
async def test_peer_delivery_and_next_task_before_original_worker_finalizes(database, tmp_path, monkeypatch, first_outcome,polling_composition):
    """A peer can publish terminal capture before the first process unwinds.

    Database gates may then admit the next Task on another process. Local cache
    cleanup must not race that admission or unlink/overwrite the new owner's files.
    """
    from app.services.durable_runtime import compose_product_runtime
    sessions, admin = database
    await record(sessions)
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',
                        lambda *_: ScriptModel([[TextBlock(text='answer')]]))
    attestations = []
    async def attest(*_):
        attestations.append(True)
        if first_outcome == 'rejected' and len(attestations) == 1:
            from workmate_runtime.execution.controller import SandboxVerificationFailed
            raise SandboxVerificationFailed('fixture rejection after Workspace binding')
        return None  # No real tools/Full Access; filesystem ownership is real.
    monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox', attest)
    settings = RuntimeSettings(reme_enabled=False,
        workspace_lifecycle=WorkspaceLifecyclePolicy(max_warm_per_worker=0))
    workers = [compose_worker(sessions.kw['bind'], settings, storage_node_id='node',
        workspace_dir=tmp_path/'workspaces', artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024, max_files=3, workspace_factory=StubWorkspace) for _ in range(2)]
    first, second = workers
    shutdown = asyncio.Event()
    product = compose_product_runtime(sessions, poll_interval=.01)
    projection = asyncio.create_task(product.serve(shutdown))
    try:
        result = await first.service.controller.start('cmd', first.service.worker_id, 'node',
                                                       first.service.bindings.prepare_start)
        assert result.status == ('completed' if first_outcome == 'completed' else 'failed')
        workspace_path = next(p for p in (tmp_path/'workspaces').iterdir() if not p.name.startswith('.'))
        keep = workspace_path/'working'/'keep.txt'
        # Immutable tool result bytes are already sealed; this file models an
        # existing working file, not an output publication.
        keep.write_text('persisted workspace')
        assert result.owner in first.workspaces._bindings
        assert not first.workspaces._ownership
        # Simulate the startup recovery path of a peer before owner finalization.
        if result.reply_id is not None:
            await second.delivery_recovery.recover(result.owner)
        async def released():
            assert not product.failures, product.failures
            return await admin.fetchval("SELECT active_product_task_id FROM conversations WHERE id='c'") is None
        await wait_for(released)
        async def allowed(*_):
            return True
        async with sessions.begin() as db:
            db.add(Task(id='t2', conversation_id='c', title='Next', approval_profile='standard'))
            await db.flush()
            db.add(TaskMessage(id='m2', task_id='t2', conversation_id='c', role='user', content={'text':'next'}))
            await accept_start(db, Command(command_id='cmd2', kind='StartTask', conversation_id='c',
                runtime_session_id='s', product_task_id='t2', expected_epoch=result.owner.execution_epoch),
                actor_user_id='u', storage_node_id='node', authorize_agent=allowed)
            await record_execution_spec(db, task_id='t2', session_id='s', actor_user_id='u',
                                        selected_model=MODEL, knowledge_enabled=False)
        result2 = await second.service.controller.start('cmd2', second.service.worker_id, 'node',
                                                        second.service.bindings.prepare_start)
        assert result2.status == 'completed'
        assert keep.read_text() == 'persisted workspace'
        # The late first finalizer cannot release or corrupt the second epoch.
        await first.service.bindings.finalize_result(result)
        await second.service.bindings.finalize_result(result2)
        await wait_for(released)
        owners = await admin.fetch("SELECT claimed_by FROM runtime_commands WHERE kind='StartTask' ORDER BY id")
        assert [r['claimed_by'] for r in owners] == [first.service.worker_id, second.service.worker_id]
        assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures') == (2 if first_outcome == 'completed' else 1)
        assert not first.workspaces._bindings and not second.workspaces._bindings
        assert keep.read_text() == 'persisted workspace'
    finally:
        shutdown.set()
        await asyncio.wait_for(projection, 10)
        for app in workers:
            await app.workspaces.close()


@pytest.mark.parametrize('decision', ['resume', 'stop'])
async def test_two_worker_loops_route_pinned_controls_to_owner(database, tmp_path, monkeypatch, decision,polling_composition):
    from agentscope.message import ToolCallBlock
    from workmate_product.application.control_commands import accept_resume, accept_stop
    from app.services.durable_runtime import compose_product_runtime
    sessions, admin = database
    await admin.execute("UPDATE tasks SET approval_profile='strict'")
    await record(sessions)
    calls = []
    def model(*_):
        reply = ([[ToolCallBlock(id='write', name='Write', input=json.dumps({
            'file_path':'/workspace/output/result.txt', 'content':'once'}))]]
            if not calls else [[TextBlock(text='done')]])
        calls.append(reply)
        return ScriptModel(reply)
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model', model)
    async def attest(*_):
        return None
    monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox', attest)
    settings = RuntimeSettings(reme_enabled=False,
        workspace_lifecycle=WorkspaceLifecyclePolicy(max_warm_per_worker=0))
    workers = [compose_worker(sessions.kw['bind'], settings, storage_node_id='node',
        workspace_dir=tmp_path/'workspaces', artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024, max_files=3, workspace_factory=StubWorkspace, drain_seconds=0) for _ in range(2)]
    from dataclasses import replace
    for worker in workers:
        worker.service.bindings = replace(worker.service.bindings, suspend=None)
    product = compose_product_runtime(sessions, poll_interval=.01)
    shutdown = asyncio.Event()
    running = [asyncio.create_task(app.serve(shutdown)) for app in [*workers, product]]
    try:
        async def parked():
            assert not product.failures, product.failures
            assert not any(w.service.failures for w in workers)
            return await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == 'awaiting_permission'
        await wait_for(parked)
        execution = await admin.fetchrow("SELECT * FROM runtime_task_executions WHERE task_id='t'")
        owner = await admin.fetchval("SELECT owner_id FROM runtime_sessions WHERE id='s'")
        state = json.loads(await admin.fetchval("SELECT state FROM task_projection_states WHERE task_id='t'"))
        pending = state['pending_interactions'][0]
        assert len(calls) == 1
        assert not list((tmp_path/'workspaces').rglob('result.txt'))
        base = dict(command_id=decision, conversation_id='c', runtime_session_id='s',
                    product_task_id='t', reply_id=execution['reply_id'], expected_epoch=execution['execution_epoch'])
        async with sessions.begin() as db:
            if decision == 'stop':
                await accept_stop(db, Command(kind='StopTask', **base), actor_user_id='u')
            else:
                async def allowed(*_):
                    return True
                command = Command(kind='ResumeInteraction', **base,
                    interaction_id=pending['interaction_id'], request_version=pending['request_version'],
                    payload={'decision_version':'approved', 'snapshot_id':execution['snapshot_id'],
                             'state_version':execution['state_version'],
                             'decisions':[{'tool_call_id':'write','approved':True}]})
                await accept_resume(db, command, actor_user_id='u', authorize_resume=allowed)
        async def completed():
            assert not product.failures, product.failures
            assert not any(w.service.failures for w in workers), [w.service.failures for w in workers]
            return await admin.fetchval("SELECT active_product_task_id FROM conversations WHERE id='c'") is None
        await wait_for(completed)
        assert await admin.fetchval('SELECT claimed_by FROM runtime_commands WHERE id=$1', decision) == owner
        assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE kind='StartTask'") == 1
        assert await admin.fetchval("SELECT run_id FROM tasks WHERE id='t'") == execution['reply_id']
        assert await admin.fetchval('SELECT count(*) FROM artifacts') == int(decision == 'resume')
        assert len(calls) == (2 if decision == 'resume' else 1)
    finally:
        shutdown.set()
        await asyncio.wait_for(asyncio.gather(*running), 15)
