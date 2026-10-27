"""Durable delayed IM reply slot and coalesced streaming delivery."""
import asyncio
from datetime import timedelta
import pytest
from sqlalchemy import select
from app.channels.supervisor import ChannelSupervisor
from app.channels.rules import render_reply
from workmate_product.persistence.models import ChannelInbox, ChannelOutbox, Task, TaskMessage, Conversation
from workmate_product.persistence.repositories import channels as repo
from test_channel_processing import channels, message
from test_native_admission import admission_database
from test_durable_projection import projection_database


class StreamTransport:
    def __init__(self):
        self.calls=[]
    def supports_stream(self,target): return True
    async def stream(self,target,text,**kw):
        self.calls.append((text,kw))
        return kw['receipt'] or 'same-message'
    async def send(self,target,text):
        self.calls.append((text,{'plain':True}))
        return 'plain-message'


async def setup(channels):
    service,sessions,admin=channels
    id=await service.ingest('ch',message())
    await service.process_one(id)
    supervisor=ChannelSupervisor(service)
    async with sessions.begin() as db:
        ch=await repo.claim_connection(db,'ch',supervisor.owner,60)
        epoch,version=ch.lease_epoch,ch.version
    return service,sessions,admin,id,supervisor,epoch,version


async def test_delayed_hint_not_immediate_and_sent_only_once(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    transport=StreamTransport()
    assert not await sup.send_one('ch',epoch,version,transport)
    async with sessions() as db:
        inbox=await db.get(ChannelInbox,id)
        row=await db.scalar(select(ChannelOutbox).where(ChannelOutbox.inbox_id==id))
        assert row.kind=='reply'
        assert (row.next_attempt_at-inbox.created_at).total_seconds()==30
    await admin.execute("UPDATE channel_outbox SET next_attempt_at=now()-interval '1 second'")
    assert await sup.send_one('ch',epoch,version,transport)
    assert not await sup.send_one('ch',epoch,version,transport)
    assert transport.calls[0][0]=='正在处理…'


async def test_body_coalesces_and_final_reuses_one_message_without_link(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    transport=StreamTransport()
    await service.update_reply(id,'你')
    await service.update_reply(id,'你好')
    assert await sup.send_one('ch',epoch,version,transport)
    assert transport.calls[0][0]=='你好'
    async with sessions.begin() as db:
        inbox=await db.get(ChannelInbox,id)
        (await db.get(Task,inbox.task_id)).status='completed'
        (await db.get(Conversation,inbox.conversation_id)).active_product_task_id=None
        db.add(TaskMessage(id='answer',task_id=inbox.task_id,conversation_id=inbox.conversation_id,
                           role='assistant',content={'text':'你好，完成了。'}))
    await service.project_results()
    await service.update_reply(id,'late stale text')
    await admin.execute("UPDATE channel_outbox SET next_attempt_at=now()-interval '1 second'")
    await sup.send_one('ch',epoch,version,transport)
    assert transport.calls[-1][0]=='你好，完成了。'
    assert transport.calls[-1][1]['receipt']=='same-message'
    assert transport.calls[-1][1]['finish'] is True
    assert await admin.fetchval('SELECT count(*) FROM channel_outbox')==1
    await service.project_results()
    assert not await sup.send_one('ch',epoch,version,transport)


async def test_new_revision_during_send_is_not_lost(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    await service.update_reply(id,'first')
    class Racing(StreamTransport):
        async def stream(self,target,text,**kw):
            await service.update_reply(id,'second')
            return await super().stream(target,text,**kw)
    await sup.send_one('ch',epoch,version,Racing())
    assert await admin.fetchval('SELECT status FROM channel_outbox')=='pending'
    assert await admin.fetchval('SELECT text FROM channel_outbox')=='second'


async def test_unknown_create_blocks_automatic_later_revisions(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    await service.update_reply(id,'first')
    class Unknown(StreamTransport):
        async def stream(self,*args,**kw): raise TimeoutError()
    await sup.send_one('ch',epoch,version,Unknown())
    await service.update_reply(id,'latest')
    assert await admin.fetchval('SELECT status FROM channel_outbox')=='unknown'
    assert not await sup.send_one('ch',epoch,version,StreamTransport())


async def test_group_never_accepts_live_text(channels):
    service,sessions,admin=channels
    id=await service.ingest('ch',message('g',chat_id='group',chat_type='group',mentioned=True))
    await service.process_one(id)
    await service.update_reply(id,'private secret')
    assert await admin.fetchval('SELECT text FROM channel_outbox')=='正在处理…'


def test_normal_private_answer_has_no_workbench_signature():
    assert render_reply('private','completed','hello','https://work.test')=='hello'


@pytest.mark.parametrize('delay',[30,60])
async def test_delay_measured_from_input_and_configurable(channels,delay):
    from workmate_product.config.channels import ChannelPolicy
    service,sessions,admin=channels
    service.policy=ChannelPolicy(reply_delay_secs=delay)
    id=await service.ingest('ch',message())
    await service.process_one(id)
    async with sessions() as db:
        inbox=await db.get(ChannelInbox,id)
        reply=await repo.reply_slot(db,id)
        assert reply.next_attempt_at==inbox.created_at+timedelta(seconds=delay)


async def test_fast_completion_replaces_unsent_hint(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    async with sessions.begin() as db:
        inbox=await db.get(ChannelInbox,id)
        (await db.get(Task,inbox.task_id)).status='completed'
        (await db.get(Conversation,inbox.conversation_id)).active_product_task_id=None
        db.add(TaskMessage(id='fast',task_id=inbox.task_id,conversation_id=inbox.conversation_id,
                           role='assistant',content={'text':'fast answer'}))
    await service.project_results()
    transport=StreamTransport()
    await sup.send_one('ch',epoch,version,transport)
    assert [text for text,_ in transport.calls]==['fast answer']
    assert transport.calls[0][1]['finish'] is True


async def test_live_feed_coalesces_body_and_revocation_closes_it(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    started,closed=asyncio.Event(),asyncio.Event()
    class Feed:
        async def watch(self,cid,tid,uid,receive):
            try:
                assert uid=='u'
                await receive('part one')
                await receive('part one and two')
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
    sup.text_feed=Feed()
    transport=StreamTransport()
    try:
        await sup._sync_live('ch',epoch,version,transport)
        await asyncio.wait_for(started.wait(),2)
        async with asyncio.timeout(3):
            while await admin.fetchval('SELECT text FROM channel_outbox')!='part one and two':
                await asyncio.sleep(0.01)
        await sup.send_one('ch',epoch,version,transport)
        assert transport.calls[0][0]=='part one and two'
        await admin.execute('DELETE FROM channel_identities')
        await sup._sync_live('ch',epoch,version,transport)
        assert not sup.live and closed.is_set()
    finally:
        for _,task in sup.live.values(): task.cancel()
        await asyncio.gather(*(task for _,task in sup.live.values()),return_exceptions=True)


async def test_plain_fallback_does_not_starve_stream_candidate(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    await admin.execute('''UPDATE channel_outbox SET reply_state='{"mode":"plain"}' ''')
    second=await service.ingest('ch',message('second'))
    await service.process_one(second)
    async with sessions() as db:
        rows=await repo.live_replies(db,'ch',1)
        assert [row.id for row in rows]==[second]


async def test_completed_task_does_not_emit_pending_hint_before_projection(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    await admin.execute("UPDATE tasks SET status='completed' WHERE channel_inbox_id=$1",id)
    await admin.execute("UPDATE channel_outbox SET next_attempt_at=now()-interval '1 second'")
    transport=StreamTransport()
    await sup.send_one('ch',epoch,version,transport)
    assert not transport.calls


async def test_many_successful_stream_updates_do_not_exhaust_manual_retry(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    await admin.execute("UPDATE channel_outbox SET status='unknown',attempts=100")
    async with sessions() as db:
        row=await repo.reply_slot(db,id)
        rid=row.id
    for attempt in range(4):
        async with sessions() as db:
            await service.retry(db,'u',rid)
        await admin.execute("UPDATE channel_outbox SET status='unknown'")
    from fastapi import HTTPException
    async with sessions() as db:
        with pytest.raises(HTTPException):
            await service.retry(db,'u',rid)


async def test_wecom_empty_bootstrap_preserves_delayed_hint(channels):
    service,sessions,admin=channels
    await admin.execute("UPDATE channel_connections SET provider='wecom'")
    id=await service.ingest('ch',message(reply_context={'req_id':'callback'}))
    await service.process_one(id)
    sup=ChannelSupervisor(service)
    async with sessions.begin() as db:
        ch=await repo.claim_connection(db,'ch',sup.owner,60)
        epoch,version=ch.lease_epoch,ch.version
    transport=StreamTransport()
    await sup.send_one('ch',epoch,version,transport)
    assert transport.calls[0][0]==''
    assert not await sup.send_one('ch',epoch,version,transport)
    await admin.execute("UPDATE channel_outbox SET next_attempt_at=now()-interval '1 second'")
    await sup.send_one('ch',epoch,version,transport)
    assert transport.calls[-1][0]=='正在处理…'
    assert transport.calls[-1][1]['receipt']=='same-message'


async def test_wecom_explicit_empty_rejection_falls_back_without_early_hint(channels):
    from app.channels.adapters.base import ChannelTransportError
    service,sessions,admin=channels
    await admin.execute("UPDATE channel_connections SET provider='wecom'")
    id=await service.ingest('ch',message(reply_context={'req_id':'callback'}))
    await service.process_one(id)
    sup=ChannelSupervisor(service)
    async with sessions.begin() as db:
        ch=await repo.claim_connection(db,'ch',sup.owner,60)
        epoch,version=ch.lease_epoch,ch.version
    class Reject(StreamTransport):
        async def stream(self,*args,**kwargs):
            raise ChannelTransportError('platform_rejected_request')
    transport=Reject()
    await sup.send_one('ch',epoch,version,transport)
    assert not transport.calls
    assert await admin.fetchval('SELECT status FROM channel_outbox')=='pending'
    assert not await sup.send_one('ch',epoch,version,transport)
    await admin.execute("UPDATE channel_outbox SET next_attempt_at=now()-interval '1 second'")
    await sup.send_one('ch',epoch,version,transport)
    assert transport.calls==[('正在处理…',{'plain':True})]


async def test_old_connection_cannot_stage_a_live_update(channels):
    service,sessions,admin,id,sup,epoch,version=await setup(channels)
    await service.update_reply(id,'current',owner=sup.owner,epoch=epoch,version=version)
    await admin.execute("UPDATE channel_connections SET lease_epoch=lease_epoch+1")
    await service.update_reply(id,'stale',owner=sup.owner,epoch=epoch,version=version)
    assert await admin.fetchval('SELECT text FROM channel_outbox')=='current'
