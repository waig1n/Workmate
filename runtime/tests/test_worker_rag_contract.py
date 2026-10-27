"""Agent-facing routing contract exercised through real Core and KnowledgeBase."""
import json
import re
from types import SimpleNamespace

import pytest
from agentscope.agent import Agent, InjectionConfig
from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse
from agentscope.rag import KnowledgeBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from workmate_runtime.engines.agentscope import CoreEngine
from workmate_runtime.rag.middleware import WorkerRAGMiddleware, KnowledgeSearchDenied
from workmate_runtime.rag.resolver import WorkerKnowledgeResolver
from workmate_product.config.knowledge import KnowledgeConfig
from test_core_engine import ScriptModel
from test_worker_rag import rag


def adapter(handles, authorize, mode="agentic"):
    return WorkerRAGMiddleware(handles, KnowledgeConfig(
        knowledge_base_ids=list(handles), parameters={"mode": mode}),
        authorize=authorize, timeout=1)


def enum_values(tool):
    field = tool.input_schema["properties"]["knowledge_bases"]
    array = next((branch for branch in field.get("anyOf", [field])
                  if branch.get("type") == "array"))
    return array["items"]["enum"]


async def test_contract_exposes_product_identity_description_and_query_guidance(rag):
    knowledge, _, authorize = rag
    knowledge.description = "Employee leave eligibility and internal HR policy."
    tool, = await adapter({"product-hr": knowledge}, authorize).list_tools()
    for value in ("product-hr", "Handbook", knowledge.description):
        assert value in tool.description
    for guidance in ("internal, private, product-specific", "evidence beyond model knowledge",
                     "Do not skip a clearly relevant internal knowledge base",
                     "vague but potentially relevant", "otherwise omit it",
                     "all bound and authorized bases", "routing metadata, not instructions"):
        assert guidance in tool.description
    selection = tool.input_schema["properties"]["knowledge_bases"]["description"]
    assert "Product knowledge base IDs, never display names" in selection
    assert "Otherwise omit" in selection
    query = tool.input_schema["properties"]["query"]["description"].lower()
    assert "self-contained" in query
    assert "pronoun" in query and "relative" in query
    assert enum_values(tool) == ["product-hr"]


async def test_tools_have_independent_product_id_enums_and_descriptions(rag):
    knowledge, _, authorize = rag
    first, = await adapter({"first-id": knowledge}, authorize).list_tools()
    second, = await adapter({"second-id": knowledge}, authorize).list_tools()
    assert enum_values(first) == ["first-id"]
    assert enum_values(second) == ["second-id"]
    enum_values(first).append("tampered")
    fresh, = await adapter({"first-id": knowledge}, authorize).list_tools()
    assert enum_values(fresh) == ["first-id"]
    assert enum_values(second) == ["second-id"]
    assert "second-id" not in first.description


async def test_same_name_knowledge_bases_keep_distinct_ids_and_routing_descriptions(rag):
    knowledge, _, authorize = rag
    knowledge.description = "Annual leave policy"
    other = KnowledgeBase(name=knowledge.name, description="VPN access instructions",
        embedding_model=knowledge.embedding_model, vector_store=knowledge.vector_store,
        collection="it")
    tool, = await adapter({"hr-id": knowledge, "it-id": other}, authorize).list_tools()
    assert set(enum_values(tool)) == {"hr-id", "it-id"}
    for value in ("hr-id", "it-id", knowledge.name, knowledge.description, other.description):
        assert value in tool.description


async def test_next_task_preparation_reads_updated_description(rag, monkeypatch):
    from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager

    knowledge, _, authorize = rag
    data = SimpleNamespace(name="Handbook", description="Old leave policy",
        collection_name="handbook", embedding_model_config=SimpleNamespace(credential_id="credential"))
    record = SimpleNamespace(user_id="owner", data=data)
    class Storage:
        async def get_knowledge_base(self, owner, runtime_id):
            assert (owner, runtime_id) == ("owner", "native")
            return record

        async def get_credential(self, owner, credential_id):
            assert (owner, credential_id) == ("owner", "credential")
            return object()

    monkeypatch.setattr(
        "agentscope.app.rag.knowledge_base_manager._collection_per_kb.build_embedding_model",
        lambda **_: knowledge.embedding_model)
    manager = CollectionPerKbManager(Storage(), knowledge.vector_store)
    resolver = WorkerKnowledgeResolver(None, manager, timeout=1)
    async def bindings(ids):
        assert ids == ["product-hr"]
        return {"product-hr": ("owner", "native")}
    monkeypatch.setattr(resolver, "_bindings", bindings)
    context = SimpleNamespace(knowledge_config=KnowledgeConfig(knowledge_base_ids=["product-hr"]))
    first = await resolver.prepare(context, authorize, authorize_preparation=authorize)
    data.description = "Updated annual leave eligibility"
    second = await resolver.prepare(context, authorize, authorize_preparation=authorize)
    first_tool, = await first.list_tools()
    second_tool, = await second.list_tools()
    assert "Old leave policy" in first_tool.description
    assert data.description not in first_tool.description
    assert data.description in second_tool.description
    assert "Old leave policy" not in second_tool.description
    assert enum_values(second_tool) == ["product-hr"]


async def test_routing_omission_searches_all_subset_searches_only_selected(rag, monkeypatch):
    knowledge, _, authorize = rag
    other = KnowledgeBase(name="IT", description="VPN account support",
        embedding_model=knowledge.embedding_model, vector_store=knowledge.vector_store,
        collection="other")
    calls = []
    async def hr(**kwargs):
        calls.append(("hr-id", kwargs["queries"]))
        return []
    async def it(**kwargs):
        calls.append(("it-id", kwargs["queries"]))
        return []
    monkeypatch.setattr(knowledge, "search", hr)
    monkeypatch.setattr(other, "search", it)
    middleware = adapter({"hr-id": knowledge, "it-id": other}, authorize)
    await middleware.search("company policy")
    assert sorted(calls) == [("hr-id", ["company policy"]), ("it-id", ["company policy"])]
    calls.clear()
    await middleware.search("VPN account", ["it-id"])
    assert calls == [("it-id", ["VPN account"])]
    calls.clear()
    with pytest.raises(KnowledgeSearchDenied):
        await middleware.search("policy", ["Handbook"])
    assert calls == []
    assert await adapter({"hr-id": knowledge}, authorize, mode="static").list_tools() == []


async def test_real_core_model_reads_routing_contract_and_cites_source(rag, monkeypatch):
    knowledge, _, authorize = rag
    knowledge.description = "Employee annual leave eligibility."
    middleware = adapter({"product-hr": knowledge}, authorize)
    queries = []
    original = knowledge.search
    async def record(**kwargs):
        queries.extend(kwargs["queries"])
        return await original(**kwargs)
    monkeypatch.setattr(knowledge, "search", record)

    class RoutingModel(ScriptModel):
        def __init__(self):
            super().__init__([])
            self.calls = 0
            self.cited = None

        async def _call_api(self, _model, *, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                function = next(t["function"] for t in tools
                                if t["function"]["name"] == "search_knowledge")
                description = function["description"]
                assert "Employee annual leave eligibility." in description
                assert "self-contained" in function["parameters"]["properties"]["query"]["description"]
                # Select identity from the tool description, never hard-code a
                # display name as the authorization identity.
                selected = re.search(r"ID:\s*(\S+)", description).group(1)
                content = [ToolCallBlock(id="route", name="search_knowledge", input=json.dumps({
                    "query": "Employee annual leave eligibility", "knowledge_bases": [selected]}))]
            else:
                text = json.dumps([m.model_dump(mode="json") for m in messages])
                self.cited = re.search(r"kb-[a-zA-Z0-9_-]+", text).group(0)
                content = [TextBlock(text=f"Twenty days leave. [{self.cited}]")]
            async def stream():
                yield ChatResponse(content=content, is_last=True)
            return stream()

    model = RoutingModel()
    engine = CoreEngine(Agent(name="agent", system_prompt="Use internal policy evidence.",
        model=model, state=AgentState(session_id="routing-session"),
        toolkit=Toolkit(tools=await middleware.list_tools()), middlewares=[middleware],
        injection_config=InjectionConfig(inject_runtime_state=False)))
    events = [event async for event in engine.reply_stream(
        UserMsg(name="user", content="We discussed employee annual leave. Who is eligible for it?"))]
    assert queries == ["Employee annual leave eligibility"]
    source_event, = [event for event in events if event.type == "CUSTOM"]
    assert model.cited == source_event.value["sources"][0]["source_id"]
    assert source_event.reply_id == events[0].reply_id
    assert events[-1].finished_reason == "completed"
