"""Shared Agent mutation primitives, preserving Product binding/default semantics."""
from sqlalchemy import select,func,update,delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from ..persistence.models import Agent,AgentCapabilityBinding,AgentDefault,Scope,CapabilityInstallation,CapabilityType
from .authorization import AccessDecisionService
from .platform_types import PlatformToolError as HTTPException
async def _lock_scope_ids(
    db: AsyncSession, scope_ids: set[str] | list[str],
) -> None:
    """Lock product scopes in the global mutation order."""
    ordered_ids = sorted(set(scope_ids))
    if not ordered_ids:
        return
    await db.execute(
        select(Scope.id)
        .where(Scope.id.in_(ordered_ids))
        .order_by(Scope.id)
        .with_for_update(),
    )

async def _lock_installations(
    db: AsyncSession, installation_ids: set[str] | list[str],
) -> dict[str, CapabilityInstallation]:
    """Lock capability rows after all participating scope rows."""
    ordered_ids = sorted(set(installation_ids))
    if not ordered_ids:
        return {}
    rows = (
        await db.scalars(
            select(CapabilityInstallation)
            .where(CapabilityInstallation.id.in_(ordered_ids))
            .order_by(CapabilityInstallation.id)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
    ).all()
    return {row.id: row for row in rows}

async def _apply_bindings(
    db: AsyncSession,
    access: AccessDecisionService,
    agent: Agent,
    skill_ids: list[str] | None,
    mcp_ids: list[str] | None,
) -> None:
    """Validate and replace the agent's capability bindings (RBAC §8).

    Personal agents may bind enabled capabilities available through
    their owner's explicit Skill grants or membership-derived MCP access.
    Organization agents may bind explicitly shared Skills and own/ancestor MCPs.
    Only explicitly supplied binding types are replaced."""
    scope = await db.get(Scope, agent.scope_id)
    allowed = await access.bindable_capability_ids(scope)

    submitted: list[tuple[CapabilityType, list[str]]] = []
    if skill_ids is not None:
        submitted.append((CapabilityType.SKILL, list(dict.fromkeys(skill_ids))))
    if mcp_ids is not None:
        submitted.append((CapabilityType.MCP, list(dict.fromkeys(mcp_ids))))
    desired = [
        (capability_type, install_id)
        for capability_type, install_ids in submitted
        for install_id in install_ids
    ]

    desired_ids = sorted({install_id for _, install_id in desired})
    # Discover immutable scope ownership without taking installation locks,
    # then follow the global order used by capability mutations: all scopes
    # (stable id order), followed by all installations (stable id order).
    discovered = []
    if desired_ids:
        discovered = (
            await db.scalars(
                select(CapabilityInstallation).where(
                    CapabilityInstallation.id.in_(desired_ids),
                ),
            )
        ).all()
    await _lock_scope_ids(
        db,
        {agent.scope_id}
        | {installation.scope_id for installation in discovered},
    )
    installations = await _lock_installations(db, desired_ids)

    for expected_type, install_id in desired:
        installation = installations.get(install_id)
        if installation is None:
            raise HTTPException(
                status_code=422,
                detail=f"Capability not found: {install_id}",
            )
        if installation.capability_type != expected_type:
            raise HTTPException(
                status_code=422,
                detail=f"Capability {install_id} is a "
                f"{getattr(installation.capability_type, 'value', installation.capability_type)}, expected "
                f"{expected_type.value}",
            )
        if not installation.enabled:
            raise HTTPException(
                status_code=422,
                detail=f"Capability is disabled: {installation.name}",
            )
        if installation.id not in allowed:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Capability {installation.name} belongs to a scope "
                    "that cannot be bound to this agent"
                ),
            )

    for capability_type, install_ids in submitted:
        by_name: dict[str, str] = {}
        for install_id in install_ids:
            installation = installations[install_id]
            existing_id = by_name.get(installation.name)
            if existing_id is not None and existing_id != install_id:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Multiple {capability_type.value} capabilities "
                        f"with the same name cannot be bound: "
                        f"{installation.name}"
                    ),
                )
            by_name[installation.name] = install_id

    # Native bindings must reject sources not yet executable, before mutation.
    from .skill_repository import ready_skill_artifacts
    from .skill_artifacts import SkillArtifactError
    try:
        await ready_skill_artifacts(db, installations.values())
    except SkillArtifactError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if any(row.capability_type == CapabilityType.MCP for row in installations.values()):
        from ..mcp.repository import ready_mcp_revisions
        from ..mcp.definitions import MCPDefinitionError
        try:
            await ready_mcp_revisions(db, installations.values())
        except MCPDefinitionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    for capability_type, install_ids in submitted:
        type_installations = select(CapabilityInstallation.id).where(
            CapabilityInstallation.capability_type == capability_type,
        )
        await db.execute(
            AgentCapabilityBinding.__table__.delete().where(
                AgentCapabilityBinding.agent_id == agent.id,
                AgentCapabilityBinding.capability_installation_id.in_(
                    type_installations,
                ),
            ),
        )
        for install_id in install_ids:
            db.add(AgentCapabilityBinding(
                agent_id=agent.id,
                capability_installation_id=install_id,
            ))
    await db.flush()

async def _upsert_default(
    db: AsyncSession, scope_id: str, agent_id: str, user_id: str,
) -> None:
    # Serialize default changes per scope, then use a native upsert so
    # concurrent first-default requests cannot race on the primary key.
    await db.execute(
        select(Scope.id).where(Scope.id == scope_id).with_for_update(),
    )
    values = {
        "scope_id": scope_id,
        "agent_id": agent_id,
        "updated_by": user_id,
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(AgentDefault).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[AgentDefault.scope_id],
            set_={
                "agent_id": statement.excluded.agent_id,
                "updated_by": statement.excluded.updated_by,
                "updated_at": func.now(),
            },
        )
        await db.execute(statement)
    elif dialect == "sqlite":
        statement = sqlite_insert(AgentDefault).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[AgentDefault.scope_id],
            set_={
                "agent_id": statement.excluded.agent_id,
                "updated_by": statement.excluded.updated_by,
                "updated_at": func.now(),
            },
        )
        await db.execute(statement)
    else:
        result = await db.execute(
            update(AgentDefault)
            .where(AgentDefault.scope_id == scope_id)
            .values(agent_id=agent_id, updated_by=user_id),
        )
        if result.rowcount == 0:
            # The scope lock makes this fallback serialized even on a
            # dialect without native ON CONFLICT support.
            db.add(AgentDefault(**values))

async def _clear_default(
    db: AsyncSession, scope_id: str, expected_agent_id: str,
) -> None:
    """Clear only this Agent's default while serialized by its Scope row."""
    await _lock_scope_ids(db, {scope_id})
    await db.execute(
        delete(AgentDefault).where(
            AgentDefault.scope_id == scope_id,
            AgentDefault.agent_id == expected_agent_id,
        ),
    )
