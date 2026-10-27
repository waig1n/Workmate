"""Persist platform input, then admit under the existing Product authority."""
import hashlib
import json
import uuid
from datetime import timedelta
from fastapi import HTTPException
from workmate_product.application.task_capacity import TaskCapacityPolicy, TaskCapacityExceeded
from workmate_product.application.task_controls import TaskControls, TaskControlConflict
from workmate_product.application.commands import CommandRejected, CommandConflict
from workmate_product.persistence.repositories import channels as repo
from workmate_product.persistence.repositories.task_capacity import lock_capacity
from workmate_product.persistence.models import (ChannelInbox, ChannelOutbox, ChannelIdentity,
    ChannelConversation, Conversation, Task, User)
from ..config import get_settings
from ..services.runtime_errors import ModelConfigError
from .management import ChannelManagement, audit
from .rules import render_reply, render_body


class ChannelService(ChannelManagement):
    def url(self, conversation_id=None):
        return self.links.conversation(conversation_id) if conversation_id else self.links.channels()

    async def ingest(self, channel_id, message, *, owner=None, epoch=None, version=None):
        """Callback returns only after durable commit; failures prevent platform ACK."""
        async with self.sessions() as db:
            channel = (await repo.lease_valid(db,channel_id,owner,epoch,version) if owner else
                       await repo.channel_by_id(db,channel_id,lock=True))
            if not channel or not channel.enabled or channel.deleted_at:
                raise ValueError('channel_inactive')
            existing = await repo.inbox_existing(db,channel_id,message.message_id)
            if existing:
                return existing.id
            if (await repo.pending_count(db,channel_id) >= self.policy.max_inbox_pending
                    or await repo.pending_deliveries(db,channel_id) >= self.policy.max_inbox_pending):
                raise ValueError('channel_inbox_full')
            if any(len(value) > 256 for value in (message.message_id,message.sender_id,message.chat_id)):
                raise ValueError('channel_identity_too_large')
            if len(json.dumps(message.target())) > 32768:
                raise ValueError('channel_context_too_large')
            ignored = message.chat_type == 'group' and (not channel.group_enabled
                or not message.mentioned or message.chat_id not in channel.group_allowlist
                or channel.provider == 'wechat')
            unsupported = message.unsupported or len(message.text) > self.policy.message_max_chars
            text = message.text[:self.policy.message_max_chars].strip()
            # A binding code is an authentication secret; store only its digest.
            if text.startswith('/bind '):
                text = '/bind sha256:' + hashlib.sha256(text[6:].strip().encode()).hexdigest()
            identity = await repo.identity(db,channel_id,message.sender_id)
            target = message.target()
            target['received_at'] = (await repo.now(db)).isoformat()
            row = ChannelInbox(id=str(uuid.uuid4()),channel_id=channel_id,channel_version=channel.version,
                user_id=identity.user_id if identity and not text.startswith('/bind') else None,
                message_id=message.message_id,sender_id=message.sender_id,chat_id=message.chat_id,
                chat_type=message.chat_type,text=text,target=target,mentioned=message.mentioned,
                unsupported=unsupported,status='ignored' if ignored else 'pending')
            db.add(row)
            await db.commit()
            return row.id

    async def outbox(self, db, inbox, kind, text, *, task=None, key=None):
        key = key or inbox.id + ':' + kind
        if await repo.outbox_exists(db,key):
            return
        db.add(ChannelOutbox(id=str(uuid.uuid4()),channel_id=inbox.channel_id,inbox_id=inbox.id,
            task_id=task.id if task else None,user_id=inbox.user_id,kind=kind,dedupe_key=key,
            target=dict(inbox.target),text=text,status='pending'))

    async def bind(self, db, channel, inbox):
        if inbox.chat_type != 'private' or channel.provider == 'wechat':
            await self.outbox(db,inbox,'binding','请私聊企业机器人完成身份绑定。')
            return
        instant = await repo.now(db)
        digest = inbox.text.removeprefix('/bind sha256:')
        code = await repo.code(db,digest) if len(digest) == 64 else None
        existing = await repo.identity(db,channel.id,inbox.sender_id)
        valid = (code and code.channel_id == channel.id and code.consumed_at is None
                 and code.expires_at > instant and await repo.binding_attempts(db,inbox,instant) <= 10)
        user = await db.get(User,code.user_id) if valid else None
        if not user or user.status != 'active' or existing and existing.user_id != user.id:
            await self.outbox(db,inbox,'binding','绑定失败，请在工作台重新获取绑定码；已有绑定请先解除。')
            return
        if not existing:
            db.add(ChannelIdentity(id=str(uuid.uuid4()),channel_id=channel.id,
                external_user_id=inbox.sender_id,user_id=user.id))
        code.consumed_at = instant
        inbox.user_id = user.id
        audit(db,user.id,channel.id,'bound')
        await self.outbox(db,inbox,'binding','身份绑定成功，可以发送文字任务。')

    async def conversation(self, db, channel, inbox, *, new=False):
        key = 'private' if inbox.chat_type == 'private' else 'group:' + inbox.chat_id
        mapping = await repo.mapping(db,channel.id,inbox.user_id,channel.agent_id,key)
        conv = await db.get(Conversation,mapping.conversation_id) if mapping else None
        if new or not conv or conv.deleted_at:
            conv = Conversation(id=str(uuid.uuid4()),user_id=inbox.user_id,agent_id=channel.agent_id,
                title=channel.name + ' · IM')
            db.add(conv)
            await db.flush()
            if mapping:
                mapping.conversation_id = conv.id
            else:
                db.add(ChannelConversation(id=str(uuid.uuid4()),channel_id=channel.id,
                    user_id=inbox.user_id,agent_id=channel.agent_id,chat_key=key,conversation_id=conv.id))
        return conv

    async def handle(self, db, channel, inbox):
        if inbox.text.startswith('/bind'):
            await self.bind(db,channel,inbox)
            return
        identity = await repo.identity(db,channel.id,inbox.sender_id)
        if (not identity or inbox.user_id is None or identity.user_id != inbox.user_id
                or channel.owner_user_id not in (None,identity.user_id)):
            await self.outbox(db,inbox,'binding','请先登录工作台绑定当前平台身份。\n' + self.url())
            return
        user = await db.get(User,identity.user_id)
        if not user or user.status != 'active':
            inbox.status = 'rejected'
            return
        inbox.user_id = user.id
        command = inbox.text.strip()
        if command != '/stop':
            if not await self.admission._authorize(db,user.id,channel.agent_id):
                raise HTTPException(403,'access_denied')
            if channel.owner_user_id is None:
                await self.validate_target(db,channel.agent_id)
        if inbox.unsupported or not inbox.text.strip():
            await self.outbox(db,inbox,'unsupported','当前仅支持文字任务，请在工作台提交文件。\n' + self.url())
            return
        command = inbox.text.strip()
        conv = await self.conversation(db,channel,inbox,new=command == '/new')
        inbox.conversation_id = conv.id
        if command == '/new':
            await self.outbox(db,inbox,'new','已开始新会话。\n'+self.url(conv.id))
        elif command in ('/status','/stop'):
            task = await db.get(Task,conv.active_product_task_id) if conv.active_product_task_id else None
            task = task or await repo.latest_task(db,conv.id)
            if not task:
                text = '当前会话没有任务。\n' + self.url(conv.id)
            elif command == '/stop' and task.status not in ('completed','failed','cancelled','interrupted'):
                await TaskControls(knowledge_enabled=self.admission.knowledge_enabled).stage_stop(db,user,conv,task)
                text = '已受理停止请求，请在工作台查看。\n' + self.url(conv.id)
            else:
                text = render_reply(inbox.chat_type,task.status,'',self.url(conv.id))
            await self.outbox(db,inbox,'control',text,task=task)
        else:
            await TaskCapacityPolicy.from_settings(get_settings()).check(db,user.id)
            task, _ = await self.admission.stage_task(db,user,conv,inbox.text,channel_inbox=inbox)
            await self.create_reply(db, inbox, task, channel.provider)

    async def create_reply(self, db, inbox, task, provider):
        due = inbox.created_at + timedelta(seconds=self.policy.reply_delay_secs)
        state = {'hint_at': due.isoformat()}
        # WeCom callbacks have a short first-response window. Attempt an empty
        # native stream early; acceptance/visual behavior is platform dependent.
        if provider == 'wecom' and inbox.chat_type == 'private':
            state = {'bootstrap': True, 'hint_at': due.isoformat()}
            due = await repo.now(db)
        row = ChannelOutbox(id=str(uuid.uuid4()), channel_id=inbox.channel_id, inbox_id=inbox.id,
            task_id=task.id, user_id=inbox.user_id, kind='reply', dedupe_key=inbox.id+':reply',
            target=dict(inbox.target), text='正在处理…', status='pending', next_attempt_at=due,
            reply_revision=1, reply_finished=False, reply_state=state)
        db.add(row)
        return row

    async def update_reply(self, inbox_id, text, *, owner=None, epoch=None, version=None):
        """Coalesce at the configured tick, not once per model token."""
        lease = (owner,epoch,version)
        if any(value is not None for value in lease) and not all(value is not None for value in lease):
            raise ValueError('Incomplete channel lease')
        async with self.sessions() as db:
            if owner is not None:
                hint = await db.get(ChannelInbox,inbox_id)
                if not hint or not await repo.lease_valid(db,hint.channel_id,owner,epoch,version):
                    return
            inbox = await repo.locked_inbox(db, inbox_id)
            if not inbox or inbox.chat_type != 'private':
                return
            task = await db.get(Task, inbox.task_id)
            if not task or task.status not in ('created','running'):
                return
            row = await repo.reply_slot(db, inbox_id)
            if not row or row.reply_finished and row.status != 'sent':
                return
            body = render_body(text, self.url(inbox.conversation_id))
            if not body:
                # A later tool call can reclassify visible text as process text.
                # Clear it in the existing card/stream without inventing progress.
                if not (row.reply_state or {}).get('body_seen'):
                    return
                body = '…'
            if row.text == body and not row.reply_finished:
                return
            row.text, row.reply_finished = body, False
            row.reply_revision += 1
            row.reply_state = {**row.reply_state, 'body_seen': True}
            if row.status == 'sent':
                row.status = 'pending'
            instant = await repo.now(db)
            row.next_attempt_at = max(instant, row.sent_at + timedelta(seconds=self.policy.stream_interval_secs)) if row.sent_at else instant
            await db.commit()

    async def process_one(self, inbox_id):
        async with self.sessions() as db:
            # Same order as configuration/admission. Inbox+Task+Start+receipt commit together.
            await lock_capacity(db)
            hint = await db.get(ChannelInbox,inbox_id)
            if not hint:
                return
            channel = await repo.channel_by_id(db,hint.channel_id,lock=True)
            inbox = await repo.locked_inbox(db,inbox_id)
            if inbox.status != 'pending':
                return
            if (not self.policy.enabled or not channel or not channel.enabled or channel.deleted_at
                    or channel.version != inbox.channel_version
                    or channel.provider == 'wechat' and not self.policy.wechat_enabled):
                inbox.status,inbox.error = 'rejected','channel_configuration_changed'
            else:
                try:
                    async with db.begin_nested():
                        await self.handle(db,channel,inbox)
                        if inbox.status == 'pending':
                            inbox.status = 'processed'
                        await db.flush()
                except (HTTPException,ModelConfigError,TaskCapacityExceeded,TaskControlConflict,
                        CommandRejected,CommandConflict):
                    inbox = await repo.locked_inbox(db,inbox_id)
                    inbox.status,inbox.error = 'rejected','product_admission_rejected'
                    await self.outbox(db,inbox,'rejected','任务未受理，请登录工作台检查权限、配置或容量后重试。\n'+self.url())
            await db.commit()

    async def project_results(self):
        async with self.sessions() as db:
            ids = await repo.projection_candidates(db,self.policy.batch_size)
        for id in ids:
            async with self.sessions() as db:
                inbox = await repo.locked_inbox(db,id)
                task = await db.get(Task,inbox.task_id)
                conv = await db.get(Conversation,inbox.conversation_id)
                if (not task or not conv or task.status not in
                    ('completed','failed','cancelled','interrupted','awaiting_permission','awaiting_external_result')):
                    continue
                if task.status in ('completed','failed','cancelled','interrupted') and conv.active_product_task_id == task.id:
                    continue
                key = inbox.id + ':' + task.status + ':' + (task.last_decision_version or '')
                row = await repo.reply_slot(db, inbox.id)
                if await repo.outbox_exists(db,key) or row and row.boundary_key == key:
                    continue
                answer = await repo.final_text(db,task.id) if inbox.chat_type == 'private' and task.status == 'completed' else ''
                if row is None:
                    channel = await repo.channel_by_id(db,inbox.channel_id)
                    row = await self.create_reply(db,inbox,task,channel.provider)
                artifacts = task.status == 'completed' and inbox.chat_type == 'private' and await repo.has_artifacts(db,task.id)
                row.text = render_reply(inbox.chat_type,task.status,answer,self.url(conv.id),has_artifacts=artifacts)
                row.reply_finished, row.boundary_key = True, key
                row.reply_revision = (row.reply_revision or 1) + 1
                row.next_attempt_at = await repo.now(db)
                if row.status == 'sent':
                    row.status = 'pending'
                await db.commit()
