"""Service-authenticated knowledge transport, shared by product API and dispatch."""

from urllib.parse import quote

import httpx


def segment(value):
    return quote(str(value), safe="")


class KnowledgeClientMixin:
    async def knowledge_request(
        self, method, owner, kb_id=None, *, suffix="", body=None, params=None
    ):
        path = "/workmate/knowledge" + ("/" + segment(kb_id) if kb_id else "") + suffix
        return await self._request(method, path, owner, json_body=body, params=params)

    async def knowledge_upload(self, owner, kb_id, filename, content_type, content, document_id):
        from .errors import RuntimeError

        try:
            async with self._client(owner) as client:
                response = await client.put(
                    f"/workmate/knowledge/{segment(kb_id)}/staged/{segment(document_id)}",
                    files={"file": (filename, content, content_type)},
                )
                if response.is_error:
                    raise RuntimeError(response.status_code, "Knowledge upload failed")
                try:
                    return response.json()
                except ValueError as error:
                    raise RuntimeError(502, "Knowledge service response is invalid") from error
        except httpx.HTTPError as exc:
            raise RuntimeError(503, "Knowledge service unavailable") from exc

    async def knowledge_content(self, owner, kb_id, document_id, max_bytes):
        from .errors import RuntimeError

        path = (
            f"/workmate/knowledge/{segment(kb_id)}/documents/{segment(document_id)}/content"
        )
        try:
            async with self._client(owner) as client:
                async with client.stream("GET", path) as response:
                    if response.is_error:
                        raise RuntimeError(
                            response.status_code, "Knowledge document unavailable"
                        )
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise RuntimeError(413, "Document exceeds configured limit")
                        chunks.append(chunk)
                    return (
                        b"".join(chunks),
                        response.headers.get(
                            "content-type", "application/octet-stream"
                        ),
                        response.headers.get("content-disposition", "attachment"),
                    )
        except httpx.HTTPError as exc:
            raise RuntimeError(503, "Knowledge service unavailable") from exc


class KnowledgeServiceClient(KnowledgeClientMixin):
    """Knowledge-only transport to the native Worker; no Session/App controls."""
    def __init__(self, base_url, token, *, timeout=120, transport=None):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.transport = transport

    def _client(self, owner):
        from .errors import RuntimeError
        if not self.token:
            raise RuntimeError(503, 'Knowledge service authentication is not configured')
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, transport=self.transport,
            headers={'X-User-ID':owner, 'X-Workmate-Internal-Token':self.token})

    async def _request(self, method, path, owner, *, json_body=None, params=None):
        from .errors import RuntimeError
        try:
            async with self._client(owner) as client:
                response = await client.request(method, path, json=json_body, params=params)
                if response.is_error:
                    raise RuntimeError(response.status_code, 'Knowledge service request failed')
                if response.status_code == 204 or not response.content:
                    return None
                try:
                    return response.json()
                except ValueError as error:
                    raise RuntimeError(502, 'Knowledge service response is invalid') from error
        except httpx.HTTPError as error:
            raise RuntimeError(503, 'Knowledge service unavailable') from error
