"""Fenced Product platform writes and receipts in one caller-owned transaction.

No network/filesystem effects or commit occur here. Business failures produce a
failed receipt and an error result; the Worker raises only after committing it.
Authority/input failures raise before receipt creation and cannot mutate Product.
"""
import base64
import hashlib
import hmac
import json
import uuid
from pydantic import ValidationError
from sqlalchemy import select,text
from sqlalchemy.exc import IntegrityError
from ..persistence.models import ToolExecutionReceipt,TaskEvent
from .execution_context import _fresh_identity_map
from .platform_types import PlatformToolRequest,PlatformToolError,_AgentCreateArgs,_AgentUpdateArgs,_SkillInstallArgs
from .agent_mutations import create_agent,update_agent
from .platform_authority import authorize_platform_call
from .skill_installation import SkillInstallationError
from .skill_artifacts import SkillArtifactError


def request_hash(request):
    return hashlib.sha256(json.dumps({'tool':request.tool_name,'arguments':request.arguments},
        sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


async def _approved_submission(db,req):
    events=(await db.scalars(select(TaskEvent).where(TaskEvent.task_id==req.task_id,
        TaskEvent.event_type.in_(['hitl_request','hitl_decision'])).order_by(TaskEvent.seq.desc()))).all()
    for event in events:
        if event.event_type!='hitl_request' or event.payload.get('replyId')!=req.reply_id:
            continue
        call=next((c for c in event.payload.get('toolCalls',[]) if c.get('id')==req.tool_call_id),None)
        if call is None:
            continue
        version=event.payload.get('decisionVersion')
        for decision in events:
            value=decision.payload
            if (decision.event_type!='hitl_decision' or decision.seq<=event.seq or not version
                    or value.get('requestVersion')!=version or value.get('actorUserId')!=req.actor_user_id):
                continue
            grant=next((d for d in value.get('decisions',[]) if d.get('toolCallId')==req.tool_call_id),None)
            if not grant:
                continue
            if grant.get('approved') is not True or call.get('name')!=req.tool_name:
                break
            approved=call.get('input',{})
            if isinstance(approved,str):
                approved=json.loads(approved)
            if not isinstance(approved,dict):
                break
            return approved
        break
    raise PlatformToolError(403,'Explicit submission confirmation required')


async def _mutation(db,req,user,access):
    if req.tool_name=='agent_create':
        return await create_agent(db,access,user,req.arguments)
    if req.tool_name=='agent_update':
        failures=(await db.scalars(select(ToolExecutionReceipt.response).where(
            ToolExecutionReceipt.task_id==req.task_id,ToolExecutionReceipt.tool_name=='agent_update',
            ToolExecutionReceipt.status=='failed'))).all()
        if sum(bool(r and r.get('target_agent_id')==req.arguments['agent_id'] and r.get('detail')=='Agent version changed') for r in failures)>=3:
            raise PlatformToolError(409,'Agent update conflict limit reached for this task; stop retrying and ask the user to start a new task')
        return await update_agent(db,access,user,req.arguments)
    from .skill_uploads import upload_skill_archive
    args=_SkillInstallArgs.model_validate(req.arguments)
    try:
        archive=base64.b64decode(args.archive_base64,validate=True)
    except ValueError as error:
        raise PlatformToolError(422,'Invalid Skill archive') from error
    if len(archive)>20*1024*1024:
        raise PlatformToolError(413,'Skill archive exceeds 20 MiB')
    if not hmac.compare_digest(hashlib.sha256(archive).hexdigest(),args.archive_sha256):
        raise PlatformToolError(409,'Skill draft changed')
    installed=await upload_skill_archive(db,actor_user_id=user.id,scope_id=args.scope_id,raw=archive,
        filename=args.filename,display_name=args.display_name,description=args.description,creation_channel="assistant")
    return {'installed':True,'bound_agent_ids':[],'skill':{'id':installed.id,'scope_id':installed.scope_id,'url':'/workspace/skills'}}


@_fresh_identity_map
async def execute_platform_mutation(db,request,*,knowledge_enabled=False):
    req=PlatformToolRequest.model_validate(request).model_copy(deep=True)
    if req.tool_name not in {'agent_create','agent_update','skill_install'}:
        raise PlatformToolError(404,'Unknown mutation tool')
    user,access=await authorize_platform_call(db,req,knowledge_enabled=knowledge_enabled)
    digest=request_hash(req)
    receipt=await db.scalar(select(ToolExecutionReceipt).where(ToolExecutionReceipt.task_id==req.task_id,
        ToolExecutionReceipt.tool_call_id==req.tool_call_id))
    if receipt:
        if receipt.request_hash!=digest or receipt.tool_name!=req.tool_name:
            raise PlatformToolError(409,'Tool call id was already used with different input')
        if receipt.status not in {'completed','failed'} or receipt.response is None:
            raise PlatformToolError(409,'Previous tool execution outcome is pending; no retry was run')
        return {**receipt.response,'error':True} if receipt.status=='failed' else receipt.response
    approved=await _approved_submission(db,req)
    try:
        if req.tool_name=='skill_install':
            args=_SkillInstallArgs.model_validate(req.arguments)
            matches=(args.archive_sha256==approved.get('expected_sha256') and args.scope_id==approved.get('scope_id')
                and args.display_name==approved.get('display_name') and args.description==approved.get('description'))
        else:
            schema=_AgentCreateArgs if req.tool_name=='agent_create' else _AgentUpdateArgs
            matches=schema.model_validate(req.arguments)==schema.model_validate(approved)
    except (ValidationError,ValueError) as error:
        raise PlatformToolError(422,'Invalid submission arguments') from error
    if not matches:
        raise PlatformToolError(409,'Submission changed; confirm the new content')
    receipt=ToolExecutionReceipt(id=str(uuid.uuid4()),task_id=req.task_id,tool_call_id=req.tool_call_id,
        tool_name=req.tool_name,request_hash=digest,status='pending')
    db.add(receipt)
    await db.flush()
    try:
        async with db.begin_nested():
            result=await _mutation(db,req,user,access)
            await db.flush()
    except (PlatformToolError,SkillInstallationError,SkillArtifactError,ValidationError,IntegrityError) as error:
        result={'error':True,'status_code':getattr(error,'status_code',422 if isinstance(error,ValidationError) else 409),
            'detail':getattr(error,'detail','Invalid or conflicting Product mutation'),'target_agent_id':req.arguments.get('agent_id')}
        receipt.status='failed'
    else:
        receipt.status='completed'
    receipt.response=result
    await db.flush()
    # Locks fence Stop/ownership changes; time can still advance while awaiting
    # business locks. Reject and roll back the entire transaction after expiry.
    if not await db.scalar(text('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=:id'),{'id':req.session_id}):
        raise PlatformToolError(409,'Platform tool lease expired during mutation')
    return result
