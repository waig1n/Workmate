"""Native Qdrant handles use trusted Product identities from real PostgreSQL."""
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from workmate_runtime.rag.resolver import WorkerKnowledgeResolver
from workmate_runtime.rag.middleware import KnowledgeSearchDenied
from workmate_product.config.knowledge import KnowledgeConfig
from test_execution_context import database
from test_worker_rag import rag
from test_worker_agent_assembly import assembly


@pytest.mark.parametrize('mutation',[None,'disable','remap','missing_handle'])
async def test_exact_product_mapping_and_revalidation(database,rag,mutation):
    sessions,admin=database
    await admin.execute("""INSERT INTO knowledge_bases(id,scope_id,name,description,runtime_id,runtime_owner_id,enabled,created_at,updated_at)
        VALUES ('kb','scope','Handbook','','native','owner',true,now(),now())""")
    calls=[]
    class Manager:
        async def get_knowledge(self,owner,runtime_id):
            calls.append((owner,runtime_id))
            if mutation=='missing_handle':
                return None
            return rag[0]
    resolver=WorkerKnowledgeResolver(sessions,Manager(),timeout=1)
    context=SimpleNamespace(knowledge_config=KnowledgeConfig(knowledge_base_ids=['kb']))
    if mutation=='missing_handle':
        with pytest.raises(ValueError,match='Exact native handles'):
            await resolver.prepare(context,rag[2],authorize_preparation=rag[2])
        return
    middleware=await resolver.prepare(context,rag[2],authorize_preparation=rag[2])
    assert calls==[('owner','native')]
    if mutation:
        column,value=('enabled',False) if mutation=='disable' else ('runtime_owner_id','other')
        async with sessions.begin() as db:
            await db.execute(text(f'UPDATE knowledge_bases SET {column}=:value'),{'value':value})
        with pytest.raises(KnowledgeSearchDenied):
            await middleware.search('leave')
        assert middleware.pending_sources==[]
    else:
        assert (await middleware.search('leave'))['sources'][0]['document_id']=='doc'


async def test_real_resolver_prepares_before_reply_exists(database,rag,assembly):
    from dataclasses import replace
    from workmate_runtime.execution.controller import ExecutionGuard
    from test_worker_agent_assembly import OWNER
    sessions,admin=database
    assembler,fixture,_=assembly
    await admin.execute("""INSERT INTO knowledge_bases(id,scope_id,name,description,runtime_id,runtime_owner_id,enabled,created_at,updated_at)
        VALUES ('kb','scope','Handbook','','native','owner',true,now(),now())""")
    config=KnowledgeConfig(knowledge_base_ids=['kb'])
    fixture['context']=replace(fixture['context'],knowledge_config=config,
        initial_spec=fixture['context'].initial_spec.model_copy(update={'knowledge_base_ids':('kb',)}))
    class Manager:
        async def get_knowledge(self,*args):
            return rag[0]
    assembler.knowledge_resolver=WorkerKnowledgeResolver(sessions,Manager(),timeout=1)
    guard=ExecutionGuard(None,OWNER,'command')
    prepared=await assembler.prepare_start(OWNER,None,guard)
    assert guard.reply_id is None
    assert prepared.engine.agent.name=='agent'
