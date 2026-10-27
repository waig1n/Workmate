# -*- coding: utf-8 -*-
"""Sanitized append-only security audit event creation."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from workmate_product.persistence.models import SecurityAuditEvent

_FORBIDDEN_FRAGMENTS = (
    "password", "hash", "token", "secret", "authorization",
    "credential", "mcp_config",
)


def _sanitize(metadata: dict | None) -> dict:
    clean = {}
    for key, value in (metadata or {}).items():
        lowered = str(key).casefold()
        if any(fragment in lowered for fragment in _FORBIDDEN_FRAGMENTS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[str(key)] = value
        elif isinstance(value, list) and all(
            isinstance(item, (str, int, float, bool)) or item is None
            for item in value
        ):
            clean[str(key)] = value
    return clean


class SecurityAuditService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    def record(
        self, event_type: str, *, actor_user_id: str | None = None,
        target_user_id: str | None = None, scope_id: str | None = None,
        request_id: str | None = None, metadata: dict | None = None,
    ) -> SecurityAuditEvent:
        event = SecurityAuditEvent(
            id=str(uuid.uuid4()), event_type=event_type,
            actor_user_id=actor_user_id, target_user_id=target_user_id,
            scope_id=scope_id, request_id=request_id,
            metadata_=_sanitize(metadata),
        )
        self._db.add(event)
        return event
