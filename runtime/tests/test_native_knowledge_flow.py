"""Product transport -> native management -> real local index/search lifecycle.

Only provider embeddings and S3 bytes are deterministic fixtures. Product RBAC
is covered separately; this test exercises service auth and owner-scoped storage.
"""
import asyncio
from types import SimpleNamespace

from httpx import ASGITransport
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
from agentscope.rag import KnowledgeBase,QdrantStore,TextParser
from app.adapter.knowledge_client import KnowledgeServiceClient
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.rag.components import NonEmptyParser
from workmate_runtime.rag.lifecycle import knowledge_resources
from workmate_runtime.roles.worker import create_worker_app
from test_knowledge_staging import Blobs
from test_worker_rag import Embedding


async def test_product_client_stages_then_indexes_searches_downloads_and_deletes(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('Native knowledge must not start AgentScope App')
    monkeypatch.setattr('agentscope.app.create_app',forbidden)
    monkeypatch.setenv('KNOWLEDGE_EMBEDDING_MODEL_API_KEY','deterministic-fixture')
    settings=RuntimeSettings(knowledge={'enabled':True},internal_token='fixture-token',
        knowledge_s3_bucket='bucket',knowledge_embedding_model_dimensions=2)
    storage=AsyncSQLAlchemyStorage(f'sqlite+aiosqlite:///{tmp_path}/knowledge.db')
    bus=InMemoryMessageBus()
    class ManagedBlobs(Blobs):
        closed=False
        async def __aenter__(self): return self
        async def __aexit__(self,*args): self.closed=True
    blobs=ManagedBlobs()
    class Manager(CollectionPerKbManager):
        async def get_knowledge(self,user_id,knowledge_base_id):
            record=await self.get_knowledge_base(user_id,knowledge_base_id)
            return KnowledgeBase(name=record.data.name,description='',embedding_model=Embedding(),
                vector_store=self._vector_store,collection=record.data.collection_name)
    manager=Manager(storage,QdrantStore(location=':memory:'))
    async def serve(_settings,shutdown,*,on_ready):
        async with knowledge_resources(storage=storage,message_bus=bus,manager=manager,
                blob_store=blobs,parsers=[NonEmptyParser(TextParser())],node_id='fixture',settings=settings):
            management=manager.workmate_knowledge_management
            assert management.storage is storage and management.blob_store is blobs
            assert management.message_bus is bus
            ready=asyncio.Event()
            ready.set()
            on_ready(SimpleNamespace(knowledge_management=management,
                service=SimpleNamespace(ready=ready,active_sessions={},failures={})))
            await shutdown.wait()
    monkeypatch.setattr('workmate_runtime.roles.worker.serve_worker',serve)
    web=create_worker_app(SimpleNamespace(runtime=settings))
    client=KnowledgeServiceClient('http://worker','fixture-token',transport=ASGITransport(app=web))
    before=set(asyncio.all_tasks())
    async with web.router.lifespan_context(web):
        kb=await client.knowledge_request('POST','owner',body={'name':'Handbook','description':'Test'})
        kb_id=kb['id']
        content=b'Twenty days of annual leave.'
        uploaded=await client.knowledge_upload('owner',kb_id,'policy.txt','text/plain',content,'document')
        assert uploaded['status']=='uploaded'
        assert await storage.get_knowledge_document('owner',kb_id,'document') is None
        assert (await client.knowledge_request('GET','owner',kb_id,suffix='/documents'))['items']==[]
        staged=await client.knowledge_content('owner',kb_id,'document',1024)
        assert staged[0]==content
        first=await client.knowledge_request('POST','owner',kb_id,suffix='/documents/document/start')
        assert first['id']=='document'
        async with asyncio.timeout(10):
            while True:
                documents=await client.knowledge_request('GET','owner',kb_id,suffix='/documents')
                record=documents['items'][0]
                if record['status'] in {'ready','error'}:
                    break
                await asyncio.sleep(.02)
        assert record['status']=='ready',record
        assert record['chunk_count']>0
        repeated=await client.knowledge_request('POST','owner',kb_id,suffix='/documents/document/start')
        assert repeated['id']=='document' and repeated['status']=='ready'
        result=await client.knowledge_request('POST','owner',kb_id,suffix='/search',
            body={'query':'leave','top_k':5,'product_id':'product-kb'})
        assert result['sources'] and all(source['document_id']=='document' for source in result['sources'])
        assert (await client.knowledge_content('owner',kb_id,'document',1024))[0]==content
        await client.knowledge_request('DELETE','owner',kb_id,suffix='/documents/document')
        assert await storage.get_knowledge_document('owner',kb_id,'document') is None
        assert not blobs.items
        assert (await client.knowledge_request('POST','owner',kb_id,suffix='/search',
            body={'query':'leave','top_k':5,'product_id':'product-kb'}))['sources']==[]
        await client.knowledge_request('DELETE','owner',kb_id,body={'document_ids':[]})
        assert await storage.get_knowledge_base('owner',kb_id) is None
    await asyncio.sleep(0)
    assert blobs.closed
    assert not [task for task in asyncio.all_tasks()-before if not task.done()]
