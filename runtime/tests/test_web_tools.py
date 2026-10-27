import json

import httpx
import pytest

from workmate_runtime.config import RuntimeSettings
from workmate_runtime.web_providers import (
    Crawl4AIFetchProvider,
    SearxngSearchProvider,
    validate_public_url,
)
from workmate_runtime.web_tools import make_web_tool_factory


async def _public_resolver(host, port):
    del host, port
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_image_search_category_and_safe_result_fields():
    def handler(request):
        assert request.url.params["categories"] == "images"
        return httpx.Response(200, json={"results": [
            {"url": "https://example.com/page", "img_src": "javascript:alert(1)"},
            {"url": "https://example.com/page", "title": "Photo",
             "img_src": "https://example.com/photo.jpg", "thumbnail_src": "data:image/png;base64,aaa"},
        ]})
    provider = SearxngSearchProvider(base_url="https://search.test", max_results=2,
        max_response_bytes=4096, transport=httpx.MockTransport(handler))
    result = await provider.search("山峰", category="images")
    assert result["category"] == "images"
    assert len(result["results"]) == 1
    assert result["results"][0]["image_url"] == "https://example.com/photo.jpg"
    assert result["results"][0]["url"] == "https://example.com/page"
    assert result["results"][0]["thumbnail_url"] is None
    with pytest.raises(ValueError, match="category"):
        await provider.search("test", category="video")


@pytest.mark.asyncio
async def test_validate_public_url_blocks_private_and_credential_targets():
    async def private_resolver(host, port):
        del host, port
        return ["127.0.0.1"]

    with pytest.raises(ValueError, match="blocked"):
        await validate_public_url("http://localhost/admin", private_resolver)
    with pytest.raises(ValueError, match="credentials"):
        await validate_public_url(
            "https://user:pass@example.com/", _public_resolver,
        )


@pytest.mark.asyncio
async def test_searxng_provider_normalizes_and_limits_results():
    def handler(request):
        assert request.url.path == "/search"
        return httpx.Response(200, json={"results": [
            {"title": "Unsafe", "url": "javascript:alert(1)", "content": ""},
            {"title": "One", "url": "https://one.test", "content": "A"},
            {"title": "Two", "url": "https://two.test", "content": "B"},
        ]})

    provider = SearxngSearchProvider(
        base_url="https://search.test",
        max_results=1,
        max_response_bytes=4096,
        transport=httpx.MockTransport(handler),
    )
    result = await provider.search("workmate")
    assert result["source"] == "searxng"
    assert result["results"] == [{
        "title": "One",
        "url": "https://one.test",
        "snippet": "A",
        "source": "searxng",
    }]


@pytest.mark.asyncio
async def test_crawl4ai_provider_validates_result_and_truncates_content():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, json={"results": [{
            "url": "https://example.com/final",
            "redirect_chain": ["https://example.com/start"],
            "markdown": {"fit_markdown": "123456"},
            "metadata": {"title": "Example"},
        }]})

    provider = Crawl4AIFetchProvider(
        base_url="https://fetch.test",
        token="secret",
        timeout_seconds=5,
        extract_mode="fit",
        max_response_bytes=4096,
        max_extracted_chars=5,
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    assert await provider.fetch("https://example.com/start") == {
        "url": "https://example.com/final",
        "title": "Example",
        "content": "12345",
        "truncated": True,
        "source": "https://example.com/final",
    }


@pytest.mark.asyncio
async def test_web_factory_uses_explicit_model_visible_schemas():
    settings = RuntimeSettings(
        web_enabled=True,
        web_tools=[
            {
                "name": "web_search",
                "use": "workmate_runtime.web_providers:SearxngSearchProvider",
                "base_url": "https://search.test",
                "max_results": 8,
            },
            {
                "name": "web_fetch",
                "use": "workmate_runtime.web_providers:Crawl4AIFetchProvider",
                "base_url": "https://fetch.test",
                "token": "",
                "timeout_seconds": 30,
                "extract_mode": "fit",
            },
        ],
    )
    tools = await make_web_tool_factory(settings)("user", "agent", "session")
    schemas = {tool.name: tool.input_schema for tool in tools}
    assert set(schemas["web_search"]["properties"]) == {"query", "category"}
    assert set(schemas["web_fetch"]["properties"]) == {"url"}
    assert json.dumps(schemas).find("_provider") == -1


@pytest.mark.asyncio
async def test_web_factory_resolves_use_through_trusted_builder_registry():
    class ReplacementSearch:
        async def search(self, query):
            return {"provider": "replacement", "query": query}

    settings = RuntimeSettings(
        web_enabled=True,
        web_tools=[{
            "name": "web_search",
            "use": "trusted:test-search",
            "base_url": "https://unused.test",
            "max_results": 8,
        }],
    )
    factory = make_web_tool_factory(
        settings,
        provider_builders={
            "trusted:test-search": lambda item, current: ReplacementSearch(),
        },
    )
    tool = (await factory("user", "agent", "session"))[0]
    chunk = await tool.call(query="switch provider")
    assert json.loads(chunk.content[0].text) == {
        "provider": "replacement",
        "query": "switch provider",
    }
