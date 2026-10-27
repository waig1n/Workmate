"""Inject authenticated external results only after real resource completion."""
import json
from agentscope.event import ExternalExecutionResultEvent, ReplyStartEvent
from agentscope.message import ToolResultBlock
from workmate_contracts import snapshot_digest
from workmate_runtime.engines.agentscope import CoreSnapshot
from workmate_runtime.transport.external_results import ExternalResumeStore
from .controller import ExecutionAborted
from .stopping_preparation import PreparingExecutionController


class ExternalResultResources:
    """Process-local job identity plus the existing trusted provider probe ledger."""
    def __init__(self, registry):
        self.registry = registry
        self.jobs = {}

    def register(self, owner, job_id, producer_id, tool_call_id, *, cleanup, probe):
        key = (owner, producer_id, job_id)
        if key in self.jobs:
            raise ValueError('External job is already registered')
        resource_id = 'external:' + producer_id + ':' + job_id
        self.registry.register_external(owner,resource_id,tool_call_id=tool_call_id,cleanup=cleanup,probe=probe)
        self.jobs[key] = (tool_call_id,resource_id)

    async def verify_result(self, owner, receipt):
        item = self.jobs.get((owner,receipt.producer_id,receipt.job_id))
        if not item or item[0] != receipt.tool_call_id:
            return False
        return await self.registry.mark_finished(owner,item[1]) is True


class ExternalExecutionController(PreparingExecutionController):
    def __init__(self, pool, *, verify_external_result, **kwargs):
        super().__init__(pool,**kwargs)
        if not callable(verify_external_result):
            raise TypeError('Trusted external resource completion proof is required')
        self.verify_external_result = verify_external_result

    async def resume(self, command_id, worker_id, storage_node_id, prepare, *, authorize_resume):
        async with self.pool.acquire() as db:
            value = await db.fetchval('SELECT payload FROM runtime_commands WHERE id=$1',command_id)
            external = value and json.loads(value).get('resume_kind') == 'external_result'
        if not external:
            return await super().resume(command_id,worker_id,storage_node_id,prepare,authorize_resume=authorize_resume)
        async with self.pool.acquire() as db:
            claim = await ExternalResumeStore(db).claim_resume(command_id,worker_id,storage_node_id,
                                                              authorize_resume=authorize_resume)
        if claim is None:
            return None
        guard = self._new_guard(claim.owner,command_id,storage_node_id)
        guard.reply_id = claim.reply_id
        return await self._supervise(guard,self._resume_external(guard,claim,storage_node_id,prepare,authorize_resume))

    async def _resume_external(self, guard, claim, node_id, prepare, authorize):
        snapshot = CoreSnapshot.model_validate(claim.boundary.snapshot)
        state = snapshot.to_state()
        if state.session_id != guard.owner.session_id or state.reply_id != claim.reply_id:
            raise ExecutionAborted('External snapshot belongs to another Session/Reply')
        engine = await prepare(guard.owner,snapshot,guard)
        self._validate_engine(engine,guard)
        if snapshot_digest(engine.snapshot().model_dump(mode='json')) != snapshot_digest(claim.boundary.snapshot):
            raise ExecutionAborted('Preparation replaced exact external continuation state')
        async with self.pool.acquire() as db:
            receipt = await ExternalResumeStore(db).receipt(claim.payload.receipt_id)
        calls = {c.id:c for c in state.get_awaiting_tool_calls(engine.agent.name) if c.state == 'submitted'}
        if not receipt or receipt.tool_call_id not in calls:
            raise ExecutionAborted('Result does not refer to an original submitted call')
        call = calls[receipt.tool_call_id]
        if any((call.id,call.name,call.input) != (c['id'],c['name'],c['input']) for c in claim.original_calls):
            raise ExecutionAborted('Submitted parameters differ from the original external request')
        result = ToolResultBlock.model_validate(receipt.result)
        if result.model_dump(mode='json') != receipt.result:
            raise ExecutionAborted('External result is not an exact Core ToolResult')
        if await self.verify_external_result(guard.owner,receipt) is not True:
            raise ExecutionAborted('External resource completion has not been proved')
        async with self.pool.acquire() as db:
            await ExternalResumeStore(db).begin_resume(claim,node_id,authorize_resume=authorize,
                event=ReplyStartEvent(session_id=guard.owner.session_id,reply_id=claim.reply_id,
                                     name=engine.agent.name).model_dump(mode='json'))
        await guard.check()
        return await self._drain(guard,engine,
            ExternalExecutionResultEvent(reply_id=claim.reply_id,execution_results=[result]),claim.boundary)
