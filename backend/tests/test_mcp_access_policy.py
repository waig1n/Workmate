"""MCP use and Agent bindings require grants independently of management."""
import uuid
from sqlalchemy import select
from app.services.access_decision import AccessDecisionService
from app.services.bootstrap import create_scope, ensure_bootstrap, ensure_user_scopes, get_root_scope
from workmate_product.persistence.models import CapabilityInstallation, ResourceShare, Role, RoleBinding, ScopeKind, ScopeMembership, User


async def test_mcp_management_and_ancestry_do_not_grant_use(db_session):
    db = db_session
    await ensure_bootstrap(db)
    root = await get_root_scope(db)
    parent = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Finance', parent=root)
    child = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Accounting', parent=parent)
    user = User(id=str(uuid.uuid4()), email='mcp-member@example.com', password_hash='x', display_name='Member')
    db.add(user)
    await db.flush()
    await ensure_user_scopes(db, user)
    db.add(ScopeMembership(user_id=user.id, scope_id=child.id))
    admin = await db.scalar(select(Role).where(Role.key == 'admin'))
    db.add(RoleBinding(user_id=user.id, role_id=admin.id, scope_id=root.id, include_descendants=True))
    cap = CapabilityInstallation(id=str(uuid.uuid4()), capability_type='mcp', scope_id=parent.id, name='finance-mcp')
    db.add(cap)
    await db.flush()
    access = AccessDecisionService(db)
    assert await access.has_permission(user.id, parent.id, 'mcp.manage')
    assert cap.id not in await access.accessible_capability_ids(user.id)
    assert cap.id not in await access.bindable_capability_ids(child)
    grant = ResourceShare(id=str(uuid.uuid4()), capability_id=cap.id, target_scope_id=parent.id, include_descendants=False)
    db.add(grant)
    await db.flush()
    assert cap.id not in await access.accessible_capability_ids(user.id)
    grant.include_descendants = True
    await db.flush()
    assert cap.id in await access.accessible_capability_ids(user.id)
    assert cap.id in await access.bindable_capability_ids(child)
    future = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Future team', parent=child)
    assert cap.id in await access.bindable_capability_ids(future)
    independent = ResourceShare(id=str(uuid.uuid4()), capability_id=cap.id, target_scope_id=child.id)
    db.add(independent)
    await db.delete(grant)
    await db.flush()
    assert cap.id in await access.bindable_capability_ids(child)
    assert cap.id not in await access.bindable_capability_ids(future)
    await db.delete(independent)
    await db.flush()
    assert cap.id not in await access.accessible_capability_ids(user.id)
