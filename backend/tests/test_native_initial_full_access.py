"""Initial native admission stores Full Access intent without probing legacy App."""
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import get_current_user
from app.db import get_db
from workmate_product.persistence.models import User
from app.main import build_app
from app.services.execution_admission import TaskAdmission
from test_native_admission import admission_database, projection_database  # noqa: F401


@pytest_asyncio.fixture
async def initial_client(admission_database, monkeypatch, tmp_path):
    sessions, admin = admission_database
    application = build_app()
    application.state.product_runtime = TaskAdmission(sessions, storage_node_id='node')

    class ForbiddenRuntime:
        def __getattr__(self, name):
            raise AssertionError('Native initial admission must not call legacy App')

    application.state.runtime_client = ForbiddenRuntime()

    async def database():
        async with sessions() as db:
            yield db

    async def current_user():
        async with sessions() as db:
            return await db.get(User, 'u')

    application.dependency_overrides[get_db] = database
    application.dependency_overrides[get_current_user] = current_user
    monkeypatch.setattr('app.attachments.get_settings', lambda: SimpleNamespace(
        artifact_storage_dir=str(tmp_path)))
    async with AsyncClient(transport=ASGITransport(application), base_url='http://test') as client:
        yield client, admin, tmp_path


async def submit(client, kind, profile):
    base = '/api/tasks/conversations/new/'
    if kind == 'json':
        return await client.post(base+'tasks', json={
            'message': 'Please review', 'approval_profile': profile})
    return await client.post(base+'tasks-with-files', data={
        'message': 'Please review', 'approval_profile': profile},
        files={'files': ('report.txt', b'Attachment content', 'text/plain')})


@pytest.mark.parametrize('kind', ['json', 'files'])
@pytest.mark.parametrize('profile', ['standard', 'full_access'])
async def test_initial_native_task_admits_profile_without_app_probe_or_enabled_audit(
    initial_client, kind, profile,
):
    client, admin, root = initial_client
    response = await submit(client, kind, profile)
    assert response.status_code == 202, response.text
    result = response.json()
    assert result['queued'] is False
    task_id = result['task']['id']
    assert result['task']['approval_profile'] == profile
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands WHERE task_id=$1', task_id) == 1
    payload = json.loads(await admin.fetchval(
        'SELECT payload FROM runtime_execution_specs WHERE task_id=$1', task_id))
    assert payload['approval_profile'] == profile
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='task.full_access_enabled'") == 0
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE task_id=$1 AND event_type='permission_mode_changed'", task_id) == 0
    message = json.loads(await admin.fetchval(
        "SELECT content FROM task_messages WHERE task_id=$1 AND role='user'", task_id))
    assert message['text'] == 'Please review'
    if kind == 'files':
        assert message['attachments'][0]['path'] == f'input/{task_id}/report.txt'
        assert (root/'.inputs'/'new'/task_id/'report.txt').read_bytes() == b'Attachment content'


@pytest.mark.parametrize('kind', ['json', 'files'])
async def test_initial_full_access_retains_agent_use_authorization(initial_client, kind):
    client, admin, root = initial_client
    await admin.execute("UPDATE agents SET deleted_at=clock_timestamp() WHERE id='a'")
    response = await submit(client, kind, 'full_access')
    assert response.status_code == 403, response.text
    assert await admin.fetchval("SELECT count(*) FROM tasks WHERE conversation_id='new'") == 0
    assert not (root/'.inputs').exists()


async def test_s3_attachment_is_published_before_admission(initial_client,monkeypatch):
    import hashlib
    client,admin,root = initial_client
    objects = {}
    class Store:
        def publish(self,key,content):
            objects[key] = content
            return hashlib.sha256(content).hexdigest()
    monkeypatch.setattr('workmate_product.artifact_storage.artifact_store_from_settings',lambda _: Store())
    response = await submit(client,'files','standard')
    assert response.status_code == 202,response.text
    task_id = response.json()['task']['id']
    message = json.loads(await admin.fetchval(
        "SELECT content FROM task_messages WHERE task_id=$1 AND role='user'",task_id))
    attachment = message['attachments'][0]
    assert attachment['storage_key'] == f'.inputs/new/{task_id}/report.txt'
    assert objects[attachment['storage_key']] == b'Attachment content'
    assert attachment['sha256'] == hashlib.sha256(b'Attachment content').hexdigest()
    assert not (root/'.inputs'/'new'/task_id/'report.txt').exists()


async def test_s3_upload_failure_does_not_admit_task(initial_client,monkeypatch):
    client,admin,_ = initial_client
    before = await admin.fetchval('SELECT count(*) FROM runtime_commands')
    class Store:
        def publish(self,*args):
            raise OSError('object store unavailable')
        def delete(self,key): pass
    monkeypatch.setattr('workmate_product.artifact_storage.artifact_store_from_settings',lambda _: Store())
    response = await submit(client,'files','standard')
    assert response.status_code == 502
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == before


async def test_s3_admission_rejection_removes_uploaded_objects(initial_client,monkeypatch):
    from fastapi import HTTPException
    import hashlib
    client,admin,_ = initial_client
    objects = {}
    class Store:
        def publish(self,key,content):
            objects[key] = content
            return hashlib.sha256(content).hexdigest()
        def delete(self,key): objects.pop(key,None)
    async def rejected(*args,**kwargs):
        assert objects
        raise HTTPException(503,'node offline')
    monkeypatch.setattr('workmate_product.artifact_storage.artifact_store_from_settings',lambda _: Store())
    monkeypatch.setattr(TaskAdmission,'create_task',rejected)
    response = await submit(client,'files','standard')
    assert response.status_code == 503
    assert objects == {}
