"""Knowledge ownership and execution admission; Runtime owns indexing."""

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from ..adapter.errors import RuntimeError as RuntimeFailure
from ..api.knowledge_schemas import KnowledgeConfig
from ..api.pagination import page_result
from workmate_product.application import resource_lists
from ..config_file import KnowledgeFileConfig, load_file_config
from workmate_product.persistence.models import Agent, Scope, ScopeKind
from workmate_product.persistence.models import KnowledgeBase
from .access_decision import AccessDecisionService
from .bootstrap import get_personal_scope
from .capabilities import agent_runtime_principal
from .security_audit import SecurityAuditService

from workmate_product.application.knowledge_access import (
    KnowledgeBindingError, KnowledgeAccess,
    validate_knowledge_bindings, unavailable_knowledge_bindings,
)

logger = logging.getLogger(__name__)


def knowledge_settings() -> KnowledgeFileConfig:
    config = load_file_config()
    return config.product.knowledge if config else KnowledgeFileConfig()


class KnowledgeService:
    def __init__(self, db, user_id: str):
        self.db = db
        self.user_id = user_id
        self.access = AccessDecisionService(db)

    async def usable_ids(self):
        return await KnowledgeAccess(self.db).accessible_ids(self.user_id)

    async def downloadable_ids(self):
        return await KnowledgeAccess(self.db).accessible_ids(self.user_id, download=True)

    async def manageable(self, scope):
        if scope.kind == ScopeKind.PERSONAL:
            return scope.owner_user_id == self.user_id
        return await self.access.has_permission(
            self.user_id, scope.id, "knowledge.manage"
        )

    async def bindable_ids(self, scope):
        return await KnowledgeAccess(self.db).bindable_ids(scope)

    async def get(self, kb_id, *, manage=False, use=False, download=False):
        kb = await self.db.get(KnowledgeBase, kb_id)
        if kb is None:
            raise HTTPException(404, "Knowledge base not found")
        scope = await self.db.get(Scope, kb.scope_id)
        usable = kb.id in await self.usable_ids()
        can_manage = await self.manageable(scope)
        if not usable and not can_manage:
            raise HTTPException(404, "Knowledge base not found")
        if manage and not can_manage:
            raise HTTPException(403, "knowledge.manage permission required")
        if use and (not usable or not kb.enabled):
            raise HTTPException(403, "Knowledge base is not available for use")
        if download and (not kb.enabled or kb.id not in await self.downloadable_ids()):
            raise HTTPException(403, "Knowledge original download permission required")
        return kb

    async def view(self, kb):
        scope = await self.db.get(Scope, kb.scope_id)
        return {
            "id": kb.id,
            "name": kb.name,
            "description": kb.description,
            "scope_id": kb.scope_id,
            "scope": {
                "id": scope.id,
                "name": scope.name,
                "kind": getattr(scope.kind, "value", scope.kind),
            },
            "enabled": kb.enabled,
            "actions": {
                "manage": await self.manageable(scope),
                "use": kb.enabled and kb.id in await self.usable_ids(),
                "download": kb.enabled and kb.id in await self.downloadable_ids(),
            },
            "created_at": kb.created_at,
            "updated_at": kb.updated_at,
        }

    async def list(self, agent_scope_id=None, *, page=1, page_size=12, q="", view="all"):
        allowed = None
        if agent_scope_id:
            accessible = await self.access.accessible_scope_ids(self.user_id)
            scope = await self.db.get(Scope, agent_scope_id)
            if scope is None or scope.id not in accessible:
                raise HTTPException(404, "Scope not found")
            allowed = await self.bindable_ids(scope)
        rows, total = await resource_lists.knowledge(
            self.db, self.access, self.user_id, page=page, page_size=page_size,
            q=q, view=view, allowed=allowed,
        )
        items = [await self.view(kb) for kb in rows]
        return page_result(items, total, page, page_size)

    async def validate_bindings(self, agent, config, *, require_user_access=True):
        try:
            return await validate_knowledge_bindings(self.db, self.user_id, agent, config,
                enabled=knowledge_settings().enabled, access=self.access,
                require_user_access=require_user_access)
        except KnowledgeBindingError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    async def unavailable(self, agent):
        return await unavailable_knowledge_bindings(self.db, self.user_id, agent,
            enabled=knowledge_settings().enabled, access=self.access)

    def audit(self, operation, kb, **metadata):
        SecurityAuditService(self.db).record(
            "knowledge." + operation,
            actor_user_id=self.user_id,
            scope_id=kb.scope_id,
            metadata={"knowledge_base_id": kb.id, **metadata},
        )

    async def create(self, body, client):
        if not knowledge_settings().enabled:
            raise HTTPException(503, "Knowledge service is disabled")
        scope = (
            await self.db.get(Scope, body.scope_id)
            if body.scope_id
            else await get_personal_scope(self.db, self.user_id)
        )
        if scope is None:
            raise HTTPException(404, "Scope not found")
        if not await self.manageable(scope):
            raise HTTPException(403, "knowledge.manage permission required")
        owner = agent_runtime_principal(scope)
        record = await runtime_call(
            client.knowledge_request(
                "POST", owner, body={"name": body.name, "description": body.description}
            )
        )
        kb = KnowledgeBase(
            id=str(uuid.uuid4()),
            scope_id=scope.id,
            name=body.name,
            description=body.description,
            runtime_id=record["id"],
            runtime_owner_id=owner,
            enabled=True,
            created_by=self.user_id,
        )
        try:
            self.db.add(kb)
            self.audit("create", kb)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            try:
                await client.knowledge_request("DELETE", owner, kb.runtime_id)
            except Exception:
                logger.exception("Knowledge creation compensation failed")
            raise HTTPException(502, "Knowledge base creation could not be completed")
        return await self.view(kb)

    async def has_bindings(self, kb_id):
        agents = (
            await self.db.scalars(select(Agent).where(Agent.deleted_at.is_(None)))
        ).all()
        return any(
            kb_id in (agent.knowledge_config or {}).get("knowledge_base_ids", [])
            for agent in agents
        )


async def runtime_call(awaitable):
    try:
        return await awaitable
    except RuntimeFailure as exc:
        status = (
            exc.status_code
            if exc.status_code in (404, 409, 413, 422, 429, 503)
            else 502
        )
        raise HTTPException(
            status,
            "Knowledge operation failed; check the document state or retry later",
        ) from exc
