"""Personal scheduled admission; each firing owns one short atomic transaction."""
import asyncio
import logging
import uuid

from fastapi import HTTPException

from workmate_product.config.schedules import SchedulePolicy
from workmate_product.schedules.rules import ScheduleInput, next_fire, missed_slots
from workmate_product.persistence.models import (
    Schedule, ScheduleOccurrence, Conversation, Task, Agent, User, SecurityAuditEvent,
)
from workmate_product.persistence.repositories import schedules as repository
from workmate_product.persistence.repositories.task_capacity import database_time, lock_capacity
from workmate_product.application.task_capacity import TaskCapacityPolicy, TaskCapacityExceeded
from ..config import get_settings
from .execution_admission import selected_model
from .runtime_errors import ModelConfigError

logger = logging.getLogger(__name__)


class ScheduleService:
    def __init__(self, sessions, admission, policy=None):
        self.sessions, self.admission = sessions, admission
        self.policy = policy or SchedulePolicy.from_settings(get_settings())

    def _enabled(self):
        if not self.policy.enabled:
            raise HTTPException(503, detail='定时任务功能已停用')

    async def _validate(self, db, user_id, body):
        self._enabled()
        if not await self.admission._authorize(db, user_id, body.agent_id):
            raise HTTPException(403, detail='当前 Agent 或其能力不可用')
        if body.notification is not None:
            from ..channels.scheduled_notifications import snapshot_target
            await snapshot_target(db,user_id,body.notification.model_dump(mode='json'),strict=True)
        try:
            return selected_model(body.model_profile)
        except ModelConfigError as error:
            raise HTTPException(422, detail='所选模型不可用') from error

    def _audit(self, db, user_id, schedule_id, action, version):
        db.add(SecurityAuditEvent(id=str(uuid.uuid4()), event_type='schedule_' + action,
            actor_user_id=user_id, metadata_={'schedule_id': schedule_id, 'version': version}))

    async def get(self, db, user_id, schedule_id):
        row = await repository.owned(db, user_id, schedule_id)
        if row is None:
            raise HTTPException(404, detail='定时任务不存在')
        return row

    async def _locked(self, db, user_id, schedule_id, version):
        await lock_capacity(db)
        row = await repository.owned(db, user_id, schedule_id, lock=True)
        if row is None:
            raise HTTPException(404, detail='定时任务不存在')
        if row.version != version:
            raise HTTPException(409, detail='定时任务已更新，请刷新后重试')
        return row

    async def create(self, db, user_id, body):
        await lock_capacity(db)
        await self._validate(db, user_id, body)
        due = next_fire(body, await database_time(db))
        if due is None:
            raise HTTPException(422, detail='请选择未来的执行时间')
        row = Schedule(id=str(uuid.uuid4()), owner_user_id=user_id,
            **body.model_dump(mode='json'), version=1, status='active', next_fire_at=due)
        db.add(row)
        self._audit(db, user_id, row.id, 'created', 1)
        await db.commit()
        await db.refresh(row)
        return row

    async def update(self, db, user_id, schedule_id, body):
        row = await self._locked(db, user_id, schedule_id, body.version)
        await self._validate(db, user_id, body)
        due = next_fire(body, await database_time(db))
        if due is None:
            raise HTTPException(422, detail='请选择未来的执行时间')
        for key, value in body.model_dump(mode='json', exclude={'version'}).items():
            setattr(row, key, value)
        row.version += 1
        if row.status == 'completed':
            row.status = 'paused'
        row.next_fire_at = due if row.status == 'active' else None
        row.pause_reason = None
        self._audit(db, user_id, row.id, 'updated', row.version)
        await db.commit()
        await db.refresh(row)
        return row

    async def set_active(self, db, user_id, schedule_id, version, active):
        row = await self._locked(db, user_id, schedule_id, version)
        if active:
            body = self._definition(row)
            await self._validate(db, user_id, body)
            due = next_fire(body, await database_time(db))
            if due is None:
                raise HTTPException(422, detail='执行时间已过，请先编辑时间')
            row.next_fire_at = due
        else:
            row.next_fire_at = None
        row.status = 'active' if active else 'paused'
        row.pause_reason = None
        row.version += 1
        self._audit(db, user_id, row.id, 'resumed' if active else 'paused', row.version)
        await db.commit()
        await db.refresh(row)
        return row

    async def delete(self, db, user_id, schedule_id, version):
        row = await self._locked(db, user_id, schedule_id, version)
        row.deleted_at = await database_time(db)
        row.status, row.next_fire_at = 'paused', None
        row.version += 1
        self._audit(db, user_id, row.id, 'deleted', row.version)
        await db.commit()

    @staticmethod
    def _definition(row):
        return ScheduleInput.model_validate({key: getattr(row, key) for key in
            ('name','agent_id','prompt','model_profile','timezone','rule','notification')})

    async def _occurrence(self, db, row):
        from ..channels.scheduled_notifications import notification_status
        task = await db.get(Task, row.task_id) if row.task_id else None
        return {**{key: getattr(row, key) for key in ('id','scheduled_for','triggered_at',
            'outcome','reason','task_id','conversation_id','missed_count')},
            'task_status': task.status if task else None,
            'notification': await notification_status(db,row.id)}

    async def serialize(self, db, row):
        agent = await db.get(Agent, row.agent_id)
        history, _ = await repository.history(db, row.id, 1)
        return {**self._definition(row).model_dump(mode='json'),
            **{key: getattr(row, key) for key in ('id','version','status','next_fire_at',
                'pause_reason','created_at','updated_at')},
            'agent_name': agent.display_name if agent else '不可用 Agent',
            'latest_occurrence': await self._occurrence(db, history[0]) if history else None}

    async def list(self, db, user_id, limit=20, offset=0):
        rows, total = await repository.page(db, user_id, limit, offset)
        return {'items': [await self.serialize(db, row) for row in rows], 'total': total}

    async def occurrences(self, db, user_id, schedule_id, limit=20, offset=0):
        await self.get(db, user_id, schedule_id)
        rows, total = await repository.history(db, schedule_id, limit, offset)
        return {'items': [await self._occurrence(db, row) for row in rows], 'total': total}

    async def notification_targets(self, db, user_id):
        from ..channels.scheduled_notifications import notification_targets
        return await notification_targets(db,user_id)

    async def retry_notification(self, db, user_id, schedule_id, occurrence_id, *, confirm_duplicates=False):
        from ..channels.scheduled_notifications import retry_notification
        await self.get(db,user_id,schedule_id)
        occurrence = await repository.occurrence(db,schedule_id,occurrence_id)
        if occurrence is None:
            raise HTTPException(404, detail='执行记录不存在')
        result = await retry_notification(db,user_id,schedule_id,occurrence_id,
            confirm_duplicates=confirm_duplicates,admission=self.admission)
        await db.commit()
        return result

    async def _fire(self, db, row, now):
        if await repository.model_failed(db, row.id, row.version):
            row.status, row.next_fire_at = 'paused', None
            row.pause_reason = '执行节点报告模型不可用，请修复后重新启用'
            self._audit(db, row.owner_user_id, row.id, 'model_unavailable', row.version)
            return
        body, due = self._definition(row), row.next_fire_at
        outcome, reason, count = 'task_created', None, 0
        model = None
        if (now - due).total_seconds() > self.policy.misfire_grace_secs:
            outcome, reason = 'missed', '超过执行宽限时间，未补跑'
            count = missed_slots(body, due, now)
        elif not await self.admission._authorize(db, row.owner_user_id, row.agent_id):
            outcome, reason = 'blocked', '任务所有者、Agent 或所需能力的授权已失效'
        else:
            try:
                model = selected_model(row.model_profile)
            except ModelConfigError:
                outcome, reason = 'blocked', '所选模型不可用'
            if outcome == 'task_created' and await repository.busy(db, row.id):
                outcome, reason = 'skipped_busy', '上一次任务尚未完成交付'
        notification = None
        if body.notification is not None:
            from ..channels.scheduled_notifications import snapshot_target
            notification = await snapshot_target(db,row.owner_user_id,
                body.notification.model_dump(mode='json'),strict=False)
        occurrence = ScheduleOccurrence(id=str(uuid.uuid4()), schedule_id=row.id,
            schedule_version=row.version, scheduled_for=due, triggered_at=now,
            outcome=outcome, reason=reason, missed_count=count,
            snapshot={'owner_user_id': row.owner_user_id, 'agent_id': row.agent_id,
                'version': row.version, 'prompt': row.prompt,
                'name': row.name, 'timezone': row.timezone, 'rule': row.rule,
                'model_profile': model.profile_name if model else row.model_profile,
                'approval_profile': 'unattended', 'notification': notification})
        db.add(occurrence)
        await db.flush()
        if outcome == 'task_created':
            await TaskCapacityPolicy.from_settings(get_settings()).check(db, row.owner_user_id)
            conversation = Conversation(id=str(uuid.uuid4()), user_id=row.owner_user_id,
                agent_id=row.agent_id, title=row.name, model_profile=model.profile_name)
            db.add(conversation)
            await db.flush()
            user = await db.get(User, row.owner_user_id)
            _, queued = await self.admission.stage_task(db, user, conversation, row.prompt,
                row.name, approval_profile='unattended', occurrence=occurrence)
            if queued:
                raise HTTPException(503, detail='执行节点暂不可用')
        if outcome == 'blocked':
            row.status, row.pause_reason, row.next_fire_at = 'paused', reason, None
        else:
            row.next_fire_at = next_fire(body, now)
            if row.next_fire_at is None:
                row.status = 'completed'
        self._audit(db, row.owner_user_id, row.id, outcome, row.version)

    async def tick(self, now=None):
        if not self.policy.enabled:
            return 0
        seen, processed = [], 0
        for _ in range(self.policy.batch_size):
            async with self.sessions() as db:
                try:
                    async with db.begin():
                        await lock_capacity(db)
                        instant = now if now is not None else await database_time(db)
                        row = await repository.failed_model_schedule(db, seen)
                        if row is None:
                            row = await repository.due(db, instant, seen)
                        if row is None:
                            break
                        seen.append(row.id)
                        await self._fire(db, row, instant)
                    processed += 1
                except TaskCapacityExceeded:
                    continue
                except HTTPException as error:
                    if error.status_code not in {429, 503}:
                        raise
        return processed

    async def serve(self, shutdown):
        while not shutdown.is_set():
            try:
                await self.tick()
            except Exception as error:
                # DB exception text can contain prompt parameters and credentials.
                logger.error('Schedule admission iteration failed (%s)', type(error).__name__)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=self.policy.poll_interval_secs)
            except TimeoutError:
                pass
