# -*- coding: utf-8 -*-
"""Strict provider adapters for governed Web Search and Fetch tools."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def _resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    rows = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({row[4][0] for row in rows})


async def validate_public_url(url: str, resolver: Resolver = _resolve_host) -> str:
    """Reject non-HTTP and non-public targets before handing them to a provider."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public http/https URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("Target URLs cannot contain credentials")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await resolver(parsed.hostname, port)
    if not addresses:
        raise ValueError("Target hostname did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, and metadata targets are blocked")
    return parsed.geturl()


async def _read_json(response: httpx.Response, max_bytes: int) -> dict:
    response.raise_for_status()
    payload = bytearray()
    async for chunk in response.aiter_bytes():
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise ValueError("Web provider response exceeds configured byte limit")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Web provider returned a non-object response")
    return value


class SearxngSearchProvider:
    def __init__(
        self,
        *,
        base_url: str,
        max_results: int,
        max_response_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_results = max_results
        self.max_response_bytes = max_response_bytes
        self.transport = transport

    async def search(self, query: str, category: str = "web") -> dict:
        if category not in {"web", "images"}:
            raise ValueError("Search category must be web or images")
        query = query.strip()
        if not query or len(query) > 2000:
            raise ValueError("Search query must contain 1-2000 characters")
        async with httpx.AsyncClient(
            base_url=self.base_url,
            transport=self.transport,
            timeout=30,
        ) as client:
            async with client.stream(
                "GET",
                "/search",
                params={"q": query, "format": "json", "categories": "images" if category == "images" else "general"},
            ) as response:
                data = await _read_json(response, self.max_response_bytes)
        rows = data.get("results")
        if not isinstance(rows, list):
            raise ValueError("SearXNG response is missing results")
        results = []
        for row in rows:
            if len(results) >= self.max_results:
                break
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                continue
            parsed = urlsplit(row["url"].strip())
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                continue
            normalized = {
                "title": str(row.get("title") or row["url"]),
                "url": row["url"],
                "snippet": str(row.get("content") or ""),
                "source": "searxng",
            }
            if category == "images":
                image_url = row.get("img_src")
                if not _safe_image_link(image_url):
                    continue
                normalized["image_url"] = image_url
                thumbnail = row.get("thumbnail_src")
                normalized["thumbnail_url"] = thumbnail if _safe_image_link(thumbnail) else None
            results.append(normalized)
        return {"query": query, "category": category, "results": results, "source": "searxng"}


def _safe_image_link(value) -> bool:
    """Only return absolute HTTP links; no fetching or DNS safety claim."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname
                    and not parsed.username and not parsed.password)
    except ValueError:
        return False


class Crawl4AIFetchProvider:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        extract_mode: str,
        max_response_bytes: int,
        max_extracted_chars: int,
        resolver: Resolver = _resolve_host,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.extract_mode = extract_mode
        self.max_response_bytes = max_response_bytes
        self.max_extracted_chars = max_extracted_chars
        self.resolver = resolver
        self.transport = transport

    async def fetch(self, url: str) -> dict:
        target = await validate_public_url(url, self.resolver)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            transport=self.transport,
            timeout=self.timeout_seconds,
        ) as client:
            async with client.stream(
                "POST",
                "/crawl",
                json={"urls": [target]},
            ) as response:
                data = await _read_json(response, self.max_response_bytes)
        rows = data.get("results")
        result = rows[0] if isinstance(rows, list) and rows else data.get("result")
        if not isinstance(result, dict):
            raise ValueError("Crawl4AI response is missing a crawl result")
        final_url = str(result.get("url") or result.get("final_url") or target)
        await validate_public_url(final_url, self.resolver)
        redirects = result.get("redirect_chain") or []
        if not isinstance(redirects, list):
            raise ValueError("Crawl4AI redirect chain must be a list")
        for redirect in redirects:
            if not isinstance(redirect, str):
                raise ValueError("Crawl4AI redirect chain contains an invalid URL")
            await validate_public_url(redirect, self.resolver)
        markdown = result.get("markdown")
        if isinstance(markdown, dict):
            content = markdown.get(
                "fit_markdown" if self.extract_mode == "fit" else "raw_markdown",
            ) or ""
        else:
            content = markdown or result.get("content") or ""
        content = str(content)
        truncated = len(content) > self.max_extracted_chars
        content = content[: self.max_extracted_chars]
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        return {
            "url": final_url,
            "title": str(metadata.get("title") or result.get("title") or ""),
            "content": content,
            "truncated": truncated,
            "source": final_url,
        }
