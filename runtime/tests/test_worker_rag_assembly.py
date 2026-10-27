"""RAG tools actually enter the native Worker Toolkit and enforce live DENY."""
from dataclasses import replace
import pytest
from agentscope.message import ToolCallBlock, TextBlock
from workmate_product.config.knowledge import KnowledgeConfig
from workmate_product.application.execution_context import ToolPolicy
from workmate_runtime.rag.middleware import WorkerRAGMiddleware
from workmate_runtime.assembly.agent import AgentAssemblyError
from test_worker_agent_assembly import assembly, OWNER, LocalGuard
from test_worker_rag import rag
from test_core_engine import ScriptModel


@pytest.mark.parametrize('mode', ['agentic','static'])
async def test_worker_assembly_executes_bound_native_knowledge(assembly,rag,monkeypatch,mode):
    assembler,fixture,_ = assembly
    config=KnowledgeConfig(knowledge_base_ids=['kb'],parameters={'mode':mode})
    fixture['context']=replace(fixture['context'],knowledge_config=config,
        initial_spec=fixture['context'].initial_spec.model_copy(update={'knowledge_base_ids':('kb',)}))
    checks=[]
    class Resolver:
        async def prepare(self,context,authorize,*,authorize_preparation):
            await authorize_preparation()
            async def checked():
                await authorize()
                checks.append(True)
            return WorkerRAGMiddleware({'kb':rag[0]},context.knowledge_config,authorize=checked,timeout=1)
    assembler.knowledge_resolver=Resolver()
    responses=([[ToolCallBlock(id='search',name='search_knowledge',input='{"query":"leave"}')]]
               if mode=='agentic' else [])+[ [TextBlock(text='done')] ]
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',lambda *_:ScriptModel(responses))
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    events=[e async for e in prepared.engine.reply_stream(prepared.inputs)]
    sources=[e for e in events if e.type=='CUSTOM' and e.name=='knowledge_sources']
    assert len(sources)==1 and sources[0].value['sources'][0]['knowledge_base_id']=='kb'
    assert len(checks)==2


async def test_static_search_cannot_bypass_live_product_deny(assembly,rag):
    assembler,fixture,_=assembly
    config=KnowledgeConfig(knowledge_base_ids=['kb'],parameters={'mode':'static'})
    fixture['context']=replace(fixture['context'],knowledge_config=config,
        initial_spec=fixture['context'].initial_spec.model_copy(update={'knowledge_base_ids':('kb',)}))
    class Resolver:
        async def prepare(self,context,authorize,*,authorize_preparation):
            await authorize_preparation()
            return WorkerRAGMiddleware({'kb':rag[0]},config,authorize=authorize,timeout=1)
    assembler.knowledge_resolver=Resolver()
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    fixture['context']=replace(fixture['context'],effective_tool_policy=ToolPolicy(denied_tools=('search_knowledge',)))
    events=[]
    with pytest.raises(AgentAssemblyError,match='Knowledge retrieval is denied'):
        async for event in prepared.engine.reply_stream(prepared.inputs):
            events.append(event)
    assert not any(e.type=='CUSTOM' for e in events)


async def test_agentic_search_cannot_bypass_live_product_deny(assembly,rag,monkeypatch):
    assembler,fixture,_=assembly
    config=KnowledgeConfig(knowledge_base_ids=['kb'],parameters={'mode':'agentic'})
    fixture['context']=replace(fixture['context'],knowledge_config=config,
        initial_spec=fixture['context'].initial_spec.model_copy(update={'knowledge_base_ids':('kb',)}))
    class Resolver:
        async def prepare(self,context,authorize,*,authorize_preparation):
            await authorize_preparation()
            return WorkerRAGMiddleware({'kb':rag[0]},config,authorize=authorize,timeout=1)
    assembler.knowledge_resolver=Resolver()
    searched=[]
    async def unexpected_search(**kwargs):
        searched.append(kwargs)
        return []
    monkeypatch.setattr(rag[0],'search',unexpected_search)
    responses=[
        [ToolCallBlock(id='search',name='search_knowledge',
            input='{"query":"employee leave eligibility","knowledge_bases":["kb"]}')],
        [TextBlock(text='The knowledge search was denied.')],
    ]
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',lambda *_:ScriptModel(responses))
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    # Revoke after assembly: the schema already exposes the formerly allowed
    # binding, but the Tool ALLOW decision must not bypass live Product policy.
    fixture['context']=replace(fixture['context'],effective_tool_policy=ToolPolicy(
        denied_tools=('search_knowledge',)))
    events=[e async for e in prepared.engine.reply_stream(prepared.inputs)]
    assert searched==[]
    assert not any(e.type=='CUSTOM' and e.name=='knowledge_sources' for e in events)
    assert any(e.type=='TOOL_RESULT_END' and e.state=='denied' for e in events)
    assert events[-1].type=='REPLY_END'
