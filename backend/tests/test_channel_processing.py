"""Durable channel admission on PostgreSQL; transports never drive Runtime."""
import asyncio
import pytest
import pytest_asyncio
from sqlalchemy import select
from app.channels.processing import ChannelService
from app.channels.contracts import InboundMessage
from app.services.execution_admission import TaskAdmission
from workmate_product.config.channels import ChannelPolicy
from workmate_product.persistence.models import (ChannelConnection, ChannelIdentity, ChannelInbox,
    ChannelOutbox, ChannelConversation, Conversation, Task, TaskMessage, User, Agent, ResourceShare, ScopeMembership)
from test_native_admission import admission_database
from test_durable_projection import projection_database


@pytest_asyncio.fixture
async def channels(admission_database):
    sessions, admin = admission_database
    async with sessions.begin() as db:
        (await db.get(Agent,'a')).visibility = 'public'
        db.add(ResourceShare(id='share',agent_id='a',target_scope_id='scope'))
        db.add(ScopeMembership(user_id='u',scope_id='scope'))
        db.add(ChannelConnection(id='ch', name='IM', provider='feishu', agent_id='a', scope_id='scope',
            credentials={'app_id':'app','app_secret':'secret'}, enabled=True,
            group_enabled=True, group_allowlist=['group']))
        await db.flush()
        db.add(ChannelIdentity(id='identity',channel_id='ch',external_user_id='external',user_id='u'))
    service = ChannelService(sessions, TaskAdmission(sessions, storage_node_id='node'),
        ChannelPolicy(), 'https://work.example')
    return service, sessions, admin


def message(id='m', **kwargs):
    return InboundMessage(id, kwargs.pop('sender_id','external'), kwargs.pop('chat_id','dm'),
        kwargs.pop('chat_type','private'), kwargs.pop('text','hello'), **kwargs)


async def test_duplicate_and_competing_processors_create_one_task(channels):
    service, sessions, admin = channels
    ids = await asyncio.gather(*(service.ingest('ch',message()) for _ in range(3)))
    assert len(set(ids)) == 1
    assert await admin.fetchval('SELECT count(*) FROM channel_inbox') == 1
    await asyncio.gather(*(service.process_one(ids[0]) for _ in range(3)))
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'") == 1
    assert await admin.fetchval("SELECT count(*) FROM channel_outbox") == 1
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE kind='StartTask'") >= 1
    async with sessions() as db:
        inbox = await db.get(ChannelInbox, ids[0])
        conv = await db.get(Conversation,inbox.conversation_id)
        assert conv.user_id == 'u' and conv.agent_id == 'a'
        assert inbox.status == 'processed'
        from app.api.schemas import TaskOut
        assert TaskOut.model_validate(await db.get(Task,inbox.task_id)).origin == 'channel'


async def test_private_and_group_contexts_separate_and_group_never_discloses_text(channels):
    service, sessions, admin = channels
    dm = await service.ingest('ch',message())
    group = await service.ingest('ch',message('g',chat_id='group',chat_type='group',mentioned=True))
    for id in (dm,group):
        await service.process_one(id)
    async with sessions.begin() as db:
        rows = [await db.get(ChannelInbox,id) for id in (dm,group)]
        assert rows[0].conversation_id != rows[1].conversation_id
        for row in rows:
            task = await db.get(Task,row.task_id)
            task.status = 'completed'
            conv = await db.get(Conversation,row.conversation_id)
            conv.active_product_task_id = None
            db.add(TaskMessage(id=row.id,task_id=task.id,conversation_id=conv.id,
                role='assistant',content={'text':'PRIVATE ANSWER'}))
    await service.project_results()
    async with sessions() as db:
        replies = list((await db.scalars(select(ChannelOutbox).where(ChannelOutbox.kind=='reply'))).all())
        assert len(replies) == 2
        for row in replies:
            assert ('PRIVATE ANSWER' in row.text) == (row.target['chat_type']=='private')
    await service.project_results()
    assert await admin.fetchval("SELECT count(*) FROM channel_outbox WHERE kind='reply' AND reply_finished=true") == 2


async def test_group_filter_and_unknown_identity_do_not_create_task(channels):
    service,sessions,admin = channels
    for msg in (message('unmentioned',chat_id='group',chat_type='group'),
                message('othergroup',chat_id='other',chat_type='group',mentioned=True),
                message('unknown',sender_id='unknown')):
        id = await service.ingest('ch',msg)
        await service.process_one(id)
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'") == 0


async def test_new_preserves_old_delivery_and_disable_drops_pending_only(channels):
    service,sessions,admin = channels
    first = await service.ingest('ch',message())
    await service.process_one(first)
    new = await service.ingest('ch',message('new',text='/new'))
    await service.process_one(new)
    second = await service.ingest('ch',message('second'))
    await service.process_one(second)
    async with sessions() as db:
        a,b = await db.get(ChannelInbox,first),await db.get(ChannelInbox,second)
        assert a.conversation_id != b.conversation_id
    pending = await service.ingest('ch',message('pending'))
    await admin.execute("UPDATE channel_connections SET enabled=false WHERE id='ch'")
    await service.process_one(pending)
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'") == 2
    assert await admin.fetchval("SELECT status FROM channel_inbox WHERE id=$1",pending) == 'rejected'


async def test_final_projection_waits_for_product_delivery_pointer(channels):
    service,sessions,admin = channels
    id = await service.ingest('ch',message())
    await service.process_one(id)
    await admin.execute("UPDATE tasks SET status='completed' WHERE channel_inbox_id=$1",id)
    await service.project_results()
    assert await admin.fetchval("SELECT count(*) FROM channel_outbox WHERE kind='reply' AND reply_finished=true") == 0


async def test_revoked_user_never_admitted(channels):
    service,sessions,admin = channels
    id = await service.ingest('ch',message())
    await admin.execute("UPDATE users SET status='disabled' WHERE id='u'")
    await service.process_one(id)
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'") == 0


async def test_rebinding_does_not_reassign_historical_inbox(channels):
    service,sessions,admin = channels
    id = await service.ingest('ch',message())
    async with sessions.begin() as db:
        db.add(User(id='other',email='other@example.test',password_hash='test',display_name='Other'))
        await db.flush()
        identity = await db.get(ChannelIdentity,'identity')
        identity.user_id = 'other'
    await service.process_one(id)
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'") == 0
    assert await admin.fetchval('SELECT user_id FROM channel_inbox WHERE id=$1',id) == 'u'


async def test_bind_code_is_hashed_consumed_once_and_never_model_input(channels):
    service,sessions,admin = channels
    async with sessions() as db:
        result = await service.binding_code(db,'u','ch')
    id = await service.ingest('ch',message('bind',sender_id='new-platform-user',text='  /bind '+result['code']+'  '))
    await service.process_one(id)
    stored = await admin.fetchval('SELECT text FROM channel_inbox WHERE id=$1',id)
    assert result['code'] not in stored and stored.startswith('/bind sha256:')
    assert await admin.fetchval("SELECT user_id FROM channel_identities WHERE external_user_id='new-platform-user'") == 'u'
    duplicate = await service.ingest('ch',message('dupcode',sender_id='stealing-user',text='/bind '+result['code']))
    await service.process_one(duplicate)
    assert await admin.fetchval("SELECT count(*) FROM channel_identities WHERE external_user_id='stealing-user'") == 0
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'") == 0


async def test_unbound_pending_message_does_not_gain_new_binding(channels):
    service,sessions,admin = channels
    id = await service.ingest('ch',message(sender_id='unknown'))
    async with sessions.begin() as db:
        db.add(ChannelIdentity(id='late-binding',channel_id='ch',external_user_id='unknown',user_id='u'))
    await service.process_one(id)
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'") == 0


async def test_stop_uses_active_task_and_preserves_owner_control_after_agent_revocation(channels):
    service,sessions,admin = channels
    for msg in (message(),message('queued')):
        id = await service.ingest('ch',msg)
        await service.process_one(id)
    await admin.execute("UPDATE agents SET created_by=NULL,visibility='private' WHERE id='a'")
    stop = await service.ingest('ch',message('stop',text='/stop'))
    await service.process_one(stop)
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE kind='StopTask'") == 1
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel' AND status='cancelled'") == 0


async def test_capacity_rejection_rolls_back_conversation_and_task(channels,monkeypatch):
    from workmate_product.application.task_capacity import TaskCapacityExceeded
    async def full(*args,**kwargs):
        raise TaskCapacityExceeded('full')
    monkeypatch.setattr('app.channels.processing.TaskCapacityPolicy.check',full)
    service,sessions,admin=channels
    id = await service.ingest('ch',message())
    await service.process_one(id)
    assert await admin.fetchval("SELECT status FROM channel_inbox WHERE id=$1",id) == 'rejected'
    assert await admin.fetchval("SELECT count(*) FROM channel_conversations") == 0
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'") == 0


async def test_private_result_excludes_process_prefix(channels):
    service,sessions,admin=channels
    id = await service.ingest('ch',message())
    await service.process_one(id)
    async with sessions.begin() as db:
        inbox = await db.get(ChannelInbox,id)
        (await db.get(Task,inbox.task_id)).status='completed'
        (await db.get(Conversation,inbox.conversation_id)).active_product_task_id=None
        db.add(TaskMessage(id='final',task_id=inbox.task_id,conversation_id=inbox.conversation_id,
            role='assistant',content={'text':'PROCESSFinal answer','presentation':{'version':1,'process_end':7}}))
    await service.project_results()
    text = await admin.fetchval("SELECT text FROM channel_outbox WHERE kind='reply' AND reply_finished=true")
    assert 'Final answer' in text and 'PROCESS' not in text


async def test_group_per_employee_and_private_share_agent_without_sharing_conversation(channels):
    service,sessions,admin=channels
    async with sessions.begin() as db:
        db.add(User(id='other',email='other@test.invalid',password_hash='test',display_name='Other'))
        await db.flush()
        db.add(ScopeMembership(scope_id='scope',user_id='other'))
        db.add(ChannelIdentity(id='other-id',channel_id='ch',external_user_id='other',user_id='other'))
    ids=[]
    for who in ('external','other'):
        id=await service.ingest('ch',message(who,sender_id=who,chat_id='group',chat_type='group',mentioned=True))
        await service.process_one(id);ids.append(id)
    async with sessions() as db:
        rows=[await db.get(ChannelInbox,id) for id in ids]
        convs=[await db.get(Conversation,r.conversation_id) for r in rows]
        assert convs[0].id!=convs[1].id and {c.user_id for c in convs}=={'u','other'}
        assert {c.agent_id for c in convs}=={'a'}


async def test_configuration_change_rejects_pending_without_rerouting(channels):
    service,sessions,admin=channels
    id=await service.ingest('ch',message())
    await admin.execute("UPDATE channel_connections SET version=version+1")
    await service.process_one(id)
    assert await admin.fetchval('SELECT status FROM channel_inbox WHERE id=$1',id)=='rejected'
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'")==0


async def test_unsupported_and_status_use_fixed_responses(channels):
    service,sessions,admin=channels
    for msg in (message('unsupported',unsupported=True),message('status',text='/status')):
        id=await service.ingest('ch',msg);await service.process_one(id)
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'")==0
    assert await admin.fetchval('SELECT count(*) FROM channel_outbox')==2


async def test_outbound_backlog_applies_ingress_backpressure(channels):
    service,sessions,admin=channels
    service.policy=ChannelPolicy(max_inbox_pending=1)
    id=await service.ingest('ch',message())
    await service.process_one(id)
    with pytest.raises(ValueError,match='inbox_full'):
        await service.ingest('ch',message('more'))
    # A platform duplicate can still be acknowledged while the backlog is full.
    assert await service.ingest('ch',message())==id
