"""Product task control use cases, independent of HTTP transport.

This outer use-case service owns commit/rollback; command admission functions
join its transaction. Worker execution remains asynchronous and durable.
"""
import json
import uuid
from ..persistence.repositories import task_controls as repository
from sqlalchemy.ext.asyncio import AsyncSession
from workmate_contracts import Command
from workmate_contracts.controls import ResumePayload,ToolDecision
from workmate_product.application.commands import CommandRejected,CommandConflict
from workmate_product.application.control_commands import (
    accept_resume, accept_stop, accept_disabled_user_stop, lock_disabled_user_conversation,
)
from workmate_product.application.full_access import accept_full_access
from workmate_product.application.knowledge_access import unavailable_knowledge_bindings
from workmate_product.projections.store import ProjectedEvent,append_business_events,lock_product_conversation
from workmate_product.application.authorization import AccessDecisionService
from .task_capacity import TaskCapacityPolicy, TaskCapacityExceeded


class TaskControlConflict(ValueError):
    """A task control request cannot be accepted against current facts."""


def _key(kind,task_id,value):
    canonical=json.dumps([kind,task_id,value],sort_keys=True,separators=(',',':'))
    return str(uuid.uuid5(uuid.NAMESPACE_URL,canonical))


class TaskControls:
    def __init__(self, *, knowledge_enabled=False, capacity_policy=TaskCapacityPolicy()):
        self.knowledge_enabled=knowledge_enabled
        self.capacity_policy=capacity_policy

    async def stop_disabled_user_tasks(self, db, *, actor_user_id, target_user_id, disable_event_id):
        """Persist each safety Stop independently after the account disable commit.

        Return failures to the administration audit without aborting cleanup of
        the other conversations. An admitted Task retains both execution gates.
        """
        rows = await repository.tasks_for_disabled_user(db, target_user_id)
        failures = []
        for item in rows:
            task_id, conversation_id = item['id'], item['conversation_id']
            try:
                if not db.in_transaction():
                    await db.begin()
                conversation = await lock_disabled_user_conversation(db,
                    conversation_id=conversation_id, actor_user_id=actor_user_id,
                    target_user_id=target_user_id, disable_event_id=disable_event_id)
                task = await repository.lock_task(db, task_id)
                if not task or task['conversation_id'] != conversation_id:
                    raise CommandRejected('Task unavailable')
                if task['status'] not in {'created','running','awaiting_permission','cancelling'}:
                    await db.commit()
                    continue
                execution = await repository.task_execution(db, task_id)
                command_id = _key('account-disable-stop', task_id, {'disable': disable_event_id})
                if execution is not None:
                    command = Command(command_id=command_id, kind='StopTask', conversation_id=conversation_id,
                        runtime_session_id=execution['session_id'], product_task_id=task_id,
                        payload={'reason': 'account_disabled'})
                    await accept_disabled_user_stop(db, command, actor_user_id=actor_user_id,
                        target_user_id=target_user_id, disable_event_id=disable_event_id)
                else:
                    if (task['status'] != 'created' or conversation['active_product_task_id'] == task_id
                            or await repository.task_command(db, task_id)
                            or await repository.active_session(db, task_id)):
                        raise CommandRejected('Task is not an unadmitted queued task')
                    await repository.cancel_queued_task(db, task_id)
                    await repository.audit_disabled_cancellation(db, task_id, conversation_id, actor_user_id, target_user_id, disable_event_id)
                    await append_business_events(db, conversation_id, operation_key=command_id, task_id=task_id,
                        items=[ProjectedEvent(key='cancel', event_type='CUSTOM', payload={'type': 'CUSTOM',
                            'name': 'TASK_CANCEL_REQUESTED', 'value': {'taskId': task_id, 'status': 'cancelled'}})])
                await db.commit()
            except Exception as error:
                await db.rollback()
                failures.append((task_id, error))
        return failures

    async def _authorize(self,db,actor_id,agent_id):
        async with AsyncSession(bind=await db.connection(),expire_on_commit=False,
                                join_transaction_mode='rollback_only') as fresh:
            await fresh.begin()
            return await self._current_access(fresh,actor_id,agent_id)

    async def _current_access(self,db,actor_id,agent_id):
        access=AccessDecisionService(db,knowledge_enabled=self.knowledge_enabled)
        if not await access.can_use_agent(actor_id,agent_id) or await access.unavailable_capability_ids(actor_id,agent_id):
            return False
        agent=await repository.agent_for_access(db,agent_id)
        return agent is not None and not await unavailable_knowledge_bindings(db,actor_id,agent,
            enabled=self.knowledge_enabled,access=access)

    async def confirm(self,db,user,conv,task,decisions,*,decision_version=None,full_access=False):
        actor_id,cid,tid=user.id,conv.id,task.id
        try:
            if not decision_version:
                raise CommandRejected('An exact approval decision version is required')
            # HTTP legacy name/input fields are never execution inputs. Original
            # parameters remain in the immutable Worker snapshot and projection.
            values=sorted((ToolDecision(tool_call_id=item['tool_call_id'],approved=item['approved'])
                for item in decisions),key=lambda d:d.tool_call_id)
            batch=_key('decision',tid,{'request':decision_version,'decisions':[v.model_dump() for v in values],
                'full_access':full_access})
            command_id=_key('resume',tid,batch)
            existing=await repository.existing_command(db, command_id)
            if existing is not None:
                command=Command(command_id=existing['id'],kind=existing['kind'],conversation_id=existing['conversation_id'],
                    runtime_session_id=existing['session_id'],product_task_id=existing['task_id'],
                    reply_id=existing['reply_id'],expected_epoch=existing['expected_epoch'],
                    interaction_id=existing['interaction_id'],request_version=existing['request_version'],payload=existing['payload'])
            else:
                row=await repository.approval_boundary(db, tid)
                if not row:
                    raise CommandRejected('Task is not awaiting permission')
                pending=next((p for p in row['state'].get('pending_interactions',[])
                    if p['kind']=='permission' and p['request_version']==decision_version),None)
                if pending is None:
                    raise CommandRejected('Approval request is stale or unavailable')
                payload=ResumePayload(decision_version=batch,snapshot_id=row['snapshot_id'],
                    state_version=row['state_version'],decisions=values,full_access=full_access)
                command=Command(command_id=command_id,kind='ResumeInteraction',conversation_id=cid,
                    runtime_session_id=row['session_id'],product_task_id=tid,reply_id=row['reply_id'],
                    expected_epoch=row['execution_epoch'],interaction_id=pending['interaction_id'],
                    request_version=decision_version,payload=payload.model_dump())
            accept=accept_full_access if full_access else accept_resume
            await accept(db,command,actor_user_id=actor_id,authorize_resume=self._authorize,
                         policy=self.capacity_policy)
            await db.commit()
            await db.refresh(task)
        except TaskCapacityExceeded:
            await db.rollback()
            raise
        except (CommandRejected,CommandConflict,ValueError,KeyError) as error:
            await db.rollback()
            raise TaskControlConflict(str(error)) from error

    async def stage_stop(self, db, user, conv, task):
        """Stage Stop facts in the caller transaction without commit or rollback.

        Channel ingestion commits this together with its inbox and reply outbox.
        Rejections leave transaction disposition to the caller.
        """
        actor_id,cid,tid=user.id,conv.id,task.id
        agent_id=await repository.conversation_agent(db, cid)
        await repository.lock_agent(db, agent_id)
        current=await lock_product_conversation(db,cid)
        if await repository.conversation_deleted_at(db, cid) is not None or current['user_id']!=actor_id or await repository.actor_status(db, actor_id)!='active':
            raise CommandRejected('Conversation unavailable to this actor')
        row=await repository.lock_task(db, tid)
        if not row or row['conversation_id']!=cid:
            raise CommandRejected('Task unavailable')
        execution=await repository.task_execution(db, tid)
        if execution is None:
            if (row['status']!='created' or current['active_product_task_id']==tid
                    or await repository.task_command(db, tid)):
                raise CommandRejected('Task is not an unadmitted queued task')
            await repository.cancel_queued_task(db, tid)
            await repository.audit_user_cancellation(db, tid, cid, actor_id)
            await append_business_events(db,cid,operation_key=_key('stop',tid,{}),task_id=tid,
                items=[ProjectedEvent(key='cancel',event_type='CUSTOM',payload={'type':'CUSTOM',
                    'name':'TASK_CANCEL_REQUESTED','value':{'taskId':tid,'status':'cancelled'}})])
        else:
            # Stable envelope for retries; shared service resolves live epoch
            # under locks and never releases execution or delivery gates.
            command=Command(command_id=_key('stop',tid,{}),kind='StopTask',conversation_id=cid,
                runtime_session_id=execution['session_id'],product_task_id=tid)
            await accept_stop(db,command,actor_user_id=actor_id)

    async def stop(self,db,user,conv,task):
        try:
            await self.stage_stop(db,user,conv,task)
            await db.commit()
            await db.refresh(task)
        except (CommandRejected,CommandConflict,ValueError) as error:
            await db.rollback()
            raise TaskControlConflict(str(error)) from error
