# -*- coding: utf-8 -*-
"""SkillHub.cn AgentScope adapter contract tests."""

import json

import httpx
import pytest

from workmate_product.skills.skillhub_cn import SkillHubCN


def _response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode(),
    )


@pytest.mark.asyncio
async def test_list_skills_maps_namespace_and_pagination():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/skills"
        assert request.url.params["keyword"] == "memory"
        assert request.url.params["page"] == "2"
        return _response({
            "code": 0,
            "message": "success",
            "data": {
                "total": 5,
                "skills": [{
                    "slug": "self-improving-agent",
                    "name": "Self Improving Agent",
                    "description": "English description",
                    "description_zh": "中文说明",
                    "namespace": {
                        "handle": "clawhub_pskoett",
                        "displayName": "pskoett",
                    },
                    "version": "3.0.24",
                    "downloads": 123,
                    "installs": 45,
                    "updated_at": 1_700_000_000_000,
                    "subCategories": [{"name_zh": "智能体"}],
                }],
            },
        })

    hub = SkillHubCN()
    hub._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        page = await hub.list_skills("user", q="memory", cursor="2", limit=2)
    finally:
        await hub.__aexit__()

    assert page.next_cursor == "3"
    assert len(page.cards) == 1
    card = page.cards[0]
    assert card.id == (
        "shcn_Y2xhd2h1Yl9wc2tvZXR0AHNlbGYtaW1wcm92aW5nLWFnZW50"
    )
    assert "/" not in card.id
    assert card.name == "self-improving-agent"
    assert card.description == "中文说明"
    assert card.tags == ["智能体"]
    assert card.updated_at == 1_700_000_000


@pytest.mark.asyncio
async def test_get_skill_fetches_detail_and_markdown():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["namespace"] == "company"
        if request.url.path.endswith("/file"):
            assert request.url.params["path"] == "SKILL.md"
            assert request.url.params["version"] == "1.2.3"
            return httpx.Response(
                200,
                text="---\nname: audit\ndescription: audit skill\n---\n# Audit\n",
            )
        return _response({
            "skill": {
                "slug": "audit",
                "name": "Audit",
                "description_zh": "审计 Skill",
            },
            "latestVersion": {"version": "1.2.3"},
            "namespace": {"handle": "company", "displayName": "Company"},
        })

    hub = SkillHubCN()
    hub._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        card = await hub.get_skill("user", "shcn_Y29tcGFueQBhdWRpdA")
    finally:
        await hub.__aexit__()

    assert card.id == "shcn_Y29tcGFueQBhdWRpdA"
    assert card.version == "1.2.3"
    assert card.markdown == "# Audit\n"


@pytest.mark.asyncio
async def test_download_streams_skillhub_zip_with_identity_params():
    archive_bytes = b"PK\x03\x04skill-archive"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/archive.zip":
            return httpx.Response(200, content=archive_bytes)
        assert request.url.path == "/api/v1/download"
        assert request.url.params["slug"] == "audit"
        assert request.url.params["namespace"] == "company"
        assert request.url.params["version"] == "1.2.3"
        return httpx.Response(302, headers={"location": "/archive.zip"})

    hub = SkillHubCN()
    hub._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    try:
        archive = await hub.archive(
            "user", "shcn_Y29tcGFueQBhdWRpdA", "1.2.3",
        )
        received = archive
    finally:
        await hub.__aexit__()

    assert received == archive_bytes


@pytest.mark.asyncio
async def test_get_skill_maps_upstream_404_to_key_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    hub = SkillHubCN()
    hub._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(KeyError):
            await hub.get_skill("user", hub._card_id("company", "missing"))
    finally:
        await hub.__aexit__()

@pytest.mark.asyncio
async def test_download_limit_closes_response_and_lifecycle_client(monkeypatch):
    import workmate_product.skills.skillhub_cn as module
    monkeypatch.setattr(module,'MAX_ARCHIVE_BYTES',4)
    class Stream(httpx.AsyncByteStream):
        closed=False
        async def __aiter__(self):
            yield b'123'
            yield b'456'
        async def aclose(self):
            self.closed=True
    stream=Stream()
    async def handler(request):
        return httpx.Response(200,stream=stream)
    hub=SkillHubCN()
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    hub._client=client
    with pytest.raises(module.HubError) as failure:
        await hub.archive('u','sample')
    assert failure.value.status_code==413 and stream.closed
    await hub.__aexit__()
    assert client.is_closed

@pytest.mark.asyncio
async def test_provider_lifecycle_closes_enabled_catalog():
    from workmate_product.skills.lifecycle import skill_catalogs
    async with skill_catalogs(skillhub_cn_enabled=True) as catalogs:
        provider=catalogs['skillhub-cn']
        client=provider._client
        assert not client.is_closed
    assert client.is_closed and provider._client is None


def test_card_ids_require_canonical_namespace_encoding_but_allow_bare_slugs():
    assert SkillHubCN._split_card_id('audit') == (None, 'audit')
    assert SkillHubCN._split_card_id('shcn_Y29tcGFueQBhdWRpdA') == ('company', 'audit')
    with pytest.raises(KeyError):
        SkillHubCN._split_card_id('company/audit')
