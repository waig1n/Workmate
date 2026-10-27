"""Trusted producer facts and independently authorized external continuations.

Callbacks authenticate the producer/job binding or current Product access using
the caller's transaction. They must not commit or perform external I/O.
"""
from workmate_product.coordination import stage_wakeup
from workmate_product.persistence.repositories import external_results as persistence
from workmate_product.persistence.repositories.control_admission import insert_command
from workmate_contracts import snapshot_digest
from workmate_contracts.external import ExternalResultReceipt, ExternalResumePayload
from .commands import AcceptedCommand, CommandConflict, CommandRejected
from .control_commands import _transaction, _owner, _binding, _digest, _duplicate


def matching_call(receipt, record):
    return next((call for call in record.get('tool_calls', [])
        if call['id'] == receipt.tool_call_id and call['name'] == receipt.tool_name
        and snapshot_digest({'input':call['input'], 'name':call['name']}) == receipt.input_digest), None)


async def record_external_receipt(db, receipt, *, verify_producer):
    """Record an immutable terminal fact; never impersonate an approval."""
    _transaction(db)
    receipt = ExternalResultReceipt.model_validate(receipt.model_dump(mode='json'))
    async with db.begin_nested():
        if await verify_producer(db, receipt) is not True:
            raise CommandRejected('External producer/job authority is denied')
        task = await persistence.lock_task(db, id=receipt.task_id)
        existing = await persistence.matching_receipt(db, id=receipt.receipt_id, tid=receipt.task_id, call=receipt.tool_call_id, producer=receipt.producer_id, job=receipt.job_id)
        if existing:
            if existing['digest'] != receipt.digest():
                raise CommandConflict('External receipt identity already has different content')
            return existing['receipt_id']
        saved = await persistence.projection_state(db, id=receipt.task_id)
        execution = await persistence.execution(db, id=receipt.task_id)
        if (not task or task['conversation_id'] != receipt.conversation_id or not saved or not execution
                or saved['session_id'] != receipt.session_id or saved['reply_id'] != receipt.reply_id
                or execution['execution_epoch'] != receipt.execution_epoch
                or execution['session_id'] != receipt.session_id or execution['reply_id'] != receipt.reply_id
                or not any(r.get('kind') == 'external_execution' and matching_call(receipt, r)
                           for r in saved['state'].get('interactions', {}).values())):
            raise CommandRejected('External receipt does not match an original submitted call')
        await persistence.insert_receipt(db, receipt)
        await persistence.append_receipt_audit(db, receipt)
        return receipt.receipt_id


async def accept_external_resume(db, command, *, actor_user_id, authorize_resume):
    _transaction(db)
    if command.kind != 'ResumeInteraction':
        raise CommandRejected('Expected ResumeInteraction')
    payload = ExternalResumePayload.model_validate(command.payload)
    digest = _digest(command, actor_user_id)
    async with db.begin_nested():
        agent, conversation = await _owner(db, command, actor_user_id)
        existing = await persistence.command(db, id=command.command_id)
        if existing:
            return _duplicate(existing, digest)
        if not agent or agent['deleted_at'] is not None or await authorize_resume(db, actor_user_id, agent['id']) is not True:
            raise CommandRejected('Current Agent/capability access is denied')
        task, session, execution = await _binding(db, command, conversation)
        if task['status'] not in {'running', 'awaiting_permission'}:
            raise CommandRejected('Task is not eligible for external continuation')
        if await persistence.outstanding_control(db, id=task['id'], snapshot=payload.snapshot_id):
            raise CommandRejected('A control command already owns this boundary')
        if any(r['status'] != 'awaiting_interaction' or r['snapshot_id'] != payload.snapshot_id
               or r['state_version'] != payload.state_version for r in (session,execution)):
            raise CommandRejected('Execution is not parked at this boundary')
        snapshot = await persistence.snapshot(db, id=payload.snapshot_id)
        saved = await persistence.lock_projection(db, id=task['id'])
        cursor = await persistence.projection_cursor(db, id=conversation['id'])
        receipt_row = await persistence.receipt(db, id=payload.receipt_id)
        if (not snapshot or not saved or not cursor or not receipt_row
                or snapshot['session_id'] != session['id'] or snapshot['task_id'] != task['id']
                or snapshot['reply_id'] != command.reply_id or snapshot['execution_epoch'] != command.expected_epoch
                or snapshot['state_version'] != payload.state_version or snapshot['status'] != 'awaiting_interaction'
                or saved['session_id'] != session['id'] or saved['reply_id'] != command.reply_id
                or saved['format_version'] != 1 or cursor['session_id'] != session['id']
                or any(r[key] != snapshot['runtime_seq'] for r,key in
                       ((session,'runtime_seq'),(saved,'last_runtime_seq'),(cursor,'last_runtime_seq')))):
            raise CommandRejected('External result projection is not current')
        receipt = ExternalResultReceipt.model_validate(receipt_row['payload'])
        record = saved['state'].get('interactions',{}).get(command.interaction_id,{})
        pending = next((p for p in saved['state'].get('pending_interactions',[])
                        if p['interaction_id'] == command.interaction_id),{})
        if (receipt.digest() != receipt_row['digest'] or receipt.task_id != task['id']
                or receipt.session_id != session['id'] or receipt.reply_id != command.reply_id
                or receipt.execution_epoch != command.expected_epoch
                or saved['state'].get('interaction_boundary_id') != payload.snapshot_id
                or any(r.get('kind') != 'external_execution' or r.get('request_version') != command.request_version
                       for r in (record,pending))
                or receipt.tool_call_id not in pending.get('pending_tool_call_ids',[])
                or not matching_call(receipt,record)):
            raise CommandRejected('Receipt does not match this pending external call')
        await insert_command(db,command,actor_user_id,digest)
        stage_wakeup(db, "commands")
        return AcceptedCommand(command.command_id,task['id'],'accepted',False)
