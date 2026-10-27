"""Auxiliary indexing runs with native storage/queue/vector components, no App."""
import asyncio
import io
import pytest
from agentscope.app.storage import AsyncSQLAlchemyStorage,EmbeddingModelConfig,KnowledgeDocumentRecord,KnowledgeDocumentData
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.rag.blob_store import LocalBlobStore
from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
from agentscope.app._bus_ops import enqueue_index_task
from agentscope.rag import QdrantStore,KnowledgeBase,TextParser
from workmate_runtime.rag.lifecycle import knowledge_resources
from workmate_runtime.rag.components import NonEmptyParser
from test_worker_rag import Embedding


@pytest.mark.parametrize('content',[b'Twenty days of leave.',b'   '])
async def test_document_indexing_without_app_and_clean_lifecycle(tmp_path,monkeypatch,content):
    def forbidden(*args,**kwargs):
        raise AssertionError('App execution cannot start')
    monkeypatch.setattr('agentscope.app.create_app',forbidden)
    storage=AsyncSQLAlchemyStorage(f'sqlite+aiosqlite:///{tmp_path}/knowledge.db')
    bus=InMemoryMessageBus()
    blobs=LocalBlobStore(tmp_path/'blobs')
    class Manager(CollectionPerKbManager):
        async def get_knowledge(self,user_id,knowledge_base_id):
            record=await self.get_knowledge_base(user_id,knowledge_base_id)
            return KnowledgeBase(name=record.data.name,description='',embedding_model=Embedding(),
                vector_store=self._vector_store,collection=record.data.collection_name)
    manager=Manager(storage,QdrantStore(location=':memory:'))
    before=set(asyncio.all_tasks())
    async with knowledge_resources(storage=storage,message_bus=bus,manager=manager,
            blob_store=blobs,parsers=[NonEmptyParser(TextParser())],node_id='index-test'):
        kb=await manager.create_knowledge_base('owner','Handbook','',EmbeddingModelConfig(
            type='openai_credential',model='deterministic',credential_id='fixture',dimensions=2))
        uri=await blobs.write_stream('document',io.BytesIO(content))
        document=KnowledgeDocumentRecord(user_id='owner',knowledge_base_id=kb.id,
            data=KnowledgeDocumentData(filename='policy.txt',size=len(content),content_type='text/plain',blob_uri=uri))
        await storage.upsert_knowledge_document('owner',document)
        await enqueue_index_task(bus,'owner',kb.id,document.id)
        async with asyncio.timeout(10):
            while True:
                record=await storage.get_knowledge_document('owner',kb.id,document.id)
                if record.status in {'ready','error'}:
                    break
                await asyncio.sleep(.02)
        if content.strip():
            assert record.status=='ready' and record.data.chunk_count>0
            handle=await manager.get_knowledge('owner',kb.id)
            assert (await handle.search(queries=['leave']))[0].document_id==document.id
        else:
            assert record.status=='error' and record.data.chunk_count==0
    await asyncio.sleep(0)
    assert not [task for task in asyncio.all_tasks()-before if not task.done()]


class FailedSubscriptionBus(InMemoryMessageBus):
    def __init__(self,*,startup=False):
        super().__init__()
        self.startup=startup
        self.fail=asyncio.Event()
        self.closed=False
    async def subscribe(self,key,*,on_ready=None):
        if self.startup:
            raise RuntimeError('fixture subscription startup failed')
        if on_ready is not None:
            on_ready()
        await self.fail.wait()
        raise RuntimeError('fixture subscription dropped')
        if False:
            yield
    async def __aexit__(self,*args):
        self.closed=True
        await super().__aexit__(*args)


def native_components(tmp_path,bus):
    storage=AsyncSQLAlchemyStorage(f'sqlite+aiosqlite:///{tmp_path}/knowledge.db')
    manager=CollectionPerKbManager(storage,QdrantStore(location=':memory:'))
    return dict(storage=storage,message_bus=bus,manager=manager,
        blob_store=LocalBlobStore(tmp_path/'blobs'),parsers=[NonEmptyParser(TextParser())],node_id='test')


async def test_subscription_startup_failure_rejects_ready_and_closes_resources(tmp_path):
    bus=FailedSubscriptionBus(startup=True)
    components=native_components(tmp_path,bus)
    before=set(asyncio.all_tasks())
    with pytest.raises(RuntimeError,match='subscription did not start'):
        async with knowledge_resources(**components):
            pytest.fail('A dead consumer must not become ready')
    assert bus.closed
    assert not [task for task in asyncio.all_tasks()-before if not task.done()]


async def test_runtime_consumer_death_signals_role_and_teardown_joins_tasks(tmp_path):
    bus=FailedSubscriptionBus()
    components=native_components(tmp_path,bus)
    before=set(asyncio.all_tasks())
    async with knowledge_resources(**components) as manager:
        assert not manager.workmate_indexing_failure.done()
        bus.fail.set()
        failure=await asyncio.wait_for(asyncio.shield(manager.workmate_indexing_failure),2)
        assert isinstance(failure,RuntimeError)
        assert str(failure)=='Knowledge indexing background stopped'
    assert bus.closed
    assert not [task for task in asyncio.all_tasks()-before if not task.done()]


async def test_partially_entered_storage_is_disposed_on_startup_failure(tmp_path):
    closed=[]
    class Storage(AsyncSQLAlchemyStorage):
        async def __aenter__(self):
            await super().__aenter__()
            raise RuntimeError('fixture schema post-initialization failure')
        async def __aexit__(self,*args):
            await super().__aexit__(*args)
            closed.append(True)
    components=native_components(tmp_path,InMemoryMessageBus())
    components['storage']=Storage(f'sqlite+aiosqlite:///{tmp_path}/partial.db')
    with pytest.raises(RuntimeError,match='post-initialization'):
        async with knowledge_resources(**components):
            pytest.fail('Failed storage initialization cannot yield resources')
    assert closed==[True]


async def test_configured_redis_url_preserves_tls_auth_port_database_and_closes_pool(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from redis.asyncio import ConnectionPool
    from redis.asyncio.connection import SSLConnection
    from workmate_runtime.rag.lifecycle import configured_knowledge_resources
    pools=[]
    closed=[]
    create=ConnectionPool.from_url
    def pool(url,**kwargs):
        result=create(url,**kwargs)
        pools.append(result)
        close=result.aclose
        async def tracked_close():
            closed.append(True)
            await close()
        result.aclose=tracked_close
        return result
    monkeypatch.setattr(ConnectionPool,'from_url',pool)
    monkeypatch.setattr('workmate_runtime.rag.components.build_knowledge_components',lambda *_:{
        'knowledge_base_manager':object(),'blob_store':object(),'knowledge_parsers':[]})
    @asynccontextmanager
    async def inspect(**kwargs):
        bus=kwargs['message_bus']
        assert bus._external_pool is pools[0]
        yield kwargs['manager']
    monkeypatch.setattr('workmate_runtime.rag.lifecycle.knowledge_resources',inspect)
    settings=SimpleNamespace(knowledge=SimpleNamespace(enabled=True),
        as_database_url='sqlite+aiosqlite:///:memory:',
        as_redis_url='rediss://reader:p%40ss@cache.example:6380/4?socket_timeout=2&decode_responses=false')
    async with configured_knowledge_resources(settings,node_id='test'):
        assert pools[0].connection_class is SSLConnection
        values=pools[0].connection_kwargs
        assert (values['host'],values['port'],values['db'],values['username'],values['password'])==(
            'cache.example',6380,4,'reader','p@ss')
        assert values['socket_timeout']==2 and values['decode_responses'] is True
    assert closed==[True]
