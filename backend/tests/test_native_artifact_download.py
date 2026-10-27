"""Native immutable Artifact downloads preserve HTTP policy without old projector."""
import hashlib
from types import SimpleNamespace
from datetime import datetime,timezone
import pytest
import pytest_asyncio
from httpx import ASGITransport,AsyncClient
from app.auth import get_current_user
from app.db import get_db
from workmate_product.persistence.models import Artifact, Conversation
from app.api.routers import artifacts


DATA=b'Immutable artifact content for range verification.\n'


@pytest_asyncio.fixture
async def download(tmp_path,monkeypatch):
    from app.main import build_app
    digest=hashlib.sha256(DATA).hexdigest()
    key=f'.objects/sha256/{digest[:2]}/{digest}'
    path=tmp_path/key;path.parent.mkdir(parents=True);path.write_bytes(DATA)
    artifact=Artifact(id='artifact',task_id='task',conversation_id='conversation',name='report.txt',
        mime_type='text/plain',storage_key=key,size=len(DATA),change_type='created',metadata_={'sha256':digest})
    conversation=Conversation(id='conversation',user_id='owner',agent_id='agent')
    events=[]
    class Database:
        async def get(self,model,identifier,**kwargs):
            if model is Artifact: return artifact if identifier=='artifact' else None
            if model is Conversation: return conversation if identifier=='conversation' else None
            raise AssertionError('Unexpected database lookup')
        async def rollback(self):
            events.append('rollback')
    async def database(): yield Database()
    async def current_user(): return SimpleNamespace(id='owner')
    app=build_app()
    app.dependency_overrides[get_db]=database
    app.dependency_overrides[get_current_user]=current_user
    snapshot=artifacts._snapshot
    async def observed_snapshot(*args,**kwargs):
        events.append('snapshot')
        assert events==['rollback','snapshot']
        return await snapshot(*args,**kwargs)
    monkeypatch.setattr(artifacts,'_snapshot',observed_snapshot)
    app.state.download_events=events
    class ForbiddenProjector:
        def __getattr__(self,name): raise AssertionError('Native download used old Artifact projector')
    app.state.artifact_projector=ForbiddenProjector()
    settings=SimpleNamespace(artifact_storage_dir=str(tmp_path),artifact_preview_extensions='pdf,txt,png',
        artifact_force_download_extensions='html,svg,js,xml',artifact_max_file_bytes=1024*1024)
    monkeypatch.setattr(artifacts,'get_settings',lambda:settings)
    async with AsyncClient(transport=ASGITransport(app),base_url='http://test') as client:
        yield client,artifact,conversation,path


async def test_native_owner_download_uses_immutable_object_and_safe_headers(download):
    client,artifact,conversation,path=download
    response=await client.get('/api/artifacts/artifact/download')
    assert response.status_code==200,response.text
    assert response.content==DATA
    assert response.headers['content-disposition'].startswith('attachment;')
    assert 'report.txt' in response.headers['content-disposition']
    assert response.headers['x-content-type-options']=='nosniff'
    assert response.headers['cache-control']=='private, no-store'
    assert client._transport.app.state.download_events==['rollback','snapshot']


@pytest.mark.parametrize('name,mime,expected',[('report.txt','text/plain','inline'),
    ('report.html','text/html','attachment'),('report.txt','text/html','attachment'),
    ('image.svg','image/svg+xml','attachment')])
async def test_native_inline_preview_preserves_active_content_restrictions(download,name,mime,expected):
    client,artifact,conversation,path=download
    artifact.name=name;artifact.mime_type=mime
    response=await client.get('/api/artifacts/artifact/download?inline=true')
    assert response.status_code==200,response.text
    assert response.headers['content-disposition'].startswith(expected+';')
    assert response.content==DATA


async def test_native_range_response_is_correct_for_verified_object(download):
    client,artifact,conversation,path=download
    response=await client.get('/api/artifacts/artifact/download',headers={'Range':'bytes=2-10'})
    assert response.status_code==206,response.text
    assert response.content==DATA[2:11]
    assert response.headers['content-range']==f'bytes 2-10/{len(DATA)}'
    assert response.headers['content-length']=='9'


async def test_native_unsatisfiable_range_returns_416(download):
    client,artifact,conversation,path=download
    response=await client.get('/api/artifacts/artifact/download',headers={'Range':'bytes=1000-'})
    assert response.status_code==416,response.text
    assert response.headers['content-range']==f'bytes */{len(DATA)}'
    assert response.content!=DATA


@pytest.mark.parametrize('condition',['foreign','deleted','missing_artifact','missing_conversation'])
async def test_native_download_hides_unowned_deleted_and_missing_records(download,condition):
    client,artifact,conversation,path=download
    identifier='artifact'
    if condition=='foreign': conversation.user_id='another-user'
    elif condition=='deleted': conversation.deleted_at=datetime.now(timezone.utc)
    elif condition=='missing_artifact': identifier='missing'
    else: artifact.conversation_id='missing'
    path.unlink()  # Authorization must hide records before checking object existence.
    response=await client.get('/api/artifacts/'+identifier+'/download')
    assert response.status_code==404,response.text
    assert client._transport.app.state.download_events==[]


async def test_native_missing_object_returns_gone(download):
    client,artifact,conversation,path=download
    path.unlink()
    response=await client.get('/api/artifacts/artifact/download')
    assert response.status_code==410,response.text


@pytest.mark.parametrize('key',['../outside','/etc/passwd','output/report.txt',
    '.objects/sha256/00/'+'a'*64,'.objects/sha256/aa/'+'A'*64])
async def test_native_download_rejects_noncanonical_storage_keys(download,key):
    client,artifact,conversation,path=download
    artifact.storage_key=key
    response=await client.get('/api/artifacts/artifact/download')
    assert response.status_code==400,response.text
    assert response.content!=DATA


@pytest.mark.parametrize('corruption',['bytes','size'])
async def test_native_corruption_returns_gone_instead_of_unverified_bytes(download,corruption):
    client,artifact,conversation,path=download
    if corruption=='bytes': path.write_bytes(b'X'*len(DATA))
    else: artifact.size+=1
    response=await client.get('/api/artifacts/artifact/download')
    assert response.status_code==410,response.text
    assert response.content!=DATA


async def test_native_inconsistent_digest_descriptor_returns_400(download):
    client,artifact,conversation,path=download
    artifact.metadata_={'sha256':'0'*64}
    response=await client.get('/api/artifacts/artifact/download')
    assert response.status_code==400,response.text
    assert response.content!=DATA


async def test_s3_download_is_authorized_and_does_not_need_local_volume(download,monkeypatch,tmp_path):
    from workmate_product.application.artifact_objects import ArtifactSnapshot
    client,artifact,conversation,path = download
    path.unlink()
    calls = []
    class Store:
        def snapshot(self,key,digest,size):
            calls.append(key)
            target = tmp_path/'private-s3-download'
            target.write_bytes(DATA)
            return ArtifactSnapshot(target)
    monkeypatch.setattr(artifacts,'artifact_store_from_settings',lambda _: Store())
    response = await client.get('/api/artifacts/artifact/download')
    assert response.status_code == 200 and response.content == DATA
    assert calls == [artifact.storage_key]
    assert not (tmp_path/'private-s3-download').exists()
    conversation.user_id = 'other'
    response = await client.get('/api/artifacts/artifact/download')
    assert response.status_code == 404 and len(calls) == 1
