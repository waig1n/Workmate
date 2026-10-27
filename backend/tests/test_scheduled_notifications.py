from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app.channels import scheduled_notifications as notifications


def test_notification_text_is_bounded_and_never_exposes_failure_reason():
    occurrence = SimpleNamespace(snapshot={'name': 'Daily report'}, outcome='blocked', reason='private secret')
    assert 'secret' not in notifications.render_notification(occurrence, None, '', 'https://work.test')
    task = SimpleNamespace(status='completed')
    text = notifications.render_notification(occurrence, task, '汉字'*4000, 'https://work.test')
    assert len(text.encode()) <= 3500


async def test_snapshot_freezes_binding_identity_and_flags(monkeypatch):
    binding = SimpleNamespace(id='b',user_id='u',external_user_id='external',channel_id='ch')
    channel = SimpleNamespace(id='ch',name='Bot',provider='feishu',enabled=True,deleted_at=None,owner_user_id=None)
    monkeypatch.setattr(notifications.repository,'binding_channel',AsyncMock(return_value=(binding,channel)))
    monkeypatch.setattr(notifications,'policy',lambda:SimpleNamespace(enabled=True,wechat_enabled=True))
    result = await notifications.snapshot_target(None,'u',{'binding_id':'b','on_success':True,'on_failure':False},strict=True)
    assert result['external_user_id']=='external' and result['user_id']=='u'
    assert result['channel_id']=='ch' and result['on_failure'] is False


async def test_invalid_snapshot_does_not_block_task_and_strict_rejects(monkeypatch):
    monkeypatch.setattr(notifications.repository,'binding_channel',AsyncMock(return_value=None))
    result = await notifications.snapshot_target(None,'u',{'binding_id':'gone','on_success':True},strict=False)
    assert result['unavailable'] and result['binding_id']=='gone'
    with pytest.raises(HTTPException):
        await notifications.snapshot_target(None,'u',{'binding_id':'gone'},strict=True)

from test_channel_processing import channels
from test_native_admission import admission_database
from test_durable_projection import projection_database
from test_schedules import definition
from app.services.schedules import ScheduleService
from app.channels.supervisor import ChannelSupervisor
from workmate_product.persistence.models import (ChannelOutbox, ChannelIdentity, ChannelConnection, ScheduleOccurrence,
                                              Task, Conversation, TaskMessage)
from sqlalchemy import select


async def scheduled(channels, **flags):
    service,sessions,admin = channels
    schedules = ScheduleService(sessions,service.admission)
    async with sessions() as db:
        row = await schedules.create(db,'u',definition(notification={'binding_id':'identity','on_success':True,'on_failure':True,**flags}))
        schedule_id,due = row.id,row.next_fire_at
    await schedules.tick(now=due)
    async with sessions() as db:
        occurrence = await db.scalar(select(ScheduleOccurrence).where(ScheduleOccurrence.schedule_id == schedule_id))
        return schedules,occurrence.id,occurrence.task_id


async def test_projection_waits_gate_and_deduplicates_without_changing_task(channels):
    service,sessions,admin = channels
    _,occurrence_id,task_id = await scheduled(channels)
    await notifications.project_notifications(service)
    assert await admin.fetchval('SELECT count(*) FROM channel_outbox') == 0
    async with sessions.begin() as db:
        task = await db.get(Task,task_id)
        task.status = 'completed'
        db.add(TaskMessage(id='final',task_id=task.id,conversation_id=task.conversation_id,role='assistant',content={'text':'PRIVATE RESULT'}))
    await notifications.project_notifications(service)
    assert await admin.fetchval('SELECT count(*) FROM channel_outbox') == 0
    async with sessions.begin() as db:
        conv = await db.get(Conversation,(await db.get(Task,task_id)).conversation_id)
        conv.active_product_task_id = None
    import asyncio
    await asyncio.gather(notifications.project_notifications(service),notifications.project_notifications(service))
    async with sessions() as db:
        rows = list((await db.scalars(select(ChannelOutbox))).all())
        assert len(rows) == 1 and rows[0].inbox_id is None
        assert rows[0].schedule_occurrence_id == occurrence_id and rows[0].kind == 'schedule_result'
        assert rows[0].target['receive_id_type'] == 'open_id'
        assert 'PRIVATE RESULT' in rows[0].text
        assert await ChannelSupervisor(service).allowed_delivery(db,await db.get(ChannelConnection,'ch'),rows[0])
    assert await admin.fetchval('SELECT origin FROM tasks WHERE id=$1',task_id) == 'schedule'


async def test_unbind_cancels_notification_and_unknown_retry_requires_confirmation(channels):
    service,sessions,admin = channels
    schedules,occurrence_id,task_id = await scheduled(channels)
    async with sessions.begin() as db:
        task = await db.get(Task,task_id)
        task.status = 'failed'
        (await db.get(Conversation,task.conversation_id)).active_product_task_id = None
    await notifications.project_notifications(service)
    async with sessions.begin() as db:
        row = await notifications.repository.delivery(db,occurrence_id)
        row.status = 'unknown'
        occurrence = await db.get(ScheduleOccurrence,occurrence_id)
        with pytest.raises(HTTPException) as error:
            await notifications.retry_notification(db,'u',occurrence.schedule_id,occurrence_id,admission=service.admission)
        assert error.value.status_code == 409
        result = await notifications.retry_notification(db,'u',occurrence.schedule_id,occurrence_id,admission=service.admission,confirm_duplicates=True)
        assert result['status'] == 'pending'
    async with sessions.begin() as db:
        await db.delete(await db.get(ChannelIdentity,'identity'))
    await notifications.project_notifications(service)
    async with sessions() as db:
        result = await notifications.notification_status(db,occurrence_id)
        assert result['status'] == 'cancelled'
    assert await admin.fetchval('SELECT status FROM tasks WHERE id=$1',task_id) == 'failed'


async def test_general_channel_retry_cannot_bypass_schedule_safety(channels):
    from app.channels.management import ChannelManagement
    service,sessions,admin = channels
    _,occurrence_id,task_id = await scheduled(channels)
    async with sessions.begin() as db:
        task = await db.get(Task,task_id)
        task.status = 'failed'
        (await db.get(Conversation,task.conversation_id)).active_product_task_id = None
    await notifications.project_notifications(service)
    async with sessions() as db:
        row = await notifications.repository.delivery(db,occurrence_id)
        row.status = 'unknown'
        with pytest.raises(HTTPException) as error:
            await ChannelManagement.retry(None,db,'u',row.id)
        assert error.value.detail == '请使用定时任务通知重试入口'


async def test_failed_task_with_success_only_does_not_remain_waiting(channels):
    service,sessions,_ = channels
    _,occurrence_id,task_id = await scheduled(channels,on_failure=False)
    async with sessions.begin() as db:
        task = await db.get(Task,task_id)
        task.status = 'failed'
        (await db.get(Conversation,task.conversation_id)).active_product_task_id = None
    await notifications.project_notifications(service)
    async with sessions() as db:
        assert await notifications.repository.delivery(db,occurrence_id) is None
        assert await notifications.notification_status(db,occurrence_id) is None


async def test_taskless_failure_projects_fixed_message_without_task_creation(channels):
    from datetime import timedelta
    service,sessions,admin = channels
    _,occurrence_id,_ = await scheduled(channels)
    async with sessions.begin() as db:
        original = await db.get(ScheduleOccurrence,occurrence_id)
        db.add(ScheduleOccurrence(id='missed-notice',schedule_id=original.schedule_id,
            schedule_version=original.schedule_version,scheduled_for=original.scheduled_for+timedelta(days=1),
            triggered_at=original.triggered_at,outcome='missed',reason='PRIVATE SECRET',snapshot=original.snapshot))
    count = await admin.fetchval('SELECT count(*) FROM tasks')
    await notifications.project_notifications(service)
    async with sessions() as db:
        row = await notifications.repository.delivery(db,'missed-notice')
        assert row.task_id is None and row.status == 'pending'
        assert '已错过执行时间' in row.text and 'PRIVATE SECRET' not in row.text
    assert await admin.fetchval('SELECT count(*) FROM tasks') == count


async def test_send_timeout_is_unknown_and_does_not_rerun_task(channels):
    from test_channel_supervisor import Transport
    from workmate_product.persistence.repositories import channels as channel_repo
    service,sessions,admin = channels
    _,occurrence_id,task_id = await scheduled(channels)
    async with sessions.begin() as db:
        task = await db.get(Task,task_id)
        task.status = 'completed'
        (await db.get(Conversation,task.conversation_id)).active_product_task_id = None
    await notifications.project_notifications(service)
    supervisor = ChannelSupervisor(service)
    async with sessions.begin() as db:
        channel = await channel_repo.claim_connection(db,'ch',supervisor.owner,60)
        epoch,version = channel.lease_epoch,channel.version
    transport = Transport(TimeoutError('PRIVATE'))
    transport.supports_typing = lambda: True
    transport.stop_typing = AsyncMock()
    await supervisor.send_one('ch',epoch,version,transport)
    await supervisor.send_one('ch',epoch,version,transport)
    assert len(transport.sent) == 1
    transport.stop_typing.assert_not_awaited()
    async with sessions() as db:
        result = await notifications.notification_status(db,occurrence_id)
        assert result['status']=='unknown' and result['requires_confirmation']
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='schedule'") == 1


async def test_wechat_context_requires_private_history_since_current_binding(channels,monkeypatch):
    from workmate_product.persistence.models import ChannelInbox
    from datetime import timedelta
    service,sessions,_ = channels
    monkeypatch.setattr(notifications,'policy',lambda:SimpleNamespace(enabled=True,wechat_enabled=True))
    async with sessions.begin() as db:
        channel = await db.get(ChannelConnection,'ch')
        channel.provider='wechat'
        binding = await db.get(ChannelIdentity,'identity')
        snapshot = await notifications.snapshot_target(db,'u',{'binding_id':'identity','on_success':True},strict=True)
        for id,kind,when in [('old','private',binding.created_at-timedelta(seconds=1)),('group','group',binding.created_at+timedelta(seconds=1))]:
            db.add(ChannelInbox(id=id,channel_id='ch',channel_version=1,message_id=id,sender_id='external',chat_id='dm',chat_type=kind,
                text='ignored',target={'reply_context':{'context_token':'unsafe'}},user_id='u',created_at=when))
        await db.flush()
        target,error = await notifications.build_target(db,snapshot)
        assert target is None and '私聊' in error
        db.add(ChannelInbox(id='new',channel_id='ch',channel_version=1,message_id='new',sender_id='external',chat_id='dm',chat_type='private',
            text='hello',target={'reply_context':{'context_token':'current'}},user_id='u',created_at=binding.created_at+timedelta(seconds=2)))
        await db.flush()
        target,error = await notifications.build_target(db,snapshot)
        assert error is None and target['reply_context']['context_token']=='current'


async def test_pending_wechat_notification_refreshes_context_at_delivery_authorization(channels,monkeypatch):
    from workmate_product.persistence.models import ChannelInbox
    from datetime import timedelta
    service,sessions,_ = channels
    monkeypatch.setattr(notifications,'policy',lambda:SimpleNamespace(enabled=True,wechat_enabled=True))
    async with sessions.begin() as db:
        channel = await db.get(ChannelConnection,'ch')
        channel.provider = 'wechat'
        binding = await db.get(ChannelIdentity,'identity')
        since = binding.created_at
        db.add(ChannelInbox(id='first-private',channel_id='ch',channel_version=1,message_id='first-private',sender_id='external',chat_id='dm',chat_type='private',
            text='hello',target={'reply_context':{'context_token':'first'}},user_id='u',created_at=since+timedelta(seconds=1),status='ignored'))
    _,occurrence_id,task_id = await scheduled(channels)
    async with sessions.begin() as db:
        task = await db.get(Task,task_id)
        task.status = 'completed'
        (await db.get(Conversation,task.conversation_id)).active_product_task_id = None
    await notifications.project_notifications(service)
    async with sessions.begin() as db:
        row = await notifications.repository.delivery(db,occurrence_id)
        assert row.target['reply_context']['context_token'] == 'first'
        db.add(ChannelInbox(id='latest-private',channel_id='ch',channel_version=1,message_id='latest-private',sender_id='external',chat_id='dm',chat_type='private',
            text='hello',target={'reply_context':{'context_token':'latest'}},user_id='u',created_at=since+timedelta(seconds=2),status='ignored'))
        await db.flush()
        assert await notifications.allowed_delivery(db,row,service.admission)
        assert row.target['reply_context']['context_token'] == 'latest'


def test_short_success_has_no_url_but_artifact_empty_and_failure_do():
    occurrence = SimpleNamespace(snapshot={'name':'Report'},outcome='task_created')
    task = SimpleNamespace(status='completed')
    url = 'https://work.test/workspace/c/c'
    assert url not in notifications.render_notification(occurrence,task,'Actual answer',url)
    assert '查看文件：' + url in notifications.render_notification(occurrence,task,'Actual answer',url,has_artifacts=True)
    assert url in notifications.render_notification(occurrence,task,'',url)
    assert url in notifications.render_notification(occurrence,SimpleNamespace(status='failed'),'private failure',url)


async def test_pending_recheck_rotates_past_an_offline_but_valid_first_row(channels):
    from datetime import timedelta
    service,sessions,_ = channels
    _,occurrence_id,task_id = await scheduled(channels)
    async with sessions.begin() as db:
        task = await db.get(Task,task_id)
        task.status='completed'
        (await db.get(Conversation,task.conversation_id)).active_product_task_id=None
    await notifications.project_notifications(service)
    async with sessions.begin() as db:
        original = await db.get(ScheduleOccurrence,occurrence_id)
        first = await notifications.repository.delivery(db,occurrence_id)
        snapshot = {**original.snapshot,'notification':{**original.snapshot['notification'],'binding_id':'revoked'}}
        db.add(ScheduleOccurrence(id='later-occurrence',schedule_id=original.schedule_id,schedule_version=original.schedule_version,
            scheduled_for=original.scheduled_for+timedelta(days=1),triggered_at=original.triggered_at,outcome='blocked',snapshot=snapshot))
        await db.flush()
        db.add(ChannelOutbox(id='later-delivery',channel_id='ch',schedule_occurrence_id='later-occurrence',user_id='u',
            kind='schedule_result',dedupe_key='later-delivery',target=dict(first.target),text='fixed',status='pending',
            reply_finished=True,created_at=first.created_at+timedelta(seconds=1)))
    service.policy=service.policy.model_copy(update={'batch_size':1})
    service._schedule_notification_check_cursor=None
    await notifications.project_notifications(service)
    async with sessions() as db:
        assert (await notifications.repository.delivery(db,'later-occurrence')).status=='pending'
    await notifications.project_notifications(service)
    async with sessions() as db:
        assert (await notifications.repository.delivery(db,'later-occurrence')).status=='cancelled'
        assert (await notifications.repository.delivery(db,occurrence_id)).status=='pending'


async def test_targets_and_retry_cap_are_owner_scoped(channels):
    service,sessions,_ = channels
    _,occurrence_id,task_id = await scheduled(channels)
    async with sessions.begin() as db:
        assert (await notifications.notification_targets(db,'u'))['items'][0]['binding_id']=='identity'
        assert (await notifications.notification_targets(db,'other'))['items']==[]
        task=await db.get(Task,task_id)
        task.status='failed'
        (await db.get(Conversation,task.conversation_id)).active_product_task_id=None
    await notifications.project_notifications(service)
    async with sessions.begin() as db:
        row=await notifications.repository.delivery(db,occurrence_id)
        row.status,row.attempts='failed',5
        await db.flush()
        assert not (await notifications.notification_status(db,occurrence_id))['can_retry']
        occurrence=await db.get(ScheduleOccurrence,occurrence_id)
        with pytest.raises(HTTPException) as error:
            await notifications.retry_notification(db,'other',occurrence.schedule_id,occurrence_id,admission=service.admission)
        assert error.value.status_code==404
        with pytest.raises(HTTPException) as error:
            await notifications.retry_notification(db,'u',occurrence.schedule_id,occurrence_id,admission=service.admission)
        assert error.value.status_code==409
