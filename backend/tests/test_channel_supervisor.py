import asyncio
import pytest
from sqlalchemy import select
from app.channels.supervisor import ChannelSupervisor
from workmate_product.persistence.repositories import channels as repo
from workmate_product.persistence.models import ChannelOutbox, ChannelConnection
from test_channel_processing import channels, message
from test_native_admission import admission_database
from test_durable_projection import projection_database


class Transport:
    def __init__(self, error=None):
        self.sent = []
        self.error = error
        self.ready = asyncio.Event()
        self.ready.set()
    async def send(self,target,text):
        self.sent.append((target,text))
        if self.error:
            raise self.error
        return 'receipt'


async def prepare(channels):
    service,sessions,admin = channels
    id = await service.ingest('ch',message())
    await service.process_one(id)
    await admin.execute("UPDATE channel_outbox SET next_attempt_at=now()-interval '1 second'")
    supervisor = ChannelSupervisor(service)
    async with sessions.begin() as db:
        row = await repo.claim_connection(db,'ch',supervisor.owner,60)
        epoch,version = row.lease_epoch,row.version
    return supervisor,epoch,version


async def test_delivery_has_receipt_and_never_reruns_task(channels):
    supervisor,epoch,version = await prepare(channels)
    transport = Transport()
    assert await supervisor.send_one('ch',epoch,version,transport)
    assert not await supervisor.send_one('ch',epoch,version,transport)
    service,sessions,admin=channels
    assert len(transport.sent)==1
    assert await admin.fetchval("SELECT status FROM channel_outbox") == 'sent'
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'") == 1


async def test_timeout_is_unknown_without_automatic_retry(channels):
    supervisor,epoch,version = await prepare(channels)
    transport = Transport(TimeoutError('secret-token-in-url'))
    await supervisor.send_one('ch',epoch,version,transport)
    await supervisor.send_one('ch',epoch,version,transport)
    service,sessions,admin=channels
    assert len(transport.sent)==1
    assert await admin.fetchval("SELECT status FROM channel_outbox") == 'unknown'
    assert 'secret' not in await admin.fetchval("SELECT last_error FROM channel_outbox")


async def test_revocation_cancels_pending_delivery(channels):
    supervisor,epoch,version = await prepare(channels)
    service,sessions,admin=channels
    await admin.execute('DELETE FROM channel_identities')
    transport = Transport()
    await supervisor.send_one('ch',epoch,version,transport)
    assert not transport.sent
    assert await admin.fetchval("SELECT status FROM channel_outbox") == 'cancelled'


async def test_stale_lease_neither_ingests_nor_sends(channels):
    supervisor,epoch,version = await prepare(channels)
    service,sessions,admin=channels
    await admin.execute("UPDATE channel_connections SET lease_until=now()-interval '1 second' WHERE id='ch'")
    async with sessions.begin() as db:
        takeover = await repo.claim_connection(db,'ch','other',60)
        assert takeover.lease_epoch > epoch
    transport = Transport()
    assert not await supervisor.send_one('ch',epoch,version,transport)
    with pytest.raises(ValueError,match='inactive'):
        await service.ingest('ch',message('late'),owner=supervisor.owner,epoch=epoch,version=version)
    assert not transport.sent


async def test_expired_sending_becomes_unknown_without_other_pending(channels):
    supervisor,epoch,version=await prepare(channels)
    service,sessions,admin=channels
    await admin.execute("UPDATE channel_outbox SET status='sending',lease_until=now()-interval '1 second'")
    assert not await supervisor.send_one('ch',epoch,version,Transport())
    assert await admin.fetchval("SELECT status FROM channel_outbox") == 'unknown'


async def test_channel_loop_health(monkeypatch):
    from types import SimpleNamespace
    from httpx import ASGITransport,AsyncClient
    from app.main import build_app
    monkeypatch.setattr('app.main.get_settings',lambda:SimpleNamespace(channel_enabled=True,schedule_enabled=False))
    app=build_app()
    app.state.product_ready=True
    for name in ('projection_task','command_publisher_task','business_publisher_task'):
        setattr(app.state,name,SimpleNamespace(done=lambda:False))
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as http:
        assert (await http.get('/health')).status_code==503
        app.state.channel_task=SimpleNamespace(done=lambda:False)
        assert (await http.get('/health')).status_code==200


class ListeningTransport(Transport):
    def __init__(self, *, fail=None, emit_message=None):
        super().__init__()
        self.started = asyncio.Event()
        self.delivered = asyncio.Event()
        self.closed = False
        self.fail = fail
        self.emit_message = emit_message
    async def listen(self,emit,stop):
        self.started.set()
        if self.fail:
            raise self.fail
        if self.emit_message:
            await emit(self.emit_message)
        await stop.wait()
    async def send(self,target,text):
        value=await super().send(target,text)
        self.delivered.set()
        return value
    async def close(self):
        self.closed=True


async def test_gateway_loop_ingests_dispatches_delivers_and_joins_connections(channels):
    service,sessions,admin=channels
    transport=ListeningTransport(emit_message=message())
    supervisor=ChannelSupervisor(service,adapter_factory=lambda *a,**kw:transport)
    shutdown=asyncio.Event()
    serving=asyncio.create_task(supervisor.serve(shutdown))
    try:
        async with asyncio.timeout(8):
            while not await admin.fetchval("SELECT count(*) FROM channel_outbox"):
                await asyncio.sleep(0.05)
        assert not transport.sent
        await admin.execute("UPDATE channel_outbox SET next_attempt_at=now()-interval '1 second'")
        await asyncio.wait_for(transport.delivered.wait(),8)
        assert await admin.fetchval("SELECT count(*) FROM tasks WHERE origin='channel'")==1
        assert await admin.fetchval("SELECT status FROM channel_connections WHERE id='ch'")=='connected'
    finally:
        shutdown.set()
        await asyncio.wait_for(serving,8)
    assert transport.closed and not supervisor.runners
    assert await admin.fetchval("SELECT lease_owner FROM channel_connections WHERE id='ch'") is None


async def test_two_supervisors_and_configuration_restart_use_one_listener(channels):
    service,sessions,admin=channels
    transports=[]
    def factory(*args,**kwargs):
        t=ListeningTransport();transports.append(t);return t
    one,two=ChannelSupervisor(service,adapter_factory=factory),ChannelSupervisor(service,adapter_factory=factory)
    await one.connections()
    await asyncio.wait_for(transports[0].started.wait(),3) if transports else await asyncio.sleep(0)
    await two.connections()
    assert len(one.runners)==1 and not two.runners
    runner=one.runners['ch']
    await admin.execute("UPDATE channel_connections SET version=version+1 WHERE id='ch'")
    await one.connections()
    await asyncio.gather(runner.task,return_exceptions=True)
    await one.connections()
    assert not one.runners and transports[0].closed


async def test_expired_wechat_session_disables_without_logging_secret(channels):
    from app.channels.adapters.base import ChannelTransportError
    service,sessions,admin=channels
    transport=ListeningTransport(fail=ChannelTransportError('wechat_session_expired'))
    supervisor=ChannelSupervisor(service,adapter_factory=lambda *a,**kw:transport)
    await supervisor.connections()
    await asyncio.wait_for(supervisor.runners['ch'].task,5)
    assert not await admin.fetchval("SELECT enabled FROM channel_connections WHERE id='ch'")
    assert '重新扫码' in await admin.fetchval("SELECT last_error FROM channel_connections WHERE id='ch'")
    await supervisor.connections()
    assert not supervisor.runners and transport.closed


async def test_cursor_is_fenced_and_committed_only_for_current_owner(channels):
    supervisor,epoch,version=await prepare(channels)
    service,sessions,admin=channels
    await supervisor.cursor('ch',epoch,version,'next')
    assert await admin.fetchval("SELECT cursor FROM channel_connections WHERE id='ch'")=='next'
    with pytest.raises(ValueError):
        await supervisor.cursor('ch',epoch+1,version,'stale')
    with pytest.raises(ValueError):
        await supervisor.cursor('ch',epoch,version,'x'*65537)


async def test_disabled_loop_does_not_open_connections(channels):
    from workmate_product.config.channels import ChannelPolicy
    service,_,_=channels
    service.policy=ChannelPolicy(enabled=False)
    supervisor=ChannelSupervisor(service,adapter_factory=lambda *a,**kw:pytest.fail('opened disabled channel'))
    shutdown=asyncio.Event();shutdown.set()
    await supervisor.serve(shutdown)


async def test_expired_same_owner_increments_epoch(channels):
    supervisor,epoch,version=await prepare(channels)
    service,sessions,admin=channels
    await admin.execute("UPDATE channel_connections SET lease_until=now()-interval '1 second'")
    async with sessions.begin() as db:
        claimed=await repo.claim_connection(db,'ch',supervisor.owner,60)
        assert claimed.lease_epoch==epoch+1
        assert await repo.lease_valid(db,'ch',supervisor.owner,epoch,version) is None


async def test_late_receipt_cannot_finish_manually_retried_attempt(channels):
    supervisor,epoch,version=await prepare(channels)
    service,sessions,admin=channels
    async with sessions.begin() as db:
        first=await repo.next_outbox(db,'ch','attempt-one',30)
        id=first.id
    await admin.execute("UPDATE channel_outbox SET status='pending',lease_until=now()-interval '1 second'")
    async with sessions.begin() as db:
        await repo.next_outbox(db,'ch','attempt-two',30)
    async with sessions.begin() as db:
        await repo.finish_delivery(db,id,'attempt-one','sent',receipt='late')
    assert await admin.fetchval("SELECT status FROM channel_outbox")=='sending'
    assert await admin.fetchval("SELECT lease_owner FROM channel_outbox")=='attempt-two'
