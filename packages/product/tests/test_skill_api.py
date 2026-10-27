"""Existing Skill HTTP contracts on the new role router, using real PostgreSQL."""
import pytest
import pytest_asyncio
from httpx import ASGITransport,AsyncClient
from workmate_product.persistence.models import User
from test_execution_context import database
from test_skill_repository import skill_database
from test_skill_artifacts import archive
from app.auth import get_current_user, get_access_decision
from app.db import get_db
from app.services.access_decision import AccessDecisionService

@pytest_asyncio.fixture
async def api(skill_database):
    sessions,admin=skill_database
    from app.main import build_app
    app=build_app()
    async def db_dependency():
        async with sessions() as db:
            yield db
    async def actor():
        async with sessions() as db:
            return await db.get(User,'u')
    from fastapi import Depends
    async def access(db=Depends(get_db)):
        return AccessDecisionService(db)
    app.dependency_overrides[get_db]=db_dependency
    app.dependency_overrides[get_current_user]=actor
    app.dependency_overrides[get_access_decision]=access
    async with AsyncClient(transport=ASGITransport(app),base_url='http://test') as client:
        yield client,admin,app

@pytest.mark.asyncio
async def test_builtin_catalog_bootstrap_description_and_protected_delete(api, skill_database):
    from app.services.workspace_assistant import ensure_builtin_skill_library
    client, admin, _ = api
    sessions, _ = skill_database
    async with sessions.begin() as db:
        await ensure_builtin_skill_library(db)
    hubs = await client.get('/api/skills/hubs')
    assert {h['hub_id'] for h in hubs.json()} == {'workmate-builtin', 'uploaded'}
    page = (await client.get('/api/skills/catalog', params={'hub_id': 'workmate-builtin'})).json()
    card = page['cards'][0]
    response = await client.post('/api/skills', json={
        'scope_id': 'org', 'hub_id': 'workmate-builtin', 'card_id': card['id'], 'name': 'alias'})
    assert response.status_code == 422, response.text
    identifier = await admin.fetchval("SELECT id FROM capability_installations WHERE metadata->>'card_id'=$1", card['id'])
    description = await client.get(f'/api/skills/{identifier}/description')
    assert description.status_code == 200 and '---' in description.json()['markdown']
    assert (await client.delete(f'/api/skills/{identifier}')).status_code == 409
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts') == len(page['cards'])

@pytest.mark.asyncio
async def test_upload_ready_duplicate_conflict_and_invalid_source(api):
    client,admin,_=api
    raw=archive({'SKILL.md':'---\nname: uploaded-example\ndescription: Example\n---\nWork.'})
    request={'files':{'archive':('example.zip',raw,'application/zip')},'data':{'scope_id':'org','display_name':'Example'}}
    first=await client.post('/api/skills/upload',**request)
    assert first.status_code==201,first.text
    assert first.json()['metadata']['creation_channel']=='upload'
    assert (await client.post('/api/skills/upload',**request)).status_code==409
    bad=await client.post('/api/skills/upload',files={'archive':('bad.zip',b'not zip')},data={'scope_id':'org'})
    assert bad.status_code==422,bad.text
    catalog=await client.get('/api/skills/catalog',params={'hub_id':'uploaded'})
    assert len(catalog.json()['cards'])==1
    assert (await client.post('/api/skills',json={'hub_id':'skillhub-cn','card_id':'sample','scope_id':'org'})).status_code==409

@pytest.mark.asyncio
async def test_authority_revoked_during_external_download_cannot_register(api):
    from workmate_product.skills.catalog import SkillCard
    client,admin,app=api
    class ExternalCatalog:
        async def get_skill(self,user_id,card_id):
            return SkillCard(hub_id='external',id=card_id,name='downloaded')
        async def archive(self,user_id,card_id,version):
            # Early scope lock is released; registration must re-read revocation.
            await admin.execute('DELETE FROM role_bindings')
            return archive()
    app.state.skill_catalogs={'external':ExternalCatalog()}
    response=await client.post('/api/skills',json={'scope_id':'org','hub_id':'external','card_id':'downloaded'})
    assert response.status_code in (403,404),response.text
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==0


@pytest.mark.asyncio
async def test_uploaded_catalog_reinstall_and_visible_listing_use_native_records(api):
    client, admin, _ = api
    raw = archive({'SKILL.md': '---\nname: reusable\ndescription: Reusable workflow\n---\nWork.'})
    first = await client.post('/api/skills/upload', files={'archive': ('reusable.zip', raw)},
                              data={'scope_id': 'org'})
    assert first.status_code == 201, first.text
    page = await client.get('/api/skills/catalog', params={'hub_id': 'uploaded'})
    card_id = page.json()['cards'][0]['id']
    duplicate = await client.post('/api/skills', json={
        'scope_id': 'org', 'hub_id': 'uploaded', 'card_id': card_id, 'name': 'reusable-alias'})
    assert duplicate.status_code == 409, duplicate.text
    assert (await client.delete('/api/skills/' + first.json()['id'])).status_code == 204
    second = await client.post('/api/skills', json={
        'scope_id': 'org', 'hub_id': 'uploaded', 'card_id': card_id, 'name': 'reusable-alias'})
    assert second.status_code == 201, second.text
    visible = await client.get('/api/skills')
    assert visible.status_code == 200
    assert second.json()['id'] in {item['id'] for item in visible.json()['items']}
    assert first.json()['id'] not in {item['id'] for item in visible.json()['items']}
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts') == 1
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts') == 1
    assert (await client.get('/api/skills/missing/description')).status_code == 404
    assert (await client.get('/api/skills/catalog', params={'hub_id': 'missing'})).status_code == 404
    assert (await client.get('/api/skills/catalog', params={
        'hub_id': 'workmate-builtin', 'cursor': 'invalid'})).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['upload','external'])
@pytest.mark.parametrize('revoke',[False,True])
async def test_public_publication_prepares_blob_without_transaction_and_rechecks_authority(api,skill_database,source,revoke):
    from test_skill_blob_storage import MemoryBlobStore
    from workmate_product.skills.catalog import SkillCard
    sessions,_=skill_database
    client,admin,app=api
    current=[]
    writes=[]
    class ObservedStore(MemoryBlobStore):
        async def put_verified(self,digest,raw):
            assert not current[0].in_transaction()
            writes.append(digest)
            async with admin.transaction():
                await admin.fetchval("SELECT id FROM scopes WHERE id='org' FOR UPDATE NOWAIT")
                if revoke:
                    await admin.execute('DELETE FROM role_bindings')
            return await super().put_verified(digest,raw)
        async def get_verified(self,digest,size_bytes):
            raise AssertionError('Prepared publication must reuse already verified bytes in business transaction')
    store=ObservedStore()
    async def observed_db():
        async with sessions(info={'skill_blob_store':store}) as db:
            current[:]=[db]
            yield db
    app.dependency_overrides[get_db]=observed_db
    raw=archive({'SKILL.md':'---\nname: prepared-example\ndescription: Example\n---\nBody'})
    class Catalog:
        async def get_skill(self,user_id,card_id):
            return SkillCard(hub_id='external',id=card_id,name='prepared-example')
        async def archive(self,user_id,card_id,version): return raw
    app.state.skill_catalogs={'external':Catalog()}
    if source=='upload':
        response=await client.post('/api/skills/upload',files={'archive':('prepared.zip',raw)},data={'scope_id':'org'})
    else:
        response=await client.post('/api/skills',json={'scope_id':'org','hub_id':'external','card_id':'prepared-example'})
    assert len(writes)==1
    assert response.status_code in ((403,404) if revoke else (201,)),response.text
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==(0 if revoke else 1)
    assert len(store.objects)==1  # Revocation may leave only an unreferenced immutable object.
