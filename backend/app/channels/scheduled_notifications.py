"""Schedule result notifications are independent of task execution and admission."""
import uuid
from fastapi import HTTPException
from workmate_product.config.channels import ChannelPolicy
from workmate_product.persistence.models import (ChannelOutbox, Schedule, Task, Conversation, User, SecurityAuditEvent)
from workmate_product.persistence.repositories import channels as channel_repo
from workmate_product.persistence.repositories import schedule_notifications as repository
from app.config import get_settings
from .rules import render_body


def policy():
    return ChannelPolicy.from_settings(get_settings())


def unavailable(channel, user_id):
    settings = policy()
    return (not settings.enabled or not channel.enabled or channel.deleted_at is not None
            or channel.owner_user_id not in (None, user_id)
            or (channel.provider == 'wechat' and not settings.wechat_enabled))


async def notification_targets(db, user_id):
    return {'items': [{'binding_id': binding.id, 'channel_id': channel.id,
        'channel_name': channel.name, 'provider': channel.provider,
        'external_user_id': binding.external_user_id,
        'available': not unavailable(channel, user_id),
        'reason': '渠道未启用或不可用' if unavailable(channel, user_id) else None}
        for binding, channel in await repository.targets(db,user_id)]}


async def snapshot_target(db, user_id, notification, *, strict=False):
    if not notification:
        return None
    frozen = {key: notification.get(key) for key in ('binding_id','on_success','on_failure')}
    frozen['user_id'] = user_id
    pair = await repository.binding_channel(db,notification.get('binding_id'),user_id)
    if pair:
        binding, channel = pair
        frozen.update(channel_id=channel.id,channel_name=channel.name,provider=channel.provider,
                      external_user_id=binding.external_user_id)
    if not pair or unavailable(pair[1],user_id):
        if strict:
            raise HTTPException(400,detail='请选择本人已绑定且已启用的通知渠道')
        frozen['unavailable'] = True
    return frozen


async def build_target(db, snapshot):
    pair = await repository.binding_channel(db,snapshot.get('binding_id'),snapshot.get('user_id'))
    if not pair:
        return None, '通知身份已解绑或不可用'
    binding, channel = pair
    if (binding.channel_id != snapshot.get('channel_id') or binding.external_user_id != snapshot.get('external_user_id')
            or channel.provider != snapshot.get('provider') or unavailable(channel,binding.user_id)):
        return None, '通知渠道或身份已变更'
    inbox = await repository.latest_context(db,binding)
    context = dict(inbox.target.get('reply_context') or {}) if inbox else {}
    if channel.provider == 'wechat' and not context.get('context_token'):
        return None, '请先在微信私聊机器人发送一条消息，再重试通知'
    # Proactive destinations never reuse callback-specific enterprise credentials.
    if channel.provider != 'wechat':
        context = {}
    target = {'chat_type':'private','sender_id':binding.external_user_id,
              'chat_id':inbox.chat_id if inbox else binding.external_user_id,'reply_context':context}
    if channel.provider == 'feishu' and not inbox:
        target['receive_id_type'] = 'open_id'
    return target, None


def render_notification(occurrence, task, text, url, *, has_artifacts=False):
    name = str(occurrence.snapshot.get('name') or '定时任务')[:80]
    if task and task.status == 'completed':
        body = f'定时任务「{name}」已完成。\n' + (text.strip() or '请在工作台查看结果。')
        if has_artifacts and url:
            body += '\n查看文件：' + url
        elif not text.strip() and url:
            body += '\n查看结果：' + url
    else:
        labels = {'blocked':'未能执行','missed':'已错过执行时间','skipped_busy':'因上一轮尚未结束而跳过'}
        reason = labels.get(occurrence.outcome,'未能完成')
        body = f'定时任务「{name}」{reason}。'
        if url:
            body += '\n查看结果：' + url
    return render_body(body,url)


async def allowed_delivery(db, row, admission):
    occurrence = await repository.occurrence(db,row.schedule_occurrence_id)
    snapshot = (occurrence.snapshot.get('notification') or {}) if occurrence else {}
    schedule = await db.get(Schedule,occurrence.schedule_id) if occurrence else None
    user = await db.get(User,row.user_id) if row.user_id else None
    if not schedule or schedule.deleted_at or schedule.owner_user_id != row.user_id or not user or user.status != 'active':
        return False
    if snapshot.get('user_id') != row.user_id or snapshot.get('channel_id') != row.channel_id:
        return False
    target, _ = await build_target(db,snapshot)
    if not target or target['sender_id'] != row.target.get('sender_id') or row.target.get('chat_type') != 'private':
        return False
    agent_id = occurrence.snapshot.get('agent_id')
    if row.task_id:
        task = await db.get(Task,row.task_id)
        conv = await db.get(Conversation,task.conversation_id) if task else None
        if not conv or conv.deleted_at or conv.user_id != row.user_id or task.id != occurrence.task_id:
            return False
        agent_id = conv.agent_id
    authorized = bool(agent_id and await admission._authorize(db,row.user_id,agent_id))
    if authorized:
        row.target = target  # Refresh short-lived reply context inside the claim transaction.
    return authorized


async def notification_status(db, occurrence_id):
    occurrence = await repository.occurrence(db,occurrence_id)
    snapshot = occurrence.snapshot.get('notification') if occurrence else None
    if not snapshot:
        return None
    row = await repository.delivery(db,occurrence_id)
    if row:
        return {'id':row.id,'status':row.status,'last_error':row.last_error,'attempts':row.attempts,
                'sent_at':row.sent_at,'can_retry':row.status in ('failed','unknown','cancelled') and row.attempts < 5,
                'requires_confirmation':row.status == 'unknown','channel_name':snapshot.get('channel_name'),
                'provider':snapshot.get('provider')}
    task = await db.get(Task,occurrence.task_id) if occurrence.task_id else None
    terminal = not task or task.status in ('completed','failed','cancelled','interrupted')
    wanted = snapshot.get('on_success') if task and task.status == 'completed' else snapshot.get('on_failure')
    if terminal and not wanted:
        return None
    return {'id':None,'status':'failed' if snapshot.get('unavailable') else 'waiting',
            'last_error':'通知目标已失效' if snapshot.get('unavailable') else None,'attempts':0,
            'sent_at':None,'can_retry':False,'requires_confirmation':False,
            'channel_name':snapshot.get('channel_name'),'provider':snapshot.get('provider')}


async def retry_notification(db,user_id,schedule_id,occurrence_id,*,confirm_duplicates=False,admission=None):
    occurrence = await repository.occurrence(db,occurrence_id,lock=True)
    schedule = await db.get(Schedule,schedule_id)
    if not occurrence or occurrence.schedule_id != schedule_id or not schedule or schedule.owner_user_id != user_id or schedule.deleted_at:
        raise HTTPException(404,detail='通知记录不存在')
    row = await repository.delivery(db,occurrence_id,lock=True)
    if not row or row.status not in ('failed','unknown','cancelled') or row.attempts >= 5:
        raise HTTPException(409,detail='当前通知不可重试')
    if row.status == 'unknown' and not confirm_duplicates:
        raise HTTPException(409,detail='发送结果未知，重试可能重复发送，请确认后重试')
    if admission is None or not await allowed_delivery(db,row,admission):
        raise HTTPException(409,detail='通知身份、渠道或访问权限已失效')
    target,error = await build_target(db,occurrence.snapshot['notification'])
    if error:
        raise HTTPException(409,detail=error)
    row.target, row.status, row.last_error = target,'pending',None
    row.next_attempt_at = await channel_repo.now(db)
    row.lease_owner = row.lease_until = None
    db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type='schedule_notification_retry',actor_user_id=user_id,
        metadata_={'schedule_id':schedule_id,'occurrence_id':occurrence_id,'delivery_id':row.id,
                   'confirmed_duplicate_risk':confirm_duplicates}))
    await db.flush()
    return await notification_status(db,occurrence_id)


async def project_notifications(service):
    async with service.sessions() as db:
        ids = await repository.candidates(db,service.policy.batch_size)
    for occurrence_id in ids:
        async with service.sessions() as db:
            occurrence = await repository.occurrence(db,occurrence_id,lock=True)
            if not occurrence or await repository.delivery(db,occurrence_id):
                continue
            snapshot = occurrence.snapshot['notification']
            task = await db.get(Task,occurrence.task_id) if occurrence.task_id else None
            target,error = await build_target(db,snapshot)
            target = target or {'chat_type':'private','sender_id':snapshot.get('external_user_id'),'chat_id':snapshot.get('external_user_id')}
            text = await channel_repo.final_text(db,task.id) if task and task.status == 'completed' else ''
            url = service.links.conversation(occurrence.conversation_id) if occurrence.conversation_id else service.links.base_url.rstrip('/') + '/workspace/schedules'
            row = ChannelOutbox(id=str(uuid.uuid4()),channel_id=snapshot['channel_id'],
                schedule_occurrence_id=occurrence.id,user_id=snapshot['user_id'],task_id=occurrence.task_id,
                kind='schedule_result',dedupe_key='schedule:' + occurrence.id,target=target,
                text=render_notification(occurrence,task,text,url,has_artifacts=bool(task and await channel_repo.has_artifacts(db,task.id))),reply_finished=True,
                status='failed' if error else 'pending',last_error=error)
            db.add(row)
            await db.flush()
            if not error and not await allowed_delivery(db,row,service.admission):
                row.status,row.last_error = 'cancelled','通知身份、渠道或访问权限已失效'
            await db.commit()
    # Connections that were disabled have no delivery runner; still expose the
    # cancellation immediately instead of leaving their notifications pending.
    async with service.sessions() as db:
        rows = await repository.pending(db,service.policy.batch_size,
            after=getattr(service,'_schedule_notification_check_cursor',None))
        for row in rows:
            if not await allowed_delivery(db,row,service.admission):
                row.status,row.last_error = 'cancelled','通知身份、渠道或访问权限已失效'
        await db.commit()
        service._schedule_notification_check_cursor = (rows[-1].created_at,rows[-1].id) if len(rows) == service.policy.batch_size else None
