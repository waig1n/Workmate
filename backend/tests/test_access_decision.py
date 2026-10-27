# -*- coding: utf-8 -*-
"""AccessDecisionService permission-matrix tests (RBAC design §5/§7)."""

import uuid

import pytest
from sqlalchemy import delete, select

from app.api.routers._agent_views import AgentOutFactory
from app.auth.permissions import (
    AGENT_CREATE,
    AGENT_UPDATE,
    ROLE_ADMIN,
    SCOPE_MANAGE,
    SKILL_MANAGE,
)
from workmate_product.persistence.models import (
    Agent,
    AgentCapabilityBinding,
    AgentDefault,
    CapabilityInstallation,
    CapabilityType,
    ResourceShare,
    Role,
    RoleBinding,
    Scope,
    ScopeKind,
    ScopeMembership,
    User,
    Visibility,
)
from app.services.access_decision import AccessDecisionService
from app.services.bootstrap import (
    create_scope,
    ensure_bootstrap,
    ensure_user_scopes,
    get_root_scope,
)


@pytest.fixture(autouse=True)
async def _bootstrapped(db_session):
    """db_session only builds tables; seed root scope + RBAC."""
    await ensure_bootstrap(db_session)


async def _make_user(db, email: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email, password_hash="x", display_name=email,
    )
    db.add(user)
    await db.flush()
    await ensure_user_scopes(db, user)
    return user


async def _bind_role(
    db, user_id, role_id, scope_id, include_descendants=True,
):
    db.add(RoleBinding(
        user_id=user_id,
        role_id=role_id,
        scope_id=scope_id,
        include_descendants=include_descendants,
    ))
    await db.flush()


async def _role(db, name) -> Role:
    return (await db.scalars(
        select(Role).where(Role.key == name)
    )).first()


async def _make_agent(db, user: User, scope_id: str, slug: str) -> Agent:
    agent = Agent(
        id=str(uuid.uuid4()),
        slug=slug,
        display_name=slug,
        system_prompt="",
        agentscope_agent_id=f"runtime-{slug}",
        scope_id=scope_id,
        visibility=Visibility.PRIVATE,
        include_descendants=True,
        created_by=user.id,
    )
    db.add(agent)
    await db.flush()
    return agent


async def _bind_capability(
    db,
    agent: Agent,
    user: User,
    scope_id: str,
    *,
    installation_id: str,
    enabled: bool = True,
    shared: bool = False,
) -> CapabilityInstallation:
    installation = CapabilityInstallation(
        id=installation_id,
        capability_type=CapabilityType.SKILL,
        scope_id=scope_id,


        name=f"skill-{installation_id}",
        display_name=installation_id,
        enabled=enabled,
        metadata_={},
        created_by=user.id,
    )
    db.add(installation)
    await db.flush()
    if shared:
        db.add(ResourceShare(
            id=str(uuid.uuid4()), capability_id=installation.id,
            target_scope_id=scope_id, include_descendants=False,
        ))
    db.add(AgentCapabilityBinding(
        agent_id=agent.id,
        capability_installation_id=installation.id,
    ))
    await db.flush()
    return installation


@pytest.mark.asyncio
async def test_member_has_no_permissions(db_session):
    db = db_session
    user = await _make_user(db, "member@test.dev")
    access = AccessDecisionService(db)
    root = await get_root_scope(db)
    assert await access.effective_permissions(user.id) == set()
    assert not await access.has_permission(user.id, root.id, AGENT_CREATE)
    assert not await access.has_permission(user.id, root.id, SCOPE_MANAGE)


@pytest.mark.asyncio
async def test_admin_binding_grants_all_permissions(db_session):
    db = db_session
    user = await _make_user(db, "admin@test.dev")
    root = await get_root_scope(db)
    await _bind_role(db, user.id, (await _role(db, ROLE_ADMIN)).id, root.id)
    access = AccessDecisionService(db)
    for permission in (AGENT_CREATE, AGENT_UPDATE, SKILL_MANAGE,
                       SCOPE_MANAGE):
        assert await access.has_permission(user.id, root.id, permission)


@pytest.mark.asyncio
async def test_first_registered_user_is_root_admin(client_and_app):
    """Registration order determines the root admin (RBAC §4)."""
    from app.db import get_sessionmaker

    http, _ = client_and_app
    await http.post("/api/auth/register", json={
        "email": "first@test.dev", "password": "password123",
        "display_name": "First", "organization_name": "Test organization",
    })
    async with get_sessionmaker()() as db:
        user = (await db.scalars(
            select(User).where(User.email == "first@test.dev")
        )).first()
        root = await get_root_scope(db)
        access = AccessDecisionService(db)
        assert await access.has_permission(user.id, root.id, SCOPE_MANAGE)


@pytest.mark.asyncio
async def test_ancestor_binding_needs_include_descendants(db_session):
    """A binding on an ancestor scope covers the descendant only when
    include_descendants is set (RBAC §5)."""
    db = db_session
    user = await _make_user(db, "dept-user@test.dev")
    root = await get_root_scope(db)
    dept = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Engineering", parent=root,
    )
    admin_role = await _role(db, ROLE_ADMIN)
    await _bind_role(
        db, user.id, admin_role.id, root.id, include_descendants=True,
    )
    access = AccessDecisionService(db)
    assert await access.has_permission(user.id, dept.id, AGENT_UPDATE)

    # Without descendant inheritance the ancestor grant stops at the
    # ancestor scope itself.
    binding = (await db.scalars(
        select(RoleBinding).where(
            RoleBinding.user_id == user.id,
            RoleBinding.role_id == admin_role.id,
        )
    )).first()
    binding.include_descendants = False
    await db.flush()
    assert not await access.has_permission(user.id, dept.id, AGENT_UPDATE)
    assert await access.has_permission(user.id, root.id, AGENT_UPDATE)


@pytest.mark.asyncio
async def test_permissions_for_scope_never_leak_across_visible_departments(
    db_session,
):
    db = db_session
    user = await _make_user(db, "scope-actions@test.dev")
    root = await get_root_scope(db)
    department_a = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Action A", parent=root,
    )
    department_b = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Visible B", parent=root,
    )
    db.add(ScopeMembership(
        user_id=user.id, scope_id=department_b.id,
    ))
    role = await _role(db, ROLE_ADMIN)
    await _bind_role(
        db, user.id, role.id, department_a.id,
        include_descendants=False,
    )

    access = AccessDecisionService(db)
    assert AGENT_CREATE in await access.permissions_for_scope(
        user.id, department_a.id,
    )
    assert await access.permissions_for_scope(user.id, department_b.id) == set()
    assert await access.permissions_for_scope(user.id, root.id) == set()


@pytest.mark.asyncio
async def test_closure_covers_all_generations(db_session):
    db = db_session
    root = await get_root_scope(db)
    child = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Parent department", parent=root,
    )
    grandchild = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Child department", parent=child,
    )

    access = AccessDecisionService(db)
    root_descendants = await access._descendant_ids(root.id)
    assert {child.id, grandchild.id} <= root_descendants
    assert root.id not in root_descendants  # strict descendants
    # A middle scope never contains its parent.
    child_descendants = await access._descendant_ids(child.id)
    assert child_descendants == {grandchild.id}


@pytest.mark.asyncio
async def test_member_scope_ids_cover_personal_and_memberships(db_session):
    db = db_session
    user = await _make_user(db, "hierarchy@test.dev")
    root = await get_root_scope(db)
    child = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Subdepartment", parent=root,
    )
    db.add(ScopeMembership(user_id=user.id, scope_id=child.id))
    await db.flush()

    access = AccessDecisionService(db)
    scope_ids = set(await access.member_scope_ids(user.id))
    personal = await access.personal_scope_id(user.id)
    assert personal is not None
    # Root membership comes with registration (RBAC §4.2); the extra
    # department membership was added explicitly.
    assert {root.id, child.id} <= scope_ids


@pytest.mark.asyncio
async def test_personal_agent_department_capability_becomes_unavailable_after_revocation(
    db_session,
):
    """Removing the owner's department membership invalidates the old binding."""
    db = db_session
    user = await _make_user(db, "revoked-capability@test.dev")
    root = await get_root_scope(db)
    department = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Capability department", parent=root,
    )
    db.add(ScopeMembership(user_id=user.id, scope_id=department.id))
    personal_scope_id = await AccessDecisionService(db).personal_scope_id(user.id)
    agent = await _make_agent(db, user, personal_scope_id, "personal-revoked")
    await _bind_capability(
        db, agent, user, department.id, installation_id="dept-capability", shared=True,
    )

    access = AccessDecisionService(db)
    assert await access.unavailable_capability_ids(user.id, agent.id) == []

    await db.execute(delete(ScopeMembership).where(
        ScopeMembership.user_id == user.id,
        ScopeMembership.scope_id == department.id,
    ))
    await db.flush()

    assert await access.unavailable_capability_ids(user.id, agent.id) == [
        "dept-capability",
    ]


@pytest.mark.asyncio
async def test_personal_agent_rejects_disabled_and_personal_scope_bindings_sorted(
    db_session,
):
    """Disabled or Personal-scoped installations are unusable and sorted."""
    db = db_session
    user = await _make_user(db, "invalid-capabilities@test.dev")
    root = await get_root_scope(db)
    access = AccessDecisionService(db)
    personal_scope_id = await access.personal_scope_id(user.id)
    agent = await _make_agent(db, user, personal_scope_id, "personal-invalid")
    await _bind_capability(
        db, agent, user, personal_scope_id,
        installation_id="z-personal-capability",
    )
    await _bind_capability(
        db, agent, user, root.id,
        installation_id="a-disabled-capability", enabled=False, shared=True,
    )

    assert await access.unavailable_capability_ids(user.id, agent.id) == [
        "a-disabled-capability",
        "z-personal-capability",
    ]


@pytest.mark.asyncio
async def test_personal_agent_without_scope_owner_fails_closed(db_session):
    """Corrupt Personal scope ownership cannot borrow the requesting user."""
    db = db_session
    user = await _make_user(db, "missing-owner@test.dev")
    root = await get_root_scope(db)
    access = AccessDecisionService(db)
    personal_scope_id = await access.personal_scope_id(user.id)
    agent = await _make_agent(db, user, personal_scope_id, "missing-owner")
    await _bind_capability(
        db, agent, user, root.id,
        installation_id="must-fail-closed", shared=True,
    )
    personal_scope = await db.get(Scope, personal_scope_id)
    personal_scope.owner_user_id = None
    await db.flush()

    assert await access.unavailable_capability_ids(user.id, agent.id) == [
        "must-fail-closed",
    ]
    factory = AgentOutFactory(db, access, user.id)
    await factory.prepare([agent])
    projected = await factory.build(agent, can_use=False)
    assert projected.unavailable_capability_ids == ["must-fail-closed"]


@pytest.mark.asyncio
async def test_organization_agent_capability_requires_current_user_access(
    db_session,
):
    """Enabled state alone must not authorize a non-member at execution time."""
    db = db_session
    creator = await _make_user(db, "org-agent-creator@test.dev")
    viewer = await _make_user(db, "org-agent-viewer@test.dev")
    root = await get_root_scope(db)
    department = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Nonmember department", parent=root,
    )
    agent = await _make_agent(db, creator, department.id, "department-agent")
    installation = await _bind_capability(
        db, agent, creator, department.id,
        installation_id="department-managed-capability", shared=True,
    )

    access = AccessDecisionService(db)
    assert await access.unavailable_capability_ids(viewer.id, agent.id) == [installation.id]
    db.add(ScopeMembership(scope_id=department.id, user_id=viewer.id))
    await db.flush()
    assert await access.unavailable_capability_ids(viewer.id, agent.id) == []

    installation.enabled = False
    await db.flush()
    assert await access.unavailable_capability_ids(viewer.id, agent.id) == [
        "department-managed-capability",
    ]


async def _set_default(db, scope_id: str, agent: Agent, user: User) -> None:
    db.add(AgentDefault(
        scope_id=scope_id,
        agent_id=agent.id,
        updated_by=user.id,
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_default_resolution_prefers_deeper_direct_department_then_id(
    db_session,
):
    """Multiple direct departments have a stable depth/id priority."""
    db = db_session
    user = await _make_user(db, "multi-department-default@test.dev")
    root = await get_root_scope(db)
    shallow_a = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Shallow A", parent=root,
    )
    shallow_b = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Shallow B", parent=root,
    )
    branch = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Branch", parent=root,
    )
    deep = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Deep", parent=branch,
    )
    for scope in (shallow_a, shallow_b, deep):
        db.add(ScopeMembership(user_id=user.id, scope_id=scope.id))

    agents = {}
    for scope in (shallow_a, shallow_b, deep):
        agent = await _make_agent(db, user, scope.id, f"default-{scope.name}")
        agent.visibility = Visibility.PUBLIC
        db.add(ResourceShare(id=str(uuid.uuid4()), agent_id=agent.id,
                             target_scope_id=scope.id, include_descendants=False))
        await _set_default(db, scope.id, agent, user)
        agents[scope.id] = agent

    access = AccessDecisionService(db)
    assert await access.resolve_default_agent(user.id) == agents[deep.id]

    await db.delete(await db.get(AgentDefault, deep.id))
    await db.flush()
    expected_tie_winner = min(shallow_a.id, shallow_b.id)
    assert (
        await access.resolve_default_agent(user.id)
        == agents[expected_tie_winner]
    )


@pytest.mark.asyncio
async def test_default_resolution_walks_ancestor_departments_before_root(
    db_session,
):
    """A direct department falls back through nearest ancestors to root."""
    db = db_session
    user = await _make_user(db, "ancestor-default@test.dev")
    root = await get_root_scope(db)
    upper = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Upper", parent=root,
    )
    nearer = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Nearer", parent=upper,
    )
    direct = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Direct", parent=nearer,
    )
    db.add(ScopeMembership(user_id=user.id, scope_id=direct.id))

    upper_agent = await _make_agent(db, user, upper.id, "upper-default")
    nearer_agent = await _make_agent(db, user, nearer.id, "nearer-default")
    root_agent = await _make_agent(db, user, root.id, "root-default")
    for agent in (upper_agent, nearer_agent, root_agent):
        agent.visibility = Visibility.PUBLIC
        db.add(ResourceShare(id=str(uuid.uuid4()), agent_id=agent.id,
                             target_scope_id=direct.id, include_descendants=False))
    await _set_default(db, upper.id, upper_agent, user)
    await _set_default(db, nearer.id, nearer_agent, user)
    await _set_default(db, root.id, root_agent, user)

    access = AccessDecisionService(db)
    assert await access.resolve_default_agent(user.id) == nearer_agent

    await db.delete(await db.get(AgentDefault, nearer.id))
    await db.flush()
    assert await access.resolve_default_agent(user.id) == upper_agent

    await db.delete(await db.get(AgentDefault, upper.id))
    await db.flush()
    assert await access.resolve_default_agent(user.id) == root_agent


@pytest.mark.asyncio
async def test_agent_management_and_legacy_ancestry_do_not_grant_use(db_session):
    db = db_session
    creator = await _make_user(db, 'grant-creator@test.dev')
    manager = await _make_user(db, 'grant-manager@test.dev')
    member = await _make_user(db, 'grant-member@test.dev')
    root = await get_root_scope(db)
    department = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Parent', parent=root)
    child = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Child', parent=department)
    db.add(ScopeMembership(scope_id=child.id, user_id=member.id))
    await _bind_role(db, manager.id, (await _role(db, ROLE_ADMIN)).id, department.id)
    agent = await _make_agent(db, creator, department.id, 'separate-agent-use')
    access = AccessDecisionService(db)
    assert await access.can_use_agent(creator.id, agent.id)  # Draft trial only.
    assert await access.can_view_agent(manager.id, agent.id)
    assert not await access.can_use_agent(manager.id, agent.id)
    agent.visibility = Visibility.PUBLIC
    await db.flush()
    assert not await access.can_use_agent(creator.id, agent.id)
    assert not await access.can_use_agent(manager.id, agent.id)
    assert not await access.can_view_agent(member.id, agent.id)
    assert agent not in await access.list_visible_agents(member.id)
    db.add(ResourceShare(id=str(uuid.uuid4()), agent_id=agent.id,
                         target_scope_id=department.id, include_descendants=False))
    await db.flush()
    assert not await access.can_use_agent(member.id, agent.id)
    db.add(ResourceShare(id=str(uuid.uuid4()), agent_id=agent.id,
                         target_scope_id=child.id, include_descendants=False))
    await db.flush()
    assert await access.can_use_agent(member.id, agent.id)
    assert agent in await access.list_visible_agents(member.id)
    agent.visibility = Visibility.PRIVATE
    await db.flush()
    assert not await access.can_view_agent(member.id, agent.id)
    assert not await access.can_use_agent(member.id, agent.id)
