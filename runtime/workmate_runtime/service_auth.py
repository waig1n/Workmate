# -*- coding: utf-8 -*-
"""Service-to-service authentication for the complete Runtime API."""

import secrets

from starlette.responses import JSONResponse


class RuntimeServiceAuthMiddleware:
    """Reject non-health Runtime traffic without the shared service token.

    This guard wraps AgentScope's stock routes as well as workmate extensions.
    It is required even on an internal network because Agent sandboxes share
    the Runtime network namespace to reach their per-workspace MCP gateway.
    """

    def __init__(self, app, *, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return
        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
        }
        supplied = headers.get(b"x-workmate-internal-token", b"").decode(
            "utf-8",
            errors="replace",
        )
        if not self.token or not secrets.compare_digest(
            supplied,
            self.token,
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
            else:
                response = JSONResponse(
                    {"detail": "Runtime service authentication failed"},
                    status_code=403,
                )
                await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
