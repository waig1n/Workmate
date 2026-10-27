"""Same-owner receipt validation reuses the exact control boundary locks."""
import json
from workmate_contracts import snapshot_digest
from workmate_contracts.external import ExternalResultReceipt, ExternalResumePayload
from .controls import ResumeStore


class ExternalResumeStore(ResumeStore):
    task_statuses = {'running', 'awaiting_permission'}
    payload_type = ExternalResumePayload

    async def receipt(self, receipt_id):
        row = await self.db.fetchrow('SELECT * FROM runtime_external_receipts WHERE receipt_id=$1',receipt_id)
        if not row:
            return None
        receipt = ExternalResultReceipt.model_validate(json.loads(row['payload']))
        return receipt if receipt.digest() == row['digest'] else None

    async def _decision_matches(self, command, payload, snapshot):
        receipt = await self.receipt(payload.receipt_id)
        saved = await self.db.fetchrow('SELECT * FROM task_projection_states WHERE task_id=$1',command['task_id'])
        if (not receipt or not saved or saved['session_id'] != command['session_id']
                or saved['reply_id'] != command['reply_id'] or saved['format_version'] != 1
                or saved['last_runtime_seq'] != snapshot['runtime_seq']
                or receipt.task_id != command['task_id'] or receipt.session_id != command['session_id']
                or receipt.reply_id != command['reply_id'] or receipt.execution_epoch != command['expected_epoch']):
            return None
        state = json.loads(saved['state'])
        record = state.get('interactions',{}).get(command['interaction_id'],{})
        pending = next((p for p in state.get('pending_interactions',[])
                        if p['interaction_id'] == command['interaction_id']),{})
        if (state.get('interaction_boundary_id') != payload.snapshot_id
                or record.get('reply_id') != command['reply_id']
                or any(r.get('kind') != 'external_execution' or r.get('request_version') != command['request_version']
                       for r in (record,pending))
                or receipt.tool_call_id not in pending.get('pending_tool_call_ids',[])):
            return None
        calls = tuple(c for c in record.get('tool_calls',[]) if c['id'] == receipt.tool_call_id
                      and c['name'] == receipt.tool_name
                      and snapshot_digest({'name':c['name'],'input':c['input']}) == receipt.input_digest)
        return calls if len(calls) == 1 else None
