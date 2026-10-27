# -*- coding: utf-8 -*-
"""AgentScope FunctionTools backed by configured Web providers."""

from agentscope.tool import FunctionTool
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from .config import RuntimeSettings
from .web_providers import Crawl4AIFetchProvider, SearxngSearchProvider

SEARCH_PROVIDER = "workmate_runtime.web_providers:SearxngSearchProvider"
FETCH_PROVIDER = "workmate_runtime.web_providers:Crawl4AIFetchProvider"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WebSearchInput(_StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    category: Literal["web", "images"] = Field(
        default="web",
        description="Use images when the user asks to find photos, pictures, wallpapers or illustrations; web searches pages and cannot provide image previews.",
    )


class WebFetchInput(_StrictModel):
    url: str = Field(min_length=1, max_length=8192)


def _search_tool(provider: SearxngSearchProvider) -> FunctionTool:
    async def web_search(query: str, category: Literal["web", "images"] = "web") -> dict:
        """Search web pages or images. Image results include source pages;
        do not assume reuse rights or download images without user intent.
        """
        if category == "web":
            return await provider.search(query)
        return await provider.search(query, category=category)

    return FunctionTool(
        web_search,
        name="web_search",
        input_schema=WebSearchInput,
        is_read_only=True,
    )


def _fetch_tool(provider: Crawl4AIFetchProvider) -> FunctionTool:
    async def web_fetch(url: str) -> dict:
        """Fetch and extract one public web page with its source URL."""
        return await provider.fetch(url)

    return FunctionTool(
        web_fetch,
        name="web_fetch",
        input_schema=WebFetchInput,
        is_read_only=True,
    )


def _build_searxng(item: dict, settings: RuntimeSettings):
    return SearxngSearchProvider(
        base_url=item["base_url"],
        max_results=item["max_results"],
        max_response_bytes=settings.web_max_response_bytes,
    )


def _build_crawl4ai(item: dict, settings: RuntimeSettings):
    return Crawl4AIFetchProvider(
        base_url=item["base_url"],
        token=item.get("token", ""),
        timeout_seconds=item["timeout_seconds"],
        extract_mode=item["extract_mode"],
        max_response_bytes=settings.web_max_response_bytes,
        max_extracted_chars=settings.web_max_extracted_chars,
    )


TRUSTED_PROVIDER_BUILDERS = {
    SEARCH_PROVIDER: _build_searxng,
    FETCH_PROVIDER: _build_crawl4ai,
}


def make_web_tool_factory(
    settings: RuntimeSettings,
    *,
    provider_builders=None,
):
    builders = provider_builders or TRUSTED_PROVIDER_BUILDERS
    providers = []
    if settings.web_enabled:
        for item in settings.web_tools:
            try:
                builder = builders[item["use"]]
            except KeyError as exc:
                raise ValueError(
                    f"Unregistered Web provider: {item['use']}",
                ) from exc
            providers.append((item["name"], builder(item, settings)))

    async def factory(user_id: str, agent_id: str, session_id: str):
        del user_id, agent_id, session_id
        tools = []
        for name, provider in providers:
            if name == "web_search":
                tools.append(_search_tool(provider))
            else:
                tools.append(_fetch_tool(provider))
        return tools

    return factory
