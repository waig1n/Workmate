"""Native knowledge management must not load the legacy Session runtime."""
import os
import subprocess
import sys


def test_management_import_excludes_legacy_runtime():
    result=subprocess.run([sys.executable,'-c','''
import sys,importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='workmate_runtime.knowledge':
            raise AssertionError('legacy knowledge runtime imported')
sys.meta_path.insert(0,Block())
from workmate_runtime.rag.management import KnowledgeManagement
from workmate_runtime.rag.management_router import create_management_router
'''],capture_output=True,text=True,env=os.environ,timeout=30)
    assert result.returncode==0,result.stderr


async def test_management_uses_current_provider_without_session_endpoint():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import FastAPI, HTTPException
    from httpx import AsyncClient, ASGITransport
    from workmate_runtime.rag.management import KnowledgeManagement
    from workmate_runtime.rag.management_router import create_management_router

    access=SimpleNamespace(resolve_knowledge_base=AsyncMock())
    storage=SimpleNamespace(list_knowledge_documents=AsyncMock(return_value=[]))
    management=KnowledgeManagement(SimpleNamespace(knowledge=SimpleNamespace(enabled=True)),
        storage,object(),object(),SimpleNamespace(_access=access))
    ready=True
    async def resolve():
        if not ready:
            raise HTTPException(503,'Knowledge management unavailable')
        return management
    web=FastAPI()
    web.include_router(create_management_router(resolve))
    async with AsyncClient(transport=ASGITransport(app=web),base_url='http://test') as client:
        response=await client.get('/workmate/knowledge/kb/documents',headers={'X-User-ID':'scope-owner'})
        assert response.status_code==200
        assert response.json()=={'items':[],'total':0}
        access.resolve_knowledge_base.assert_awaited_once_with('scope-owner','kb')
        assert (await client.put('/workmate/knowledge/session',json={})).status_code==405
        ready=False
        assert (await client.get('/workmate/knowledge/kb/documents',headers={'X-User-ID':'scope-owner'})).status_code==503
    storage.list_knowledge_documents.assert_awaited_once_with('scope-owner','kb')


async def test_worker_management_auth_readiness_and_shutdown(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from httpx import AsyncClient, ASGITransport
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.roles.worker import create_worker_app
    from workmate_runtime.rag.management import KnowledgeManagement
    settings=RuntimeSettings(internal_token='fixture-token')
    access=SimpleNamespace(resolve_knowledge_base=AsyncMock())
    storage=SimpleNamespace(list_knowledge_documents=AsyncMock(return_value=[]))
    management=KnowledgeManagement(settings,storage,object(),object(),SimpleNamespace(_access=access))
    # Management service availability is explicitly enabled in this fixture.
    settings.knowledge.enabled=True
    service=SimpleNamespace(ready=asyncio.Event(),active_sessions=set(),failures={})
    async def serve(settings,shutdown,*,on_ready):
        service.ready.set()
        on_ready(SimpleNamespace(service=service,knowledge_management=management))
        await shutdown.wait()
        service.ready.clear()
    monkeypatch.setattr('workmate_runtime.roles.worker.serve_worker',serve)
    web=create_worker_app(SimpleNamespace(runtime=settings))
    headers={'X-Workmate-Internal-Token':'fixture-token','X-User-ID':'scope-owner'}
    async with AsyncClient(transport=ASGITransport(app=web),base_url='http://worker') as client:
        assert (await client.get('/workmate/knowledge/kb/documents',headers=headers)).status_code==503
        async with web.router.lifespan_context(web):
            assert (await client.get('/workmate/knowledge/kb/documents')).status_code==403
            response=await client.get('/workmate/knowledge/kb/documents',headers=headers)
            assert response.status_code==200,response.text
            assert response.json()=={'items':[],'total':0}
            assert (await client.put('/workmate/knowledge/session',headers=headers,json={})).status_code==405
        assert (await client.get('/workmate/knowledge/kb/documents',headers=headers)).status_code==503
    storage.list_knowledge_documents.assert_awaited_once_with('scope-owner','kb')
