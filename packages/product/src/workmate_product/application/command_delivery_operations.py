"""Explicit operator redrive of quarantined, never-claimed command intent.

This does not execute or re-author commands, replace actors or reset approvals.
The caller owns the transaction; all failed validation must roll it back.
"""
import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession
from workmate_contracts import Command
from workmate_contracts.controls import ResumePayload, StopPayload
from ..persistence.repositories import command_delivery_operations as repository
from ..persistence.repositories import control_admission as controls
from ..persistence.repositories.node_placement import NodePlacementRepository
from ..persistence.recovery_provenance import valid_release
from .authorization import AccessDecisionService
from .commands import CommandRejected
from .control_commands import _binding, _digest, _transaction, _utc
from .full_access import stage_full_access
from .knowledge_access import unavailable_knowledge_bindings
from .task_capacity import TaskCapacityPolicy


def _require(condition, reason):
    if not condition:
        raise CommandRejected(reason)


async def _current_access(db, actor, agent_id, knowledge_enabled):
    # Do not reuse an ORM identity map populated before ordered admission locks.
    from ..persistence.models import Agent
    await db.flush()
    async with AsyncSession(bind=await db.connection(),expire_on_commit=False,
                            join_transaction_mode='rollback_only') as fresh:
        await fresh.begin()
        access=AccessDecisionService(fresh,knowledge_enabled=knowledge_enabled)
        agent=await fresh.get(Agent,agent_id)
        return (agent is not None and await access.can_use_agent(actor,agent_id)
            and not await access.unavailable_capability_ids(actor,agent_id)
            and not await unavailable_knowledge_bindings(fresh,actor,agent,enabled=knowledge_enabled,access=access))


async def _resources_known(db, saved, session, execution):
    parked=await repository.suspension(db,session) if session['snapshot_id'] else None
    if parked is not None and (parked['status']!='released' or parked['resume_epoch'] is not None):
        return False
    if saved['session_owner_id'] is not None:
        return bool(saved['session_lease_live'])
    if session['active_task_id'] is None:
        return execution['status']=='accepted' and execution['execution_epoch']==0 and execution['reply_id'] is None
    return (valid_release(parked) and parked['resume_epoch'] is None
        and parked['task_id']==execution['task_id'] and parked['session_id']==session['id']
        and parked['source_epoch']==session['execution_epoch'] and parked['reply_id']==session['reply_id']
        and parked['storage_node_id']==session['storage_node_id']
        and parked['state_version']==session['state_version'] and parked['runtime_seq']==session['runtime_seq'])


async def _resume_boundary(db, command, task, session, execution, history, policy):
    payload=ResumePayload.model_validate(command.payload)
    _require(task['status']=='awaiting_permission' and task['last_decision_version']==payload.decision_version,
             'Resume decision is no longer current')
    for row in (session,execution):
        _require(row['status']=='awaiting_interaction' and row['snapshot_id']==payload.snapshot_id
            and row['state_version']==payload.state_version,'Resume snapshot is stale')
    snapshot=await controls.snapshot(db,id=payload.snapshot_id)
    projection=await controls.lock_projection(db,id=task['id'])
    cursor=await controls.projection_cursor(db,id=command.conversation_id)
    _require(snapshot and snapshot['session_id']==session['id'] and snapshot['task_id']==task['id']
        and snapshot['reply_id']==command.reply_id and snapshot['execution_epoch']==command.expected_epoch
        and snapshot['status']=='awaiting_interaction' and snapshot['state_version']==payload.state_version
        and snapshot['runtime_seq']==session['runtime_seq'] and projection and cursor
        and projection['last_runtime_seq']==snapshot['runtime_seq'] and cursor['last_runtime_seq']==snapshot['runtime_seq']
        and projection['session_id']==session['id'] and projection['reply_id']==command.reply_id
        and projection['format_version']==1 and cursor['session_id']==session['id'],
        'Resume projection is not at the original immutable boundary')
    state=projection['state']
    record=state.get('interactions',{}).get(command.interaction_id)
    pending=next((p for p in state.get('pending_interactions',[]) if p['interaction_id']==command.interaction_id),None)
    _require(record and pending and state.get('interaction_boundary_id')==payload.snapshot_id
        and record.get('reply_id')==command.reply_id
        and all(p.get('kind')=='permission' and p.get('request_version')==command.request_version for p in (record,pending)),
        'Approval request is no longer current')
    decisions=[{'toolCallId':d.tool_call_id,'approved':d.approved} for d in payload.decisions]
    receipt=next((r['payload'] for r in history if r['event_type']=='hitl_decision'
        and r['payload'].get('decisionVersion')==payload.decision_version),None)
    _require(receipt and receipt.get('requestVersion')==command.request_version
        and receipt.get('decisions')==decisions,'Original approval consent receipt is missing or different')
    _require(not await policy.expired(db,task['id'],command.request_version),'Approval request expired')
    if payload.full_access:
        await stage_full_access(db,command,task,session,record=False)


async def _stop_authority(db, command, actor, conversation, history, policy):
    reason=StopPayload.model_validate(command.payload).reason
    receipt=next((r['payload'] for r in history if r['event_type']=='task_cancel_requested'
        and r['payload'].get('actorUserId')==actor and r['payload'].get('reason')==reason),None)
    _require(receipt,'Original Stop authorization receipt is missing')
    if reason=='user_requested':
        # Accepted safety cancellation survives later account disable, matching
        # Worker Stop authority. The current operator is independently root-authorized.
        _require(conversation['user_id']==actor,'Original Stop actor is no longer the owner')
    elif reason=='account_disabled':
        target=await repository.user(db,conversation['user_id'])
        audit=await controls.disable_receipt(db,id=receipt.get('disableEventId'))
        valid=False
        try:
            valid=bool(target and audit and _utc(target['disabled_at'])==_utc(audit['metadata'].get('disabled_at')))
        except (ValueError,TypeError,AttributeError):
            pass
        _require(receipt.get('commandId')==command.command_id and receipt.get('targetUserId')==conversation['user_id']
            and target and target['status']=='disabled' and target['disabled_by']==actor
            and audit and audit['event_type']=='user.disable' and audit['actor_user_id']==actor
            and audit['target_user_id']==conversation['user_id'] and valid,
            'Account-disable authorization receipt is stale')
    else:
        _require(receipt.get('commandId')==command.command_id and conversation['user_id']==actor
            and receipt.get('initiatedBy')=='approval_expiry_policy'
            and receipt.get('requestVersion') and await policy.expired(db,command.product_task_id,receipt['requestVersion']),
            'Approval-expiry authorization is stale')


async def redrive_command(db, command_id, *, operator_id, expected_generation, storage_node_id,
                          reason, knowledge_enabled=False, policy=TaskCapacityPolicy()):
    """Root-authorized operator action, preserving original actor and command."""
    _transaction(db)
    _require(isinstance(reason,str) and 1<=len(reason.strip())<=256,'A bounded operational reason is required')
    await controls.lock_organization_scopes(db)
    _require(operator_id in await AccessDecisionService(db).active_root_admin_ids(),
             'An active root administrator is required for redrive')
    context=await repository.lock_context(db,command_id)
    _require(context is not None,'Command unavailable')
    saved,delivery,agent,conversation,task,session,execution=context
    _require(saved and delivery and saved['status']=='accepted' and saved['claimed_by'] is None
        and saved['claimed_epoch'] is None and saved['claimed_at'] is None
        and delivery['status']=='quarantined' and delivery['delivery_generation']==expected_generation,
        'Only the exact latest quarantined never-claimed command may be redriven')
    _require(agent and conversation and task and session and execution
        and conversation['deleted_at'] is None and conversation['agent_id']==agent['id']
        and task['conversation_id']==conversation['id'] and conversation['active_product_task_id']==task['id']
        and session['conversation_id']==conversation['id'] and execution['session_id']==session['id']
        and session['storage_node_id']==storage_node_id and delivery['storage_node_id']==storage_node_id,
        'Task or storage binding is stale')
    _require(await NodePlacementRepository(db).nodes(conversation['user_id'],agent['id'])=={storage_node_id},
             'Storage placement is inconsistent')
    _require(await _resources_known(db,saved,session,execution),'Execution resources are not proven safe')
    command=Command(command_id=saved['id'],kind=saved['kind'],conversation_id=saved['conversation_id'],
        runtime_session_id=saved['session_id'],product_task_id=saved['task_id'],expected_epoch=saved['expected_epoch'],
        reply_id=saved['reply_id'],interaction_id=saved['interaction_id'],request_version=saved['request_version'],
        payload=saved['payload'],protocol_version=saved['protocol_version'])
    actor=saved['actor_user_id']
    history=await repository.audit_rows(db,task['id'])
    if command.kind=='StopTask':
        await _binding(db,command,conversation,stopping=True)
        _require(task['status']=='cancelling','Task no longer awaits Stop')
        await _stop_authority(db,command,actor,conversation,history,policy)
    else:
        _require(agent['deleted_at'] is None and conversation['user_id']==actor
            and await controls.actor_status(db,actor)=='active'
            and await _current_access(db,actor,agent['id'],knowledge_enabled),'Original actor no longer has Agent/capability access')
        if command.kind=='StartTask':
            digest=hashlib.sha256(json.dumps({'command':command.content_digest(),'actor_user_id':actor,
                'storage_node_id':storage_node_id},sort_keys=True,separators=(',',':')).encode()).hexdigest()
            _require(digest==saved['content_digest'],'Original Start parameters changed')
            _require(task['status']=='created' and session['active_task_id'] is None
                and session['execution_epoch']==command.expected_epoch
                and session['status'] in {'accepted','completed','failed','cancelled','interrupted'}
                and execution['status']=='accepted' and execution['execution_epoch']==0
                and execution['reply_id'] is None,'Start is no longer unclaimed')
        elif command.kind=='ResumeInteraction':
            _require(_digest(command,actor)==saved['content_digest'],'Original Resume parameters changed')
            await _binding(db,command,conversation)
            await _resume_boundary(db,command,task,session,execution,history,policy)
        else:
            raise CommandRejected('Unsupported redrive command')
    return await repository.redrive(db,saved,delivery,operator_id=operator_id,reason=reason.strip())
