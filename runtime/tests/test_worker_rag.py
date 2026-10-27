"""Native RAG keeps real Core search, source events, and transient static hints."""
from types import SimpleNamespace
import asyncio

import pytest
from agentscope.agent import Agent,InjectionConfig
from agentscope.message import TextBlock,UserMsg,ToolCallBlock,HintBlock
from agentscope.rag import KnowledgeBase,QdrantStore,Chunk
from agentscope.tool import Toolkit
from agentscope.state import AgentState
from workmate_runtime.engines.agentscope import CoreEngine
from workmate_runtime.rag.middleware import WorkerRAGMiddleware,KnowledgeSearchDenied
from workmate_product.config.knowledge import KnowledgeConfig
from test_core_engine import ScriptModel


class Embedding:
    dimensions=2
    supports_multimodal=False
    async def __call__(self,inputs):
        return SimpleNamespace(embeddings=[[1.,0.] for _ in inputs])


@pytest.fixture
async def rag():
    async with QdrantStore(location=':memory:') as store:
        knowledge=KnowledgeBase(name='Handbook',description='test',embedding_model=Embedding(),
            vector_store=store,collection='handbook')
        await knowledge.insert_document([Chunk(content=TextBlock(text='Twenty days leave.'),source='policy.md',
            chunk_index=0,total_chunks=1,metadata={'page':1,'private':'do not expose'})],document_id='doc')
        policy={'allowed':True,'calls':0}
        async def authorize():
            policy['calls']+=1
            if not policy['allowed']:
                raise KnowledgeSearchDenied('revoked')
        yield knowledge,policy,authorize


def middleware(rag,*,mode='agentic',timeout=.5):
    knowledge,policy,authorize=rag
    return WorkerRAGMiddleware({'kb':knowledge},KnowledgeConfig(knowledge_base_ids=['kb'],parameters={'mode':mode}),
        authorize=authorize,timeout=timeout)


async def test_native_search_returns_stable_authorized_sources(rag):
    result=await middleware(rag).search('leave')
    source,=result['sources']
    assert source['knowledge_base_id']=='kb' and source['document_id']=='doc'
    assert source['metadata']=={'page':1} and source['excerpt']=='Twenty days leave.'
    assert source['source_id'].startswith('kb-')
    assert rag[1]['calls']==2


async def test_unknown_kb_and_revocation_never_become_empty_matches(rag):
    adapter=middleware(rag)
    with pytest.raises(KnowledgeSearchDenied):
        await adapter.search('leave',['other'])
    rag[1]['allowed']=False
    with pytest.raises(KnowledgeSearchDenied):
        await adapter.search('leave')
    assert adapter.pending_sources==[]


async def test_revocation_during_search_discards_retrieved_private_content(rag,monkeypatch):
    knowledge,policy,authorize=rag
    original=knowledge.search
    async def revoke(**kwargs):
        result=await original(**kwargs)
        policy['allowed']=False
        return result
    monkeypatch.setattr(knowledge,'search',revoke)
    adapter=middleware(rag)
    with pytest.raises(KnowledgeSearchDenied):
        await adapter.search('leave')
    assert adapter.pending_sources==[]


async def test_search_timeout_cancels_children_and_does_not_claim_no_matches(rag,monkeypatch):
    exited=asyncio.Event()
    async def blocked(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
    monkeypatch.setattr(rag[0],'search',blocked)
    with pytest.raises(TimeoutError):
        await middleware(rag,timeout=.02).search('leave')
    assert exited.is_set()


@pytest.mark.parametrize('mode',['agentic','static'])
async def test_real_core_emits_reply_bound_sources_without_app_bus_and_removes_hints(rag,mode):
    adapter=middleware(rag,mode=mode)
    responses=([[ToolCallBlock(id='search',name='search_knowledge',input='{"query":"leave"}')]] if mode=='agentic' else [])
    responses.append([TextBlock(text='Use the policy.')])
    engine=CoreEngine(Agent(name='agent',system_prompt='test',model=ScriptModel(responses),
        state=AgentState(session_id='session'),toolkit=Toolkit(tools=await adapter.list_tools()),middlewares=[adapter],
        injection_config=InjectionConfig(inject_runtime_state=False)))
    events=[e async for e in engine.reply_stream(UserMsg(name='user',content='leave policy'))]
    sources=[e for e in events if e.type=='CUSTOM']
    assert len(sources)==1, [e.model_dump() for e in events]
    assert sources[0].reply_id==events[0].reply_id==sources[0].value['reply_id']
    assert sources[0].value['sources'][0]['knowledge_base_id']=='kb'
    assert events[-1].type=='REPLY_END'
    assert not any(isinstance(block,HintBlock) and block.source=='KnowledgeBase'
        for msg in engine.snapshot().to_state().context for block in msg.content)
