"""Provider wire policy and immutable embedding dimensionality."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import TextBlock
from agentscope.rag import Chunk, KnowledgeBase
from workmate_runtime.embedding_policy import (
    resolve_dimensions_policy,
    DimensionCheckedEmbedding,
)


def resolve(**kwargs):
    defaults = dict(
        provider="openai",
        model="text-embedding-3-small",
        base_url=None,
        dimensions=1024,
        mode="auto",
    )
    return resolve_dimensions_policy(**(defaults | kwargs))


@pytest.mark.parametrize(
    "model,size", [("text-embedding-3-small", 1536), ("text-embedding-3-large", 3072)]
)
def test_official_openai_auto_and_dimension_ranges(model, size):
    assert resolve(model=model, dimensions=size) is True
    assert (
        resolve(model=model, dimensions=1, base_url="https://api.openai.com/v1") is True
    )
    with pytest.raises(ValueError):
        resolve(model=model, dimensions=size + 1)


@pytest.mark.parametrize("host", ["api.siliconflow.cn", "api.siliconflow.com"])
def test_siliconflow_bge_omits_dimensions_but_requires_fixed_length(host):
    assert resolve(model="BAAI/bge-m3", base_url="https://" + host + "/v1") is False
    with pytest.raises(ValueError):
        resolve(model="BAAI/bge-m3", base_url="https://" + host + "/v1", dimensions=768)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "https://unknown.example/v1"},
        {"model": "unknown"},
        {"provider": "dashscope", "model": "text-embedding-v4"},
    ],
)
def test_unknown_auto_requires_explicit_strategy(kwargs):
    with pytest.raises(ValueError, match="explicit"):
        resolve(**kwargs)
    assert resolve(**kwargs, mode="send") is True


def test_explicit_omit_for_unknown_provider():
    assert resolve(base_url="https://unknown.example", mode="omit") is False


def test_dashscope_cannot_claim_to_omit_unsupported_wire_parameter():
    with pytest.raises(ValueError, match="omit"):
        resolve(provider="dashscope", mode="omit")


@pytest.mark.parametrize("size", [2, 4])
async def test_bad_embedding_lengths_never_reach_vector_store(size):
    call = AsyncMock(return_value=SimpleNamespace(embeddings=[[0.0] * size]))

    class Model:
        dimensions = 3
        supports_multimodal = False

        async def __call__(self, inputs):
            return await call(inputs)

    vector_store = SimpleNamespace(
        create_collection=AsyncMock(),
        has_collection=AsyncMock(return_value=True),
        insert=AsyncMock(),
    )
    knowledge = KnowledgeBase(
        name="test",
        description="",
        embedding_model=DimensionCheckedEmbedding(Model()),
        vector_store=vector_store,
        collection="test",
    )
    chunk = Chunk(
        content=TextBlock(text="text"),
        source="source.txt",
        chunk_index=0,
        total_chunks=1,
    )
    with pytest.raises(RuntimeError, match="dimension"):
        await knowledge.insert_document(chunks=[chunk], document_id="doc")
    vector_store.insert.assert_not_awaited()


async def test_valid_vectors_are_not_truncated_or_padded():
    vectors = [[1.0, 2.0, 3.0]]

    class Model:
        dimensions = 3

        async def __call__(self, inputs):
            return SimpleNamespace(embeddings=vectors)

    result = await DimensionCheckedEmbedding(Model())(["text"])
    assert result.embeddings is vectors


async def test_bad_query_embedding_never_reaches_vector_search():
    class Model:
        dimensions = 3
        supports_multimodal = False

        async def __call__(self, inputs):
            return SimpleNamespace(embeddings=[[1.0, 2.0]])

    store = SimpleNamespace(
        has_collection=AsyncMock(return_value=True),
        create_collection=AsyncMock(),
        search=AsyncMock(),
    )
    knowledge = KnowledgeBase(
        name="test",
        description="",
        embedding_model=DimensionCheckedEmbedding(Model()),
        vector_store=store,
        collection="test",
    )
    with pytest.raises(RuntimeError, match="dimension"):
        await knowledge.search(queries=["question"])
    store.search.assert_not_awaited()


@pytest.mark.parametrize(
    "model,dimension",
    [("text-embedding-3-small", 1536), ("text-embedding-3-large", 3072)],
)
def test_official_omit_requires_native_output_dimension(model, dimension):
    assert resolve(model=model, dimensions=dimension, mode="omit") is False
    with pytest.raises(ValueError, match="default dimension"):
        resolve(model=model, dimensions=dimension - 1, mode="omit")


def test_known_siliconflow_rejects_forcing_unsupported_parameter():
    with pytest.raises(ValueError, match="does not support"):
        resolve(
            model="BAAI/bge-m3", base_url="https://api.siliconflow.cn/v1", mode="send"
        )
