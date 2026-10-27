"""Management authorization and QR lifecycle on isolated PostgreSQL."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app.channels.management import ChannelManagement
from app.channels.rules import ChannelCreate, ChannelUpdate
from test_durable_projection import projection_database
from test_native_admission import admission_database

@pytest.fixture
def service(admission_database):
    sessions,_=admission_database
    return ChannelManagement(sessions,SimpleNamespace(_authorize=AsyncMock(return_value=True)),
        SimpleNamespace(enabled=True,wechat_enabled=True,max_connections=20),'https://workmate.example')

async def seed(admin,provider='wechat',owner="'u'",id='ch'):
    await admin.execute(f"INSERT INTO channel_connections(id,name,provider,agent_id,scope_id,owner_user_id,credentials) VALUES ($1,'Bot',$2,'a',(SELECT scope_id FROM agents WHERE id='a'),{owner},'{{}}')",id,provider)

@pytest.fixture
def qr_adapter(monkeypatch):
    adapter=SimpleNamespace(get_qrcode=AsyncMock(return_value={'qrcode':'opaque','qrcode_img_content':'https://liteapp.weixin.qq.com/q/example'}),get_qrcode_status=AsyncMock(return_value={'status':'confirmed','bot_token':'SERVER_SECRET','ilink_bot_id':'bot','ilink_user_id':'external','baseurl':'https://ilinkai.weixin.qq.com'}),close=AsyncMock())
    monkeypatch.setattr('app.channels.management.WechatAdapter',lambda credentials:adapter)
    return adapter

async def test_qr_is_owner_bound_and_confirm_enables_with_server_secret(admission_database,service,qr_adapter):
    sessions,admin=admission_database;await seed(admin)
    async with sessions() as db:
        result=await service.qr(db,'u','ch')
        assert result=={'qrcode':'opaque','qrcode_url':'https://liteapp.weixin.qq.com/q/example'}
    async with sessions() as db:
        assert await service.qr_status(db,'u','ch','opaque')=={'status':'confirmed'}
    assert 'SERVER_SECRET' in await admin.fetchval("SELECT credentials::text FROM channel_connections WHERE id='ch'")
    assert await admin.fetchval("SELECT enabled FROM channel_connections WHERE id='ch'")
    assert await admin.fetchval("SELECT user_id FROM channel_identities WHERE external_user_id='external'")=='u'
    version = await admin.fetchval("SELECT version FROM channel_connections WHERE id='ch'")
    async with sessions() as db:
        disabled = await service.update(db,'u','ch',ChannelUpdate(version=version,name='Bot',agent_id='a',enabled=False))
        assert disabled['enabled'] is False
    async with sessions() as db:
        enabled = await service.update(db,'u','ch',ChannelUpdate(version=disabled['version'],name='Bot',agent_id='a',enabled=True))
        assert enabled['enabled'] is True
    async with sessions() as db:
        with pytest.raises(HTTPException) as error:await service.qr(db,'other','ch')
        assert error.value.status_code in (403,404)

async def test_qr_rejects_untrusted_links_and_hides_provider_errors(admission_database,service,qr_adapter):
    sessions,admin=admission_database;await seed(admin)
    qr_adapter.get_qrcode.return_value={'qrcode':'opaque','qrcode_img_content':'https://evil.test/secret'}
    async with sessions() as db:
        with pytest.raises(HTTPException) as error:await service.qr(db,'u','ch')
        assert error.value.status_code==502 and 'evil' not in error.value.detail
    qr_adapter.get_qrcode.side_effect=RuntimeError('sensitive secret')
    async with sessions() as db:
        with pytest.raises(HTTPException) as error:await service.qr(db,'u','ch')
        assert 'secret' not in error.value.detail

async def test_qr_expires_and_rejects_stale_request(admission_database,service,qr_adapter):
    sessions,admin=admission_database;await seed(admin)
    async with sessions() as db:await service.qr(db,'u','ch')
    async with sessions() as db:
        with pytest.raises(HTTPException):await service.qr_status(db,'u','ch','wrong')
    await admin.execute("UPDATE channel_connections SET credentials=json_build_object('_qr',json_build_object('qrcode','opaque','user_id','u','expires_at','2000-01-01T00:00:00+00:00')) WHERE id='ch'")
    async with sessions() as db:
        with pytest.raises(HTTPException):await service.qr_status(db,'u','ch','opaque')
    qr_adapter.get_qrcode_status.assert_not_awaited()

async def test_qr_network_holds_no_transaction_and_rejects_concurrent_edit(admission_database,service,qr_adapter):
    sessions,admin=admission_database;await seed(admin)
    async with sessions() as db:
        async def network():
            assert not db.in_transaction()
            await admin.execute("UPDATE channel_connections SET version=version+1 WHERE id='ch'")
            return {'qrcode':'opaque','qrcode_img_content':'https://liteapp.weixin.qq.com/q/example'}
        qr_adapter.get_qrcode.side_effect=network
        with pytest.raises(HTTPException) as error:await service.qr(db,'u','ch')
        assert error.value.status_code==409
    assert json.loads(await admin.fetchval("SELECT credentials::text FROM channel_connections WHERE id='ch'"))=={}

async def test_duplicate_wechat_login_cannot_claim_same_bot(admission_database,service,qr_adapter):
    sessions,admin=admission_database;await seed(admin);await seed(admin,id='second')
    for id in ('ch','second'):
        async with sessions() as db:await service.qr(db,'u',id)
    async with sessions() as db:await service.qr_status(db,'u','ch','opaque')
    async with sessions() as db:
        with pytest.raises(HTTPException) as error:await service.qr_status(db,'u','second','opaque')
        assert error.value.status_code==409
    assert await admin.fetchval('SELECT count(*) FROM channel_identities')==1

async def test_global_binding_rate_serializes_across_connections(admission_database,service):
    sessions,admin=admission_database
    for i in range(6):await seed(admin,provider='wecom',owner='NULL',id=f'ch{i}')
    await admin.execute('UPDATE channel_connections SET enabled=true')
    async def attempt(i):
        async with sessions() as db:
            try:return await service.binding_code(db,'u',f'ch{i}')
            except HTTPException as e:return e.status_code
    outcomes=await asyncio.gather(*(attempt(i) for i in range(6)))
    assert sum(isinstance(v,dict) for v in outcomes)==5
    assert outcomes.count(429)==1

async def test_enterprise_permissions_redaction_and_version(admission_database,service,monkeypatch):
    sessions,admin=admission_database;await seed(admin,provider='wecom',owner='NULL')
    await admin.execute("UPDATE channel_connections SET credentials='{"+'"bot_id":"bot","secret":"NEVER_RETURN"'+"}',group_allowlist='[\"private-group\"]'")
    monkeypatch.setattr(service,'permission',AsyncMock(return_value=False))
    async with sessions() as db:
        listing=await service.list(db,'u');assert 'NEVER_RETURN' not in str(listing)
        assert listing['items'][0]['group_allowlist']==[]
        with pytest.raises(HTTPException) as error:await service.delete(db,'u','ch',1)
        assert error.value.status_code==403
    monkeypatch.setattr(service,'permission',AsyncMock(return_value=True))
    async with sessions() as db:
        with pytest.raises(HTTPException) as error:await service.delete(db,'u','ch',999)
        assert error.value.status_code==409
    async with sessions() as db:await service.delete(db,'u','ch',1)
    assert not await admin.fetchval("SELECT enabled FROM channel_connections WHERE id='ch'")
    assert json.loads(await admin.fetchval("SELECT credentials::text FROM channel_connections WHERE id='ch'"))=={}

async def test_create_write_only_credentials_update_and_unique_bot(admission_database,service,monkeypatch):
    sessions,admin=admission_database
    monkeypatch.setattr(service,'permission',AsyncMock(return_value=True))
    await admin.execute("UPDATE agents SET visibility='public' WHERE id='a'")
    await admin.execute("INSERT INTO resource_shares(id,agent_id,target_scope_id) VALUES ('share','a','scope')")
    body=ChannelCreate(name='Office',provider='wecom',agent_id='a',credentials={'bot_id':'bot','secret':'TOPSECRET'})
    async with sessions() as db:
        row=await service.create(db,'u',body)
        assert row['has_credentials'] and not row['enabled'] and 'TOPSECRET' not in str(row)
    async with sessions() as db:
        changed=await service.update(db,'u',row['id'],ChannelUpdate(version=1,name='Office',agent_id='a',enabled=True))
        assert changed['enabled'] and changed['version']==2
    async with sessions() as db:
        with pytest.raises(HTTPException) as error:await service.create(db,'u',body)
        assert error.value.status_code==409
    assert 'TOPSECRET' in await admin.fetchval('SELECT credentials::text FROM channel_connections LIMIT 1')

async def test_router_requires_trusted_actor_and_validates_request(admission_database,service):
    from fastapi import FastAPI
    from httpx import ASGITransport,AsyncClient
    from app.api.routers.channels import router
    from app.auth import get_current_user
    from app.db import get_db
    sessions,admin=admission_database;await seed(admin,provider='wecom',owner='NULL')
    app=FastAPI();app.state.channel_service=service;app.include_router(router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id='u')
    async def database():
        async with sessions() as db:yield db
    app.dependency_overrides[get_db]=database
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get('/channels');assert response.status_code==200
        assert response.json()['items'][0]['id']=='ch'
        assert (await client.post('/channels/ch/qr-status',json={'qrcode':'x','user_id':'other'})).status_code==422
        assert (await client.get('/channels/ch/deliveries?limit=0')).status_code==422
        assert (await client.delete('/channels/ch?version=0')).status_code==422

async def test_disable_still_works_with_disabled_policy_and_broken_agent(admission_database,service,monkeypatch):
    sessions,admin=admission_database;await seed(admin,provider='wecom',owner='NULL')
    await admin.execute("UPDATE channel_connections SET enabled=true")
    service.policy.enabled=False
    monkeypatch.setattr(service,'permission',AsyncMock(return_value=True))
    monkeypatch.setattr(service,'validate_agent',AsyncMock(side_effect=AssertionError('must not validate to disable')))
    async with sessions() as db:
        row=await service.update(db,'u','ch',ChannelUpdate(version=1,name='Bot',agent_id='a',enabled=False))
    assert not row['enabled']

async def test_enterprise_agent_requires_explicit_organization_grant(admission_database,service,monkeypatch):
    sessions,admin=admission_database
    monkeypatch.setattr(service,'permission',AsyncMock(return_value=True))
    body=ChannelCreate(name='Office',provider='wecom',agent_id='a',credentials={'bot_id':'bot','secret':'secret'})
    async with sessions() as db:
        with pytest.raises(HTTPException) as error:await service.create(db,'u',body)
        assert error.value.status_code==422
    await admin.execute("UPDATE agents SET visibility='public' WHERE id='a'")
    async with sessions() as db:
        with pytest.raises(HTTPException):await service.create(db,'u',body)

async def test_unbind_is_owner_only_and_bindings_remain_private(admission_database,service):
    sessions,admin=admission_database;await seed(admin,provider='wecom',owner='NULL')
    await admin.execute("INSERT INTO channel_identities(id,channel_id,external_user_id,user_id) VALUES ('identity','ch','external','u')")
    async with sessions() as db:
        assert (await service.bindings(db,'other'))['items']==[]
        with pytest.raises(HTTPException) as error:await service.unbind(db,'other','identity')
        assert error.value.status_code==404
    async with sessions() as db:await service.unbind(db,'u','identity')
    assert await admin.fetchval('SELECT count(*) FROM channel_identities')==0

async def test_router_forwards_only_authenticated_actor():
    from fastapi import FastAPI
    from httpx import ASGITransport,AsyncClient
    from app.api.routers.channels import router
    from app.auth import get_current_user
    from app.db import get_db
    names=('list','create','bindings','unbind','retry','update','delete','binding_code','deliveries','qr','qr_status')
    service=SimpleNamespace(**{name:AsyncMock(return_value={}) for name in names})
    app=FastAPI();app.include_router(router);app.state.channel_service=service
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id='trusted')
    app.dependency_overrides[get_db]=lambda:'db'
    requests=[('GET','/channels',None,'list'),('POST','/channels',{'name':'n','provider':'wechat','agent_id':'a'},'create'),('GET','/channels/bindings',None,'bindings'),('DELETE','/channels/bindings/i',None,'unbind'),('POST','/channels/deliveries/d/retry',None,'retry'),('PATCH','/channels/ch',{'name':'n','agent_id':'a','enabled':False,'version':1},'update'),('DELETE','/channels/ch?version=1',None,'delete'),('POST','/channels/ch/binding-code',None,'binding_code'),('GET','/channels/ch/deliveries',None,'deliveries'),('POST','/channels/ch/qr',None,'qr'),('POST','/channels/ch/qr-status',{'qrcode':'opaque'},'qr_status')]
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        for method,path,body,name in requests:
            response=await client.request(method,path,json=body)
            assert response.status_code in (200,201,204),response.text
            assert getattr(service,name).await_args.args[:2]==('db','trusted')
        app.state.channel_service=None
        assert (await client.get('/channels')).status_code==503

async def test_delivery_history_retry_owner_and_attempt_cap(admission_database,service):
    sessions,admin=admission_database;await seed(admin,provider='wecom',owner='NULL')
    await admin.execute("INSERT INTO channel_inbox(id,channel_id,channel_version,message_id,sender_id,chat_id,chat_type,text,target) VALUES ('i','ch',1,'msg','ext','chat','private','hello','{}')")
    await admin.execute("INSERT INTO channel_outbox(id,channel_id,inbox_id,user_id,kind,dedupe_key,target,text,status) VALUES ('d','ch','i','u','binding','dedupe','{}','safe','unknown')")
    async with sessions() as db:
        values=await service.deliveries(db,'u','ch');assert values['items'][0]['conversation_id'] is None
        assert (await service.deliveries(db,'other','ch'))['items']==[]
        with pytest.raises(HTTPException):await service.retry(db,'other','d')
    async with sessions() as db:assert await service.retry(db,'u','d')=={'status':'pending'}
    async with sessions() as db:
        with pytest.raises(HTTPException):await service.retry(db,'u','d')
    await admin.execute("UPDATE channel_outbox SET status='failed',attempts=5 WHERE id='d'")
    async with sessions() as db:
        with pytest.raises(HTTPException):await service.retry(db,'u','d')

async def test_deleted_robot_key_can_be_reused(admission_database,service,monkeypatch):
    sessions,admin=admission_database
    monkeypatch.setattr(service,'permission',AsyncMock(return_value=True))
    await admin.execute("UPDATE agents SET visibility='public' WHERE id='a'")
    await admin.execute("INSERT INTO resource_shares(id,agent_id,target_scope_id) VALUES ('share','a','scope')")
    body=ChannelCreate(name='Office',provider='wecom',agent_id='a',credentials={'bot_id':'bot','secret':'secret'})
    async with sessions() as db:first=await service.create(db,'u',body)
    async with sessions() as db:await service.delete(db,'u',first['id'],1)
    async with sessions() as db:second=await service.create(db,'u',body)
    assert second['id']!=first['id']

async def test_router_rejects_oversized_chunked_credentials_before_parsing():
    from fastapi import FastAPI
    from httpx import ASGITransport,AsyncClient
    from app.api.routers.channels import router
    app=FastAPI();app.include_router(router)
    async def payload():
        yield b'{"credentials":{"secret":"'
        yield b'x'*32768
        yield b'"}}'
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/channels',content=payload(),headers={'content-type':'application/json'})
    assert response.status_code==413 and 'xxxx' not in response.text


async def test_expired_lease_is_not_reported_connected(admission_database,service):
    sessions,admin=admission_database
    await seed(admin)
    await admin.execute("UPDATE channel_connections SET enabled=true,status='connected',lease_until=now()-interval '1 second'")
    async with sessions() as db:
        row=await service.get(db,'u','ch')
        assert (await service.view(db,'u',row))['status']=='disconnected'

async def test_wechat_enable_before_login_has_scan_instruction(admission_database,service):
    sessions,admin=admission_database;await seed(admin)
    async with sessions() as db:
        with pytest.raises(HTTPException) as error:
            await service.update(db,'u','ch',ChannelUpdate(version=1,name='Bot',agent_id='a',enabled=True))
        assert error.value.status_code == 422
        assert '扫码登录' in error.value.detail

@pytest.mark.parametrize('blocked',['agent','public_url','policy'])
async def test_qr_confirm_rechecks_enable_conditions(admission_database,service,qr_adapter,blocked):
    from app.services.public_urls import WorkmateLinks
    sessions,admin=admission_database;await seed(admin)
    async with sessions() as db: await service.qr(db,'u','ch')
    if blocked == 'agent':
        service.validate_agent=AsyncMock(side_effect=HTTPException(403,'access_denied'))
    elif blocked == 'public_url':
        service.links=WorkmateLinks('')
    else:
        service.policy.enabled=False
    async with sessions() as db:
        with pytest.raises(HTTPException):
            await service.qr_status(db,'u','ch','opaque')
    assert not await admin.fetchval("SELECT enabled FROM channel_connections WHERE id='ch'")
    assert await admin.fetchval('SELECT count(*) FROM channel_identities') == 0
    assert 'SERVER_SECRET' not in await admin.fetchval("SELECT credentials::text FROM channel_connections WHERE id='ch'")

@pytest.mark.parametrize('changed_user,changed_bot,remove_identity,accepted', [
    (False, True, False, True),
    (True, True, False, False),
    (True, False, False, False),
    (False, True, True, False),
])
async def test_wechat_relogin_uses_bound_owner_identity(
    admission_database,service,qr_adapter,changed_user,changed_bot,remove_identity,accepted,
):
    sessions,admin=admission_database;await seed(admin)
    async with sessions() as db: await service.qr(db,'u','ch')
    async with sessions() as db: await service.qr_status(db,'u','ch','opaque')
    old_key=await admin.fetchval("SELECT bot_key FROM channel_connections WHERE id='ch'")
    if remove_identity:
        await admin.execute("DELETE FROM channel_identities WHERE channel_id='ch'")
    async with sessions() as db: await service.qr(db,'u','ch')
    qr_adapter.get_qrcode_status.return_value={
        'status':'confirmed','bot_token':'NEW_SERVER_SECRET',
        'ilink_bot_id':'rotated-bot' if changed_bot else 'bot',
        'ilink_user_id':'different-user' if changed_user else 'external',
        'baseurl':'https://ilinkai.weixin.qq.com',
    }
    async with sessions() as db:
        if accepted:
            assert await service.qr_status(db,'u','ch','opaque') == {'status':'confirmed'}
        else:
            with pytest.raises(HTTPException) as error:
                await service.qr_status(db,'u','ch','opaque')
            assert error.value.status_code == 409
    row=await admin.fetchrow("SELECT enabled,bot_key,credentials::text AS credentials FROM channel_connections WHERE id='ch'")
    assert row['enabled'] is accepted
    if accepted:
        assert row['bot_key'] != old_key
        assert 'NEW_SERVER_SECRET' in row['credentials']
        assert await admin.fetchval("SELECT count(*) FROM channel_identities WHERE channel_id='ch'") == 1
    else:
        assert row['bot_key'] == old_key
        assert 'NEW_SERVER_SECRET' not in row['credentials']
