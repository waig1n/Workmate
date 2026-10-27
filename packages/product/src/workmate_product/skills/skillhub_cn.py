# -*- coding: utf-8 -*-
"""Product-owned bounded HTTP provider for the public SkillHub.cn registry."""

from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import AsyncExitStack
from typing import Any, AsyncIterator

import httpx
import yaml
from .catalog import SkillCard, SkillHubPage, SkillCatalogError
from ..application.skill_artifacts import MAX_ARCHIVE_BYTES


class HubError(SkillCatalogError):
    def __init__(self, hub_id, status_code, detail):
        self.hub_id = hub_id
        super().__init__(status_code, detail)


_CHUNK_SIZE = 64 * 1024
_CARD_ID_PREFIX = "shcn_"


class SkillHubCN:
    """Lifecycle-owned provider with bounded downloaded archive bytes."""
    def __init__(self, *, base_url="https://api.skillhub.cn", timeout=30.0, max_retries=2):
        self.hub_id = "skillhub-cn"
        self.display_name = "SkillHub 中国"
        self.description = "面向中国用户的公共 Skills 社区。"
        self.icon_url = "https://www.skillhub.cn/favicon.ico"
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SkillHubCN":
        # Downloads are redirected to SkillHub's object storage.
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            response = await self._http().get(
                f"{self.base_url}{path}",
                params=params,
                headers={"Accept": "application/json"},
            )
            if response.status_code in {429, 502, 503, 504} and (
                attempt < self.max_retries
            ):
                await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
                continue
            if response.status_code >= 400:
                raise HubError(
                    self.hub_id,
                    response.status_code,
                    response.text,
                )
            return response
        raise HubError(self.hub_id, 503, "upstream retry limit exceeded")

    @staticmethod
    def _split_card_id(card_id: str) -> tuple[str | None, str]:
        if card_id.startswith(_CARD_ID_PREFIX):
            encoded = card_id.removeprefix(_CARD_ID_PREFIX)
            padding = "=" * (-len(encoded) % 4)
            try:
                raw = base64.urlsafe_b64decode(encoded + padding).decode()
            except (binascii.Error, UnicodeDecodeError):
                return None, card_id
            namespace, separator, slug = raw.partition("\0")
            if separator and namespace and slug:
                return namespace, slug
            return None, card_id
        if "/" in card_id:
            raise KeyError(card_id)
        return None, card_id

    @staticmethod
    def _card_id(namespace: str, slug: str) -> str:
        if not namespace:
            return slug
        raw = f"{namespace}\0{slug}".encode()
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"{_CARD_ID_PREFIX}{encoded}"

    @staticmethod
    def _tags(item: dict[str, Any]) -> list[str]:
        values: list[str] = []
        category = item.get("category")
        if isinstance(category, str) and category:
            values.append(category)
        for entry in item.get("subCategories") or []:
            if isinstance(entry, str):
                value = entry
            elif isinstance(entry, dict):
                value = str(
                    entry.get("name_zh")
                    or entry.get("name")
                    or entry.get("slug")
                    or "",
                )
            else:
                value = ""
            if value and value not in values:
                values.append(value)
        return values

    def _to_card(self, item: dict[str, Any]) -> SkillCard:
        slug = str(item["slug"])
        namespace = item.get("namespace")
        namespace = namespace if isinstance(namespace, dict) else {}
        handle = str(namespace.get("handle") or "").strip()
        updated_at = item.get("updated_at") or item.get("updatedAt")
        if isinstance(updated_at, (int, float)) and updated_at > 10_000_000_000:
            updated_at = float(updated_at) / 1000.0

        display_name = (
            item.get("name")
            or namespace.get("publicSlug")
            or item.get("displayName")
            or slug
        )
        author = (
            namespace.get("displayName")
            or item.get("ownerName")
            or item.get("author")
        )
        web_path = f"{handle}/{slug}" if handle else slug
        return SkillCard(
            hub_id=self.hub_id,
            id=self._card_id(handle, slug),
            name=slug,
            display_name=str(display_name),
            description=str(
                item.get("description_zh") or item.get("description") or "",
            ),
            tags=self._tags(item),
            version=str(item["version"]) if item.get("version") else None,
            updated_at=float(updated_at) if updated_at else None,
            author=str(author) if author else None,
            icon_url=item.get("iconUrl") or item.get("icon_url"),
            installs=item.get("installs"),
            downloads=item.get("downloads"),
            url=f"https://www.skillhub.cn/skills/{web_path}",
            metadata={
                key: item[key]
                for key in ("source", "stars", "labels", "verified")
                if item.get(key) is not None
            },
        )

    @staticmethod
    def _payload(response: httpx.Response, hub_id: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise HubError(hub_id, 502, "SkillHub returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HubError(hub_id, 502, "SkillHub returned an invalid payload")
        if payload.get("code", 0) not in (0, None):
            raise HubError(
                hub_id,
                502,
                str(payload.get("message") or "SkillHub request failed"),
            )
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise HubError(hub_id, 502, "SkillHub returned invalid data")
        return data

    async def list_skills(
        self,
        user_id: str,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SkillHubPage:
        del user_id
        try:
            page = max(1, int(cursor or "1"))
        except ValueError as exc:
            raise HubError(self.hub_id, 400, "invalid page cursor") from exc
        page_size = min(max(limit, 1), 100)
        params: dict[str, Any] = {
            "page": page,
            "pageSize": page_size,
            "sortBy": "downloads",
            "order": "desc",
        }
        if q:
            params["keyword"] = q
        data = self._payload(await self._request("/api/skills", params), self.hub_id)
        records = data.get("skills") or []
        total = int(data.get("total") or 0)
        next_cursor = str(page + 1) if page * page_size < total else None
        return SkillHubPage(
            cards=[
                self._to_card(item)
                for item in records
                if isinstance(item, dict) and item.get("slug")
            ],
            next_cursor=next_cursor,
        )

    async def get_skill(self, user_id: str, card_id: str) -> SkillCard:
        del user_id
        namespace, slug = self._split_card_id(card_id)
        params = {"namespace": namespace} if namespace else {}
        try:
            detail_response = await self._request(
                f"/api/v1/skills/{slug}",
                params,
            )
            data = self._payload(detail_response, self.hub_id)
            item = dict(data.get("skill") or {})
            item.setdefault("slug", slug)
            item["namespace"] = data.get("namespace") or item.get("namespace")
            latest = data.get("latestVersion")
            if isinstance(latest, dict):
                item["version"] = latest.get("version") or item.get("version")
            item["ownerName"] = (
                (data.get("owner") or {}).get("displayName")
                if isinstance(data.get("owner"), dict)
                else item.get("ownerName")
            )
            version = item.get("version")
            file_params: dict[str, Any] = {"path": "SKILL.md", **params}
            if version:
                file_params["version"] = version
            markdown_response = await self._request(
                f"/api/v1/skills/{slug}/file",
                file_params,
            )
        except HubError as exc:
            if exc.status_code == 404:
                raise KeyError(card_id) from exc
            raise

        card = self._to_card(item)
        card.markdown = _markdown_body(markdown_response.text)
        return card

    async def archive(
        self,
        user_id: str,
        card_id: str,
        version: str | None = None,
    ) -> bytes:
        del user_id
        namespace, slug = self._split_card_id(card_id)
        params: dict[str, Any] = {"slug": slug}
        if namespace:
            params["namespace"] = namespace
        if version:
            params["version"] = version

        stack = AsyncExitStack()
        try:
            response = await stack.enter_async_context(
                self._http().stream(
                    "GET",
                    f"{self.base_url}/api/v1/download",
                    params=params,
                    headers={"Accept": "application/zip"},
                ),
            )
            if response.status_code >= 400:
                body = await response.aread()
                if response.status_code == 404:
                    raise KeyError(card_id)
                raise HubError(
                    self.hub_id,
                    response.status_code,
                    body.decode("utf-8", errors="replace"),
                )
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise HubError(self.hub_id, 413, "Skill archive exceeds 20 MiB")
                chunks.append(chunk)
            await stack.aclose()
            return b"".join(chunks)
        except BaseException:
            await stack.aclose()
            raise



def _markdown_body(markdown: str) -> str:
    """Remove optional YAML front matter, matching AgentScope's ClawHub."""
    if not markdown.startswith("---"):
        return markdown
    parts = markdown.split("---", 2)
    if len(parts) != 3:
        return markdown
    try:
        metadata = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return markdown
    return parts[2].lstrip("\r\n") if isinstance(metadata, dict) else markdown
