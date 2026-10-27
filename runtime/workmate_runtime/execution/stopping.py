"""Same-owner parked interruption, independent of the running execution path."""
from contextlib import aclosing

from agentscope.event import UserInterruptEvent
from workmate_contracts import snapshot_digest
from workmate_contracts.realtime import MessageSnapshotReference
from workmate_runtime.engines.agentscope import CoreSnapshot
from workmate_runtime.execution.controller import ExecutionAborted, ExecutionController, ExecutionGuard, ExecutionResult
from workmate_runtime.transport.stopping import StopStore


class StopGuard(ExecutionGuard):
    async def check(self):
        # A cleanup claim is never a grant to model, compress or act. This must
        # fail even though the owner still holds a valid lease for cleanup writes.
        raise ExecutionAborted('Parked Stop must not invoke model, compression or tools')


def _unfinished(state, agent_name):
    if not state.context or state.context[-1].name != agent_name or state.context[-1].role != 'assistant':
        return set()
    message = state.context[-1]
    return ({c.id for c in message.get_content_blocks('tool_call')}
            - {r.id for r in message.get_content_blocks('tool_result')})


class ParkedStopController(ExecutionController):
    def __init__(self,pool,*,lease_seconds=30,seal_terminal=None,realtime_writer_factory=None):
        super().__init__(pool,lease_seconds=lease_seconds)
        if seal_terminal is not None and not callable(seal_terminal):
            raise TypeError('Terminal delivery sealing requires a trusted callback')
        self.seal_terminal=seal_terminal
        if realtime_writer_factory is not None and not callable(realtime_writer_factory):
            raise TypeError('Realtime writer factory requires a trusted callback')
        self.realtime_writer_factory=realtime_writer_factory

    async def stop(self, command_id, worker_id, storage_node_id, prepare, *, verify_cleanup):
        """Stop only a parked Reply under a still-live same owner.

        prepare(owner, exact_snapshot, guard) is a trusted, side-effect-free
        cleanup assembly; it must not depend on current Agent-use permission.
        verify_cleanup(owner, original_snapshot, stopped_snapshot) must return
        exactly True after checking Task-scoped subprocesses and external jobs
        quiescent. Closing Core SUBMITTED state alone is NOT such evidence.
        Both callbacks run outside database transactions. No production adapters
        or long-lived parked supervisor are installed by this primitive.
        """
        async with self.pool.acquire() as db:
            claim = await StopStore(db).claim_parked(command_id,worker_id,storage_node_id,lease_seconds=self.lease_seconds)
        if claim is None:
            return None
        guard = StopGuard(self,claim.owner,command_id)
        guard.reply_id = claim.boundary.reply_id
        return await self._supervise(guard,self._interrupt(guard,claim,storage_node_id,prepare,verify_cleanup))

    async def _interrupt(self, guard, claim, node_id, prepare, verify_cleanup):
        writer = None
        try:
            if self.realtime_writer_factory is not None:
                writer = await self.realtime_writer_factory(guard,claim.boundary)
            return await self._interrupt_core(guard,claim,node_id,prepare,verify_cleanup,writer)
        finally:
            if writer is not None:
                await writer.close()

    async def _interrupt_core(self, guard, claim, node_id, prepare, verify_cleanup, writer):
        original = CoreSnapshot.model_validate(claim.boundary.snapshot).model_copy(deep=True)
        state = original.to_state()
        if state.session_id != guard.owner.session_id or state.reply_id != guard.reply_id:
            raise ExecutionAborted('Parked snapshot belongs to another Session/Reply')
        engine = await prepare(guard.owner,original,guard)
        self._validate_engine(engine,guard)
        if snapshot_digest(engine.snapshot().model_dump(mode='json')) != snapshot_digest(claim.boundary.snapshot):
            raise ExecutionAborted('Cleanup assembly replaced the original parked state')
        unfinished = _unfinished(state,engine.agent.name)
        if not unfinished or not state.has_awaiting_tool_calls(engine.agent.name):
            raise ExecutionAborted('Parked Stop requires actual pending work in the original Agent')
        events, closed = [], set()
        async with aclosing(engine.reply_stream(UserInterruptEvent(reply_id=guard.reply_id))) as stream:
            async for event in stream:
                if guard.failure is not None:
                    raise ExecutionAborted('Ownership lost while draining parked cleanup') from guard.failure
                raw = event.model_dump(mode='json')
                if (event.reply_id != guard.reply_id or raw.get('session_id',guard.owner.session_id) != guard.owner.session_id
                        or event.type not in {'TOOL_RESULT_START','TOOL_RESULT_TEXT_DELTA','TOOL_RESULT_END','REPLY_END'}):
                    raise ExecutionAborted('Unexpected event during parked cleanup')
                if event.type != 'REPLY_END' and raw.get('tool_call_id') not in unfinished:
                    raise ExecutionAborted('Cleanup event refers to an unrelated tool')
                if event.type == 'TOOL_RESULT_END':
                    if raw.get('state') != 'interrupted' or raw['tool_call_id'] in closed:
                        raise ExecutionAborted('Cleanup did not interrupt each unfinished tool exactly once')
                    closed.add(raw['tool_call_id'])
                events.append(raw)
                if writer is not None:
                    await writer.accept(raw)
        if (closed != unfinished or guard.active_tools or not events or events[-1]['type'] != 'REPLY_END'
                or events[-1].get('finished_reason') != 'interrupted'
                or sum(e['type']=='REPLY_END' for e in events) != 1):
            raise ExecutionAborted('Incomplete parked cleanup cannot become a terminal boundary')
        stopped = engine.snapshot()
        if _unfinished(stopped.to_state(),engine.agent.name):
            raise ExecutionAborted('Unfinished tool state remains after interruption')
        if await verify_cleanup(guard.owner,original,stopped) is not True:
            raise ExecutionAborted('Task-scoped resource cleanup has not been proven')
        if guard.failure is not None:
            raise ExecutionAborted('Ownership lost while checking resource cleanup') from guard.failure
        evidence = stopped.boundary_evidence(engine.agent.name)
        boundary = {'type':'EXECUTION_BOUNDARY','reply_id':guard.reply_id,'status':'interrupted',
            'pending_tool_call_ids':[],'boundary_evidence':evidence.model_dump(mode='json'),
            'stop_cleanup':{'command_id':claim.command_id,'original_snapshot_id':claim.boundary.snapshot_id,
                            'core_drained':True,'resources_quiescent':True}}
        if writer is not None:
            cut = await writer.checkpoint('interrupted')
            if cut is not None:
                boundary['display_snapshot'] = MessageSnapshotReference.from_snapshot(cut).model_dump(mode='json')
            else:
                boundary['display_empty'] = True
        if self.seal_terminal is not None:
            boundary['delivery_seal_sha256'] = await self.seal_terminal(
                guard.owner,guard.reply_id,'interrupted',stopped.model_dump(mode='json'))
        async with self.pool.acquire() as db:
            version,seq = await StopStore(db).finish(claim,node_id,snapshot=stopped.model_dump(mode='json'),
                                                   events=events,boundary=boundary)
        return ExecutionResult(guard.owner,guard.reply_id,'interrupted',version,seq,())
