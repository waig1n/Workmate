"""Staged interaction ingestion and safe-boundary projection; no resume dispatch.

Only Worker-derived boundary evidence can expose a permission request. Original
request identity survives partial decisions; external execution remains internal.
"""
import hashlib
import json

from pydantic import ValidationError
from sqlalchemy import select

from workmate_contracts import BoundaryEvidence, PendingToolCall, snapshot_digest, MAX_PAYLOAD_BYTES
from workmate_product.projections.store import ProjectedEvent, ProjectionConflict, ProjectionResult
from workmate_product.approval import approval_request_version
from workmate_product.persistence.models import Task, TaskEvent, TaskMessage
from workmate_contracts.realtime import message_id
from .durable_lifecycle import finalize_terminal_boundary
from .durable_projection import live_projector, _audit, _TERMINAL
from .display_boundary import display_evidence


def interaction_projector():
    """Explicit new-path installation; does not enable existing HTTP/Worker roles."""
    return live_projector(finalize_boundary=_finalize,stage_interaction=_stage)


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def _identity(call,kind):
    try:
        return PendingToolCall(id=call['id'],name=call['name'],input=call['input'],kind=kind)
    except (KeyError,TypeError,ValidationError) as error:
        raise ProjectionConflict('Malformed interaction tool identity') from error


async def _stage(db,event,state):
    kind='permission' if event.event_type=='REQUIRE_USER_CONFIRM' else 'external_execution'
    calls=event.payload.get('tool_calls')
    if not isinstance(calls,list) or not calls or len(_canonical(calls).encode())>MAX_PAYLOAD_BYTES:
        raise ProjectionConflict('Interaction request must contain bounded original calls')
    identities=[_identity(call,kind) for call in calls]
    if len({call.id for call in identities})!=len(calls):
        raise ProjectionConflict('Duplicate interaction tool identity')
    records=state.setdefault('interactions',{})
    existing={(record['kind'],call['id']):(record,call)
              for record in records.values() for call in record['tool_calls']}
    known=0
    permission_parents={}
    for identity,raw in zip(identities,calls):
        prior=existing.get((kind,identity.id))
        if prior is None:
            other=existing.get(('permission' if kind=='external_execution' else 'external_execution',identity.id))
            if other:
                if (kind!='external_execution' or
                        identity!=_identity(other[1],kind) or
                        _canonical(raw.get('suggested_rules',[]))!=_canonical(other[1].get('suggested_rules',[]))):
                    raise ProjectionConflict('External transition requires the original approved call')
                permission_parents[identity.id]=other[0]['request_version']
            continue
        known+=1
        record,original=prior
        permission_parents.update(record.get('permission_parents',{}))
        if (identity!=_identity(original,record['kind']) or
                _canonical(raw.get('suggested_rules',[]))!=_canonical(original.get('suggested_rules',[]))):
            raise ProjectionConflict('An original interaction call cannot be replaced')
    await _check_external_parents(db,event.product_task_id,permission_parents,records)
    if known:
        if known!=len(calls):
            raise ProjectionConflict('Cannot merge a prior interaction with new calls')
        return ProjectionResult(state,[])  # Repeated/subset request keeps its original version.
    version=approval_request_version(event.reply_id,calls)
    if kind=='external_execution':
        version='external-'+version
    iid='interaction-'+hashlib.sha256((kind+':'+version).encode()).hexdigest()[:32]
    records[iid]={'kind':kind,'request_version':version,'reply_id':event.reply_id,
        'permission_parents':permission_parents,
        'tool_calls':calls,'source_event_ids':[event.runtime_event_id],'source_seq':event.runtime_seq,'published_pending_ids':None}
    return ProjectionResult(state,[])


def _evidence(event,boundary):
    try:
        evidence=BoundaryEvidence.model_validate(event.payload.get('boundary_evidence'))
    except ValidationError as error:
        raise ProjectionConflict('Missing or unsupported boundary evidence') from error
    if (not boundary or evidence.snapshot_sha256!=snapshot_digest(boundary['payload'])
            or [c.id for c in evidence.pending_tools]!=event.payload.get('pending_tool_call_ids')):
        raise ProjectionConflict('Boundary evidence does not match the immutable snapshot')
    if bool(evidence.pending_tools)!=(boundary['status']=='awaiting_interaction'):
        raise ProjectionConflict('Pending tools disagree with boundary execution status')
    return evidence


async def _history(db,task_id):
    rows=(await db.execute(select(TaskEvent.event_type,TaskEvent.payload).where(
        TaskEvent.task_id==task_id,TaskEvent.event_type.in_(
            ['hitl_request','hitl_decision','hitl_abandoned','hitl_resume_exhausted'])))).all()
    decided=set()
    closed=set()
    requested=set()
    for kind,payload in rows:
        if kind=='hitl_decision':
            decided.update(item['toolCallId'] for item in payload.get('decisions',[])
                           if isinstance(item,dict) and isinstance(item.get('toolCallId'),str))
        elif kind=='hitl_request':
            requested.add(payload.get('decisionVersion') or approval_request_version(
                payload.get('replyId',''),payload.get('toolCalls',[])))
        else:
            closed.add(payload.get('decisionVersion'))
    return decided,closed,requested


async def _finalize(db,event,state,boundary):
    task=await db.get(Task,event.product_task_id,populate_existing=True)
    if task is None or task.conversation_id!=event.conversation_id or task.run_id!=event.reply_id:
        raise ProjectionConflict('Interaction boundary belongs to a different Product Reply')
    if task.status in _TERMINAL:
        return ProjectionResult(state,[])
    evidence=_evidence(event,boundary)
    if boundary['status'] in _TERMINAL:
        return await finalize_terminal_boundary(db,event,state,boundary,interaction_closer=_close)
    records=state.get('interactions',{})
    decided,closed,requested=await _history(db,task.id)
    _coalesce(records,requested|closed,event.reply_id)
    by_call={(record['kind'],call['id']):(iid,record,call) for iid,record in records.items() for call in record['tool_calls']}
    groups={}
    for call in evidence.pending_tools:
        original=by_call.get((call.kind,call.id))
        if not original or _identity(original[2],original[1]['kind'])!=call or original[1]['reply_id']!=event.reply_id:
            raise ProjectionConflict('Pending tool has no matching original request')
        groups.setdefault(original[0],[]).append(call.id)
    parents={cid:version for iid in groups for cid,version in records[iid].get('permission_parents',{}).items()}
    await _check_external_parents(db,task.id,parents,records)
    actual={c.id for c in evidence.pending_tools}
    for record in records.values():
        if record['request_version'] in closed:
            continue
        outstanding={c['id'] for c in record['tool_calls']}-set(state.get('tool_results',{}))
        if record['kind']=='permission':
            outstanding-=decided
        if not outstanding<=actual:
            raise ProjectionConflict('Boundary omitted an unresolved original call')
    pending=[]
    public=[]
    display=None
    if task.status!='cancelling':
        public=_decision_events(records,decided,closed)
    for iid,ids in groups.items():
        record=records[iid]
        version=record['request_version']
        if version in closed:
            continue
        actionable=[cid for cid in ids if record['kind']=='external_execution' or cid not in decided]
        pending.append({'interaction_id':iid,'request_version':version,'kind':record['kind'],
                        'pending_tool_call_ids':ids,'awaiting_resume':not actionable})
        if record['kind']!='permission' or task.status=='cancelling':
            continue
        if not actionable:
            continue
        if version not in requested:
            await _audit(db,event,'hitl_request',{'toolCalls':record['tool_calls'],
                'replyId':event.reply_id,'decisionVersion':version,'approvalProfile':task.approval_profile})
        if record['published_pending_ids']!=actionable:
            if display is None:
                message=await db.get(TaskMessage,message_id(task.id,event.reply_id),populate_existing=True)
                display=display_evidence(event,message,phases={'approval'})
            # Order and suggested rules come from the immutable original request.
            calls=[call for call in record['tool_calls'] if call['id'] in actionable]
            public.append(ProjectedEvent(key=iid,event_type='CUSTOM',payload={'type':'CUSTOM',
                'name':'HITL_APPROVAL_REQUEST','value':{'toolCalls':calls,'decisionVersion':version,
                    'taskId':task.id,'approvalProfile':task.approval_profile},**display}))
            record['published_pending_ids']=actionable
    if task.status!='cancelling' and any(p['kind']=='permission' and not p['awaiting_resume'] for p in pending):
        task.status='awaiting_permission'
    elif task.status=='awaiting_permission' and pending and all(p['kind']=='external_execution' for p in pending):
        task.status='running'
    state['pending_interactions']=pending
    state['interaction_boundary_id']=boundary['id']
    return ProjectionResult(state,public)


async def _close(db,event,state,boundary):
    """Seal all permission requests with terminal evidence, not just the latest."""
    decided,closed,requested=await _history(db,event.product_task_id)
    records=state.get('interactions',{})
    versions={record['request_version'] for record in records.values() if record['kind']=='permission'}
    if not requested<=versions:
        raise ProjectionConflict('Missing original durable interaction history')
    for record in records.values():
        version=record['request_version']
        if record['kind']!='permission' or version in closed or version not in requested:
            continue
        ids={call['id'] for call in record['tool_calls']}
        if not ids<=decided:
            kind='hitl_abandoned'
        elif not ids<=set(state.get('tool_results',{})):
            kind='hitl_resume_exhausted'
        else:
            continue  # Decided and executed; never claim its resume was exhausted.
        await _audit(db,event,kind,{'decisionVersion':version,'replyId':event.reply_id,
            'sealedBy':'safe_execution_boundary'})
    state['pending_interactions']=[]


def _coalesce(records,visible_versions,reply_id):
    """Core emits one REQUIRE per call; expose one Product request per new batch.

    Only never-published requests can be combined. Once the first safe boundary
    establishes a Product version, partial decisions retain that original batch.
    """
    candidates=sorted([(iid,r) for iid,r in records.items()
        if r['kind']=='permission' and r['request_version'] not in visible_versions
        and r['published_pending_ids'] is None],key=lambda item:item[1]['source_seq'])
    if len(candidates)<2:
        return
    calls=[call for _,record in candidates for call in record['tool_calls']]
    version=approval_request_version(reply_id,calls)
    iid='interaction-'+hashlib.sha256(('permission:'+version).encode()).hexdigest()[:32]
    combined={**candidates[0][1],'request_version':version,'tool_calls':calls,
        'source_event_ids':[eid for _,r in candidates for eid in r['source_event_ids']]}
    for old,_ in candidates:
        del records[old]
    records[iid]=combined


async def _approved_calls(db,task_id):
    approvals=set()
    denied=set()
    payloads=await db.scalars(select(TaskEvent.payload).where(
        TaskEvent.task_id==task_id,TaskEvent.event_type=='hitl_decision'))
    for payload in payloads:
        for item in payload.get('decisions',[]):
            if isinstance(item,dict) and isinstance(item.get('toolCallId'),str):
                (approvals if item.get('approved') is True else denied).add(item['toolCallId'])
    return approvals-denied


def _decision_events(records,decided,closed):
    events=[]
    for iid,record in records.items():
        if record['kind']!='permission' or record['request_version'] in closed:
            continue
        answered=[call['id'] for call in record['tool_calls'] if call['id'] in decided]
        if answered and record.get('published_decided_ids')!=answered:
            events.append(ProjectedEvent(key=iid+':decision',event_type='CUSTOM',payload={'type':'CUSTOM',
                'name':'HITL_DECISION_RECORDED','value':{'decisionVersion':record['request_version'],
                                                     'decidedToolCallIds':answered}}))
            record['published_decided_ids']=answered
    return events


async def _check_external_parents(db,task_id,parents,records):
    if not parents:
        return  # Direct external execution need not have had a permission ASK.
    approved=await _approved_calls(db,task_id)
    _,closed,requested=await _history(db,task_id)
    originals={(record['request_version'],call['id']) for record in records.values()
               if record['kind']=='permission' for call in record['tool_calls']}
    if any(cid not in approved or version in closed or version not in requested
           or (version,cid) not in originals for cid,version in parents.items()):
        raise ProjectionConflict('External execution parent approval is closed or no longer valid')
