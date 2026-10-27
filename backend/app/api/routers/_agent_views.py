# -*- coding: utf-8 -*-
"""Shared AgentOut builder (agents & conversations routers).

Caches the requesting user's resolved default agent and loaded scopes
per instance so listing many conversations stays cheap.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from workmate_product.persistence.models import (
    Agent,
    AgentCapabilityBinding,
    CapabilityInstallation,
    CapabilityType,
    Scope,
    ScopeKind,
)
from ...services.knowledge import KnowledgeService
from ...services.access_decision import AccessDecisionService
from ..schemas import AgentActions, AgentOut, AgentToolPolicy, ScopeOut


class AgentOutFactory:
    def __init__(
        self, db: AsyncSession, access: AccessDecisionService, user_id: str,
    ) -> None:
        self._db = db
        self._access = access
        self._user_id = user_id
        self._scopes: dict[str, Scope | None] = {}
        self._permissions: dict[str, set[str]] = {}
        self._bindings: dict[tuple[str, CapabilityType], list[str]] = {}
        self._unavailable: dict[str, list[str]] = {}
        self._can_use: dict[str, bool] = {}
        self._default_id: str | None = None
        self._default_done = False

    async def scope_of(self, agent: Agent) -> Scope | None:
        if agent.scope_id not in self._scopes:
            self._scopes[agent.scope_id] = await self._db.get(
                Scope, agent.scope_id,
            )
        return self._scopes[agent.scope_id]

    async def _default_agent_id(self) -> str | None:
        if not self._default_done:
            default = await self._access.resolve_default_agent(self._user_id)
            self._default_id = default.id if default else None
            self._default_done = True
        return self._default_id

    async def binding_ids(
        self, agent_id: str, capability_type: CapabilityType,
    ) -> list[str]:
        cached = self._bindings.get((agent_id, capability_type))
        if cached is not None:
            return cached
        rows = (
            await self._db.scalars(
                select(
                    AgentCapabilityBinding.capability_installation_id,
                ).join(
                    CapabilityInstallation,
                    CapabilityInstallation.id
                    == AgentCapabilityBinding.capability_installation_id,
                ).where(
                    AgentCapabilityBinding.agent_id == agent_id,
                    CapabilityInstallation.capability_type == capability_type,
                ),
            )
        ).all()
        return list(rows)

    async def prepare(self, agents: list[Agent]) -> None:
        """Batch Scope, permission, binding and availability projection."""
        if not agents:
            return
        agent_ids = {agent.id for agent in agents}
        scope_ids = {agent.scope_id for agent in agents}
        scopes = (
            await self._db.scalars(
                select(Scope).where(Scope.id.in_(scope_ids)),
            )
        ).all()
        self._scopes.update({scope.id: scope for scope in scopes})
        self._permissions.update(
            await self._access.permissions_for_scopes(
                self._user_id, scope_ids,
            ),
        )
        for agent in agents:
            for capability_type in CapabilityType:
                self._bindings[(agent.id, capability_type)] = []
            self._unavailable[agent.id] = []

        rows = (
            await self._db.execute(
                select(
                    AgentCapabilityBinding.agent_id,
                    CapabilityInstallation.id,
                    CapabilityInstallation.capability_type,
                    CapabilityInstallation.scope_id,
                    CapabilityInstallation.enabled,
                    Scope.kind,
                )
                .join(
                    CapabilityInstallation,
                    CapabilityInstallation.id
                    == AgentCapabilityBinding.capability_installation_id,
                )
                .join(Scope, Scope.id == CapabilityInstallation.scope_id)
                .where(AgentCapabilityBinding.agent_id.in_(agent_ids)),
            )
        ).all()
        for row in rows:
            self._bindings[(row.agent_id, row.capability_type)].append(row.id)
        self._unavailable.update(await self._access.unavailable_capabilities_for_agents(
            self._user_id, agents,
        ))
        shared_ids = await self._access.shared_agent_ids(self._user_id)
        for agent in agents:
            scope = self._scopes.get(agent.scope_id)
            self._can_use[agent.id] = bool(scope and agent.deleted_at is None and (
                scope.owner_user_id == self._user_id if scope.kind == ScopeKind.PERSONAL
                else (agent.created_by == self._user_id if agent.visibility == "private"
                      else agent.id in shared_ids)
            ))
        for values in self._bindings.values():
            values.sort()

    async def can_use(self, agent_id: str) -> bool:
        if agent_id not in self._can_use:
            self._can_use[agent_id] = await self._access.can_use_agent(
                self._user_id, agent_id,
            )
        return self._can_use[agent_id]

    async def unavailable_ids(self, agent_id: str) -> list[str]:
        if agent_id in self._unavailable:
            return self._unavailable[agent_id]
        return await self._access.unavailable_capability_ids(
            self._user_id, agent_id,
        )

    async def build(
        self, agent: Agent, *, can_use: bool | None = None, include_binding_issues: bool = False,
    ) -> AgentOut:
        scope = await self.scope_of(agent)
        if scope and scope.id not in self._permissions:
            self._permissions[scope.id] = await self._access.permissions_for_scope(
                self._user_id, agent.scope_id,
            )
        permissions = self._permissions.get(agent.scope_id, set())
        personal_owner = bool(
            scope
            and scope.kind == ScopeKind.PERSONAL
            and scope.owner_user_id == self._user_id
        )
        issues = None
        if include_binding_issues and (personal_owner or "agent.update" in permissions):
            from workmate_product.application.agent_binding_status import binding_issues
            from ...services.knowledge import knowledge_settings
            issues = await binding_issues(self._db, agent, self._access,
                knowledge_enabled=knowledge_settings().enabled)
        return AgentOut(
            binding_issues=issues,
            knowledge_config=agent.knowledge_config or {},
            unavailable_knowledge_base_ids=await KnowledgeService(self._db, self._user_id).unavailable(agent),
            id=agent.id,
            slug=agent.slug,
            display_name=agent.display_name,
            system_prompt=(agent.system_prompt if personal_owner or "agent.update" in permissions else ""),
            description=agent.description,
            tool_policy=AgentToolPolicy.model_validate(agent.tool_policy or {}),
            system_key=agent.system_key,
            platform_tool_profile=agent.platform_tool_profile,
            scope_id=agent.scope_id,
            scope=(
                ScopeOut(
                    id=scope.id,
                    kind=scope.kind,
                    name=scope.name,
                    parent_id=scope.parent_id,
                    permissions=sorted(permissions),
                )
                if scope else None
            ),
            visibility=agent.visibility,
            include_descendants=agent.include_descendants,
            created_by=agent.created_by,
            skill_binding_ids=await self.binding_ids(
                agent.id, CapabilityType.SKILL,
            ),
            mcp_binding_ids=await self.binding_ids(
                agent.id, CapabilityType.MCP,
            ),
            unavailable_capability_ids=await self.unavailable_ids(agent.id),
            actions=AgentActions(
                use=(
                    can_use
                    if can_use is not None
                    else await self.can_use(agent.id)
                ),
                update=(
                    personal_owner
                    or "agent.update" in permissions
                ),
                delete=(
                    agent.system_key is None
                    and (
                        personal_owner
                        or "agent.delete" in permissions
                    )
                ),
                publish=(
                    not personal_owner
                    and "agent.update" in permissions
                    and "agent.publish" in permissions
                ),
            ),
            is_default=(await self._default_agent_id()) == agent.id,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
        )
