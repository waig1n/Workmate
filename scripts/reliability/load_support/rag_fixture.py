"""Fixed corpus in a real local vector index; deterministic embeddings only.

This is query-path preparation, not HTTP upload/index management acceptance.
Product bindings, authorization, Worker resolver and Core search remain real.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

TEXT='Fixture policy: annual leave is twenty days.'


class Embedding:
    dimensions=2
    supports_multimodal=False
    async def __call__(self,inputs):
        return SimpleNamespace(embeddings=[[1.,0.] for _ in inputs])


@asynccontextmanager
async def resources(settings,*,node_id):
    from agentscope.rag import KnowledgeBase,QdrantStore,Chunk
    from agentscope.message import TextBlock
    async with QdrantStore(location=':memory:') as store:
        kb=KnowledgeBase(name='Fixture Handbook',description='fixed fixture',embedding_model=Embedding(),vector_store=store,collection='fixture-handbook')
        await kb.insert_document([Chunk(content=TextBlock(text=TEXT),source='policy.txt',chunk_index=0,total_chunks=1)],document_id='fixture-doc')
        class Manager:
            async def get_knowledge(self,owner,identifier):
                assert owner=='fixture-user' and identifier=='fixture-runtime-kb'
                return kb
        yield Manager()


async def seed_rag(sessions):
    from workmate_product.persistence.models import KnowledgeBase,Agent,ResourceShare
    async with sessions.begin() as db:
        db.add(KnowledgeBase(id='fixture-kb',scope_id='fixture-scope',name='Fixture Handbook',runtime_id='fixture-runtime-kb',runtime_owner_id='fixture-user',created_by='fixture-user'))
        await db.flush()
        db.add(ResourceShare(id='fixture-kb-share',knowledge_base_id='fixture-kb',target_scope_id='fixture-scope'))
        agent=await db.get(Agent,'fixture-agent')
        agent.knowledge_config={'knowledge_base_ids':['fixture-kb'],'parameters':{'mode':'agentic'}}
