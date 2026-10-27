"""Configuration diagnostics, independent of the requesting manager's use rights.

Caller must authorize Agent management before exposing this view.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config.knowledge import KnowledgeConfig
from ..persistence.models import (
    Agent,
    AgentCapabilityBinding,
    CapabilityInstallation,
    CapabilityMCPRevision,
    CapabilitySkillArtifact,
    KnowledgeBase,
    Scope,
)
from .authorization import AccessDecisionService
from .knowledge_access import KnowledgeAccess


async def binding_issues(
    db: AsyncSession, agent: Agent, access: AccessDecisionService, *, knowledge_enabled: bool,
) -> list[dict[str, str]]:
    scope = await db.get(Scope, agent.scope_id)
    allowed = await access.bindable_capability_ids(scope) if scope else set()
    rows = (await db.scalars(
        select(CapabilityInstallation)
        .join(AgentCapabilityBinding,
              AgentCapabilityBinding.capability_installation_id == CapabilityInstallation.id)
        .where(AgentCapabilityBinding.agent_id == agent.id)
    )).all()
    identifiers = [row.id for row in rows]
    skill_ready = set(await db.scalars(
        select(CapabilitySkillArtifact.capability_id)
        .where(CapabilitySkillArtifact.capability_id.in_(identifiers))
    ))
    mcp_ready = set(await db.scalars(
        select(CapabilityMCPRevision.capability_id)
        .where(CapabilityMCPRevision.capability_id.in_(identifiers))
    ))
    issues = []
    for row in rows:
        reason = None
        if not row.enabled:
            reason = 'disabled'
        elif row.id not in allowed:
            reason = 'unshared'
        elif row.capability_type == 'skill' and row.id not in skill_ready:
            reason = 'unready'
        elif row.capability_type == 'mcp' and row.id not in mcp_ready:
            reason = 'unready'
        if reason:
            issues.append({'id': row.id, 'kind': row.capability_type, 'reason': reason})

    config = KnowledgeConfig.model_validate(agent.knowledge_config or {})
    if not config.knowledge_base_ids:
        return issues
    allowed_kb = await KnowledgeAccess(db).bindable_ids(scope) if scope else set()
    libraries = {kb.id: kb for kb in await db.scalars(
        select(KnowledgeBase).where(KnowledgeBase.id.in_(config.knowledge_base_ids))
    )}
    for identifier in config.knowledge_base_ids:
        kb = libraries.get(identifier)
        reason = None
        if kb is None:
            reason = 'missing'
        elif not kb.enabled:
            reason = 'disabled'
        elif not knowledge_enabled:
            reason = 'service_disabled'
        elif identifier not in allowed_kb:
            reason = 'unshared'
        if reason:
            issues.append({'id': identifier, 'kind': 'knowledge', 'reason': reason})
    return issues
