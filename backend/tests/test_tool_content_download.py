"""Tool content is privately retrieved through durable task ownership."""
import hashlib
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from app.api.routers import tasks
from app.auth import get_current_user
from app.db import get_db
from workmate_product.persistence.models import Task, Conversation
from workmate_product.application.artifact_objects import ArtifactSnapshot, ArtifactObjectUnavailable


@pytest.mark.parametrize('case', ['owner','foreign','missing','unavailable'])
async def test_private_exact_download(case, tmp_path, monkeypatch):
    from workmate_product import artifact_storage
    data = '真实完整参数☃'.encode()
    digest = hashlib.sha256(data).hexdigest()
    reads = []
    class DB:
        async def get(self, model, identity):
            if model is Task:
                return None if case == 'missing' else SimpleNamespace(conversation_id='conv')
            if model is Conversation:
                return SimpleNamespace(user_id='other' if case == 'foreign' else 'owner',deleted_at=None)
        async def scalar(self, query, params):
            assert params == {'task_id':'task','tool_call_id':'call','kind':'arguments'}
            return {'sha256':digest,'sizeBytes':len(data)}
        async def rollback(self):
            reads.append('rollback')
    class Store:
        def snapshot(self, key, sha, size):
            assert reads == ['rollback']
            reads.append('snapshot')
            assert key == f'.tool-content/sha256/{digest[:2]}/{digest}'
            assert (sha,size) == (digest,len(data))
            if case == 'unavailable':
                raise ArtifactObjectUnavailable()
            path=tmp_path/'snapshot'; path.write_bytes(data)
            return ArtifactSnapshot(path)
    monkeypatch.setattr(artifact_storage,'tool_content_store_from_settings',lambda settings: Store())
    async def db(): yield DB()
    async def user(): return SimpleNamespace(id='owner')
    app=FastAPI(); app.include_router(tasks.router,prefix='/api')
    app.dependency_overrides[get_db]=db; app.dependency_overrides[get_current_user]=user
    async with AsyncClient(transport=ASGITransport(app),base_url='http://test') as client:
        response=await client.get('/api/tasks/task/tools/call/content?kind=arguments')
    assert response.status_code == {'owner':200,'foreign':404,'missing':404,'unavailable':410}[case]
    if case == 'owner':
        assert response.content == data
        assert response.headers['cache-control'] == 'private, no-store'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert not (tmp_path/'snapshot').exists()
    elif case in {'foreign','missing'}:
        assert not reads


def test_tool_content_config_yaml_and_environment(tmp_path,monkeypatch):
    from app.config import Settings
    from workmate_runtime.config import load_runtime_settings
    path=tmp_path/'workmate.yaml'
    path.write_text('product:\n  artifacts:\n    storage:\n      tool_content_enabled: true\n      tool_content_inline_bytes: 4096\n      tool_content_preview_chars: 256\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    for read in (lambda: Settings(_env_file=None).artifact_storage,
                 lambda: load_runtime_settings().artifact_storage):
        value=read()
        assert value.tool_content_enabled and value.tool_content_inline_bytes == 4096
        assert value.tool_content_preview_chars == 256 and value.backend == 'local'
    monkeypatch.setenv('ARTIFACT_STORAGE_TOOL_CONTENT_INLINE_BYTES','8192')
    monkeypatch.setenv('ARTIFACT_STORAGE_TOOL_CONTENT_ENABLED','false')
    for read in (lambda: Settings(_env_file=None).artifact_storage,
                 lambda: load_runtime_settings().artifact_storage):
        assert read().tool_content_inline_bytes == 8192
        assert not read().tool_content_enabled
    monkeypatch.setenv('ARTIFACT_STORAGE_TOOL_CONTENT_INLINE_BYTES','0')
    with pytest.raises(ValueError): Settings(_env_file=None)
    with pytest.raises(ValueError): load_runtime_settings()


async def test_attachment_upload_keeps_artifact_storage_factory(tmp_path, monkeypatch):
    """Tool-payload storage must not replace the attachment publication adapter."""
    import io
    from fastapi import UploadFile
    from workmate_product import artifact_storage
    data=b'attachment content'
    conversation=SimpleNamespace(id='conv',user_id='owner',agent_id='agent',deleted_at=None)
    published={}
    class DB:
        async def get(self, model, identity):
            assert model is Conversation and identity=='conv'
            return conversation
    class Access:
        async def can_use_agent(self,*args): return True
        async def unavailable_capability_ids(self,*args): return []
    class Runtime:
        async def create_task(self, *args, **kwargs):
            published.update(kwargs)
            return SimpleNamespace(id=kwargs['task_id']),False
    async def security(*args): pass
    async def present(*args): return {'id':published['task_id']}
    factories=[]
    monkeypatch.setattr(artifact_storage,'artifact_store_from_settings',lambda settings: factories.append('artifact') or None)
    def forbidden(settings): raise AssertionError('Attachment used tool-content storage')
    monkeypatch.setattr(artifact_storage,'tool_content_store_from_settings',forbidden)
    monkeypatch.setattr(tasks,'attachment_task_dir',lambda cid,tid:str(tmp_path/tid))
    monkeypatch.setattr(tasks,'present_task',present)
    monkeypatch.setattr(tasks,'TaskCreatedOut',lambda **kwargs:kwargs)
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(product_runtime=Runtime())))
    result=await tasks.create_task_with_files('conv',request,message='read this',title=None,
        approval_profile='standard',files=[UploadFile(io.BytesIO(data),filename='report.txt')],
        user=SimpleNamespace(id='owner'),db=DB(),access=Access(),security_check=security)
    assert factories==['artifact'] and result['queued'] is False
    assert published['attachments'][0]['name']=='report.txt'
    assert (tmp_path/published['task_id']/'report.txt').read_bytes()==data
