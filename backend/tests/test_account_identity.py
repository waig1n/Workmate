"""Account identity and atomic department transfer security contracts."""
import pytest
from sqlalchemy import select
from workmate_product.persistence.models import Role, RoleBinding, ScopeKind, ScopeMembership, User
from app.services.bootstrap import ensure_bootstrap, ensure_user_scopes, get_root_scope, create_scope
from app.services.role_management import RoleManagementService
from app.services.user_management import UserManagementService, UserManagementError
from app.services.scoped_administration import ScopedAdministrationService


async def setup_accounts(db):
    await ensure_bootstrap(db)
    admin = User(id='admin-user', email='admin@test.dev', password_hash='x', display_name='Admin')
    person = User(id='employee', email='employee@test.dev', password_hash='x', display_name='Employee')
    db.add_all([admin, person])
    await db.flush()
    await ensure_user_scopes(db, admin, first_user=True)
    await ensure_user_scopes(db, person)
    root = await get_root_scope(db)
    finance = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Finance', parent=root)
    sales = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Sales', parent=root)
    db.add(ScopeMembership(scope_id=finance.id, user_id=person.id))
    await db.commit()
    return admin, person, root, finance, sales


async def grant(db, user, scope, key, permissions):
    role, _ = await RoleManagementService(db).create_role(key=key, display_name=key,
        description='', permission_keys=permissions, delegatable=False)
    db.add(RoleBinding(user_id=user.id, scope_id=scope.id, role_id=role.id, include_descendants=True))
    await db.commit()
    return role


async def test_ordinary_account_has_identity_without_member_role(db_session):
    _, person, root, _, _ = await setup_accounts(db_session)
    assert await db_session.get(ScopeMembership, (root.id, person.id))
    assert not (await db_session.scalars(select(RoleBinding).where(RoleBinding.user_id == person.id))).all()
    assert await db_session.scalar(select(Role).where(Role.key == 'member')) is None
    assert (await db_session.scalar(select(Role).where(Role.key == 'admin'))).display_name == '组织管理员'


@pytest.mark.parametrize('retain', [False, True])
async def test_transfer_preserves_identity_and_other_roles(db_session, retain):
    admin, person, root, finance, sales = await setup_accounts(db_session)
    local = await grant(db_session, person, finance, 'finance_editor', ['agent.update'])
    other = await grant(db_session, person, sales, 'sales_editor', ['agent.update'])
    await UserManagementService(db_session, actor_id=admin.id).update_access(person,
        membership_scope_ids=[sales.id], retain_departure_roles=retain)
    assert await db_session.get(ScopeMembership, (root.id, person.id))
    assert await db_session.get(ScopeMembership, (finance.id, person.id)) is None
    assert bool(await db_session.get(RoleBinding, (person.id, local.id, finance.id))) is retain
    assert await db_session.get(RoleBinding, (person.id, other.id, sales.id))


@pytest.mark.parametrize('scoped', [False, True])
async def test_member_manager_can_move_without_roles_but_cannot_silently_change_roles(db_session, scoped):
    admin, person, root, finance, sales = await setup_accounts(db_session)
    await grant(db_session, admin, root, 'members', ['membership.manage', 'membership.read', 'user.read'])
    admin_role = await db_session.scalar(select(Role).where(Role.key == 'admin'))
    await db_session.delete(await db_session.get(RoleBinding, (admin.id, admin_role.id, root.id)))
    # Keep an independent root administrator for the last-admin invariant.
    db_session.add(RoleBinding(user_id=person.id, scope_id=root.id, role_id=admin_role.id))
    await db_session.commit()
    local = await grant(db_session, person, finance, 'local', ['agent.update'])
    with pytest.raises(UserManagementError) as error:
        if scoped:
            await ScopedAdministrationService(db_session, admin.id).set_member(finance.id, person.id,
                person.access_version, present=False)
        else:
            await UserManagementService(db_session, actor_id=admin.id).update_access(person,
                membership_scope_ids=[sales.id])
    assert error.value.code == 'department_roles_require_authorization'
    assert await db_session.get(ScopeMembership, (finance.id, person.id))
    assert await db_session.get(RoleBinding, (person.id, local.id, finance.id))
    await db_session.delete(await db_session.get(RoleBinding, (person.id, local.id, finance.id)))
    await db_session.commit()
    if scoped:
        await ScopedAdministrationService(db_session, admin.id).set_member(finance.id, person.id,
            person.access_version, present=False)
    else:
        await UserManagementService(db_session, actor_id=admin.id).update_access(person,
            membership_scope_ids=[sales.id])
    assert await db_session.get(ScopeMembership, (finance.id, person.id)) is None
    assert await db_session.get(ScopeMembership, (root.id, person.id))


async def test_organization_membership_cannot_be_removed(db_session):
    admin, person, root, _, _ = await setup_accounts(db_session)
    with pytest.raises(UserManagementError, match='企业成员身份'):
        await ScopedAdministrationService(db_session, admin.id).set_member(root.id, person.id,
            person.access_version, present=False)
    assert await db_session.get(ScopeMembership, (root.id, person.id))


async def test_role_binder_can_edit_roles_without_department_management(db_session):
    admin, person, root, finance, sales = await setup_accounts(db_session)
    binder = await grant(db_session, admin, root, 'binder', ['role.bind'])
    admin_role = await db_session.scalar(select(Role).where(Role.key == 'admin'))
    await db_session.delete(await db_session.get(RoleBinding, (admin.id, admin_role.id, root.id)))
    db_session.add(RoleBinding(user_id=person.id, scope_id=root.id, role_id=admin_role.id))
    await db_session.commit()
    _, bindings, _ = await UserManagementService(db_session).user_access(person)
    await UserManagementService(db_session, actor_id=admin.id).update_access(person,
        membership_scope_ids=[finance.id], role_bindings=bindings)
    with pytest.raises(UserManagementError) as error:
        await UserManagementService(db_session, actor_id=admin.id).update_access(person,
            membership_scope_ids=[sales.id], role_bindings=bindings)
    assert error.value.status_code == 403
    assert await db_session.get(ScopeMembership, (finance.id, person.id))


@pytest.mark.parametrize('scoped', [False, True])
async def test_explicit_retention_requires_role_authority_even_without_departure_roles(db_session, scoped):
    admin, person, root, finance, _ = await setup_accounts(db_session)
    await grant(db_session, admin, root, 'members', ['membership.manage', 'user.read'])
    admin_role = await db_session.scalar(select(Role).where(Role.key == 'admin'))
    await db_session.delete(await db_session.get(RoleBinding, (admin.id, admin_role.id, root.id)))
    db_session.add(RoleBinding(user_id=person.id, scope_id=root.id, role_id=admin_role.id))
    await db_session.commit()
    with pytest.raises(UserManagementError) as error:
        if scoped:
            await ScopedAdministrationService(db_session, admin.id).set_member(finance.id, person.id,
                person.access_version, present=False, retain_departure_roles=True)
        else:
            await UserManagementService(db_session, actor_id=admin.id).update_access(person,
                membership_scope_ids=[], retain_departure_roles=True)
    assert error.value.status_code == 403
    assert await db_session.get(ScopeMembership, (finance.id, person.id))


async def test_default_departure_clears_roles_even_if_stale_client_submits_them(db_session):
    admin, person, root, finance, sales = await setup_accounts(db_session)
    local = await grant(db_session, person, finance, 'local', ['agent.update'])
    _, bindings, _ = await UserManagementService(db_session).user_access(person)
    await UserManagementService(db_session, actor_id=admin.id).update_access(person,
        membership_scope_ids=[sales.id], role_bindings=bindings)
    assert await db_session.get(RoleBinding, (person.id, local.id, finance.id)) is None
    assert await db_session.get(ScopeMembership, (root.id, person.id))


async def test_onboarding_membership_manager_does_not_need_role_bind(db_session):
    admin, person, root, finance, _ = await setup_accounts(db_session)
    await grant(db_session, person, root, 'onboarding', ['user.manage', 'membership.manage'])
    created = await UserManagementService(db_session, actor_id=person.id).create_user(
        email='new@test.dev', display_name='New', password='test-password-123',
        must_change_password=True, membership_scope_ids=[finance.id])
    assert await db_session.get(ScopeMembership, (root.id, created.id))
    assert await db_session.get(ScopeMembership, (finance.id, created.id))
    assert not (await db_session.scalars(select(RoleBinding).where(RoleBinding.user_id == created.id))).all()
    with pytest.raises(UserManagementError) as error:
        await UserManagementService(db_session, actor_id=person.id).create_user(
            email='illegal@test.dev', display_name='Illegal', password='test-password-123',
            must_change_password=True, role_bindings=[{'scope_id': root.id, 'role_key': 'admin'}])
    assert error.value.status_code == 403
    assert await db_session.scalar(select(User).where(User.email == 'illegal@test.dev')) is None


@pytest.mark.parametrize('retain', [False, True])
async def test_scoped_departure_cleans_or_explicitly_retains_direct_roles(db_session, retain):
    admin, person, root, finance, sales = await setup_accounts(db_session)
    local = await grant(db_session, person, finance, 'local', ['agent.update'])
    other = await grant(db_session, person, sales, 'other', ['agent.update'])
    await ScopedAdministrationService(db_session, admin.id).set_member(finance.id, person.id,
        person.access_version, present=False, retain_departure_roles=retain)
    assert bool(await db_session.get(RoleBinding, (person.id, local.id, finance.id))) is retain
    assert await db_session.get(RoleBinding, (person.id, other.id, sales.id))
    assert await db_session.get(ScopeMembership, (root.id, person.id))


async def test_removed_role_key_cannot_be_created_or_assigned(db_session):
    from app.services.role_management import RoleManagementError
    admin, person, root, finance, _ = await setup_accounts(db_session)
    with pytest.raises(RoleManagementError) as error:
        await RoleManagementService(db_session, actor_id=admin.id).create_role(key='member',
            display_name='Ordinary member', description='', permission_keys=[])
    assert error.value.code == 'reserved_role_key'
    with pytest.raises(UserManagementError) as error:
        await UserManagementService(db_session, actor_id=admin.id).update_access(person,
            membership_scope_ids=[finance.id], role_bindings=[{'scope_id': root.id, 'role_key': 'member'}])
    assert error.value.code == 'role_not_found'
    assert await db_session.get(ScopeMembership, (finance.id, person.id))


async def test_department_editor_cannot_replace_roles_and_version_conflict_keeps_access(db_session):
    admin, person, root, finance, sales = await setup_accounts(db_session)
    await grant(db_session, person, root, 'members', ['membership.manage'])
    with pytest.raises(UserManagementError) as error:
        await UserManagementService(db_session, actor_id=person.id).update_access(person,
            membership_scope_ids=[sales.id], role_bindings=[])
    assert error.value.status_code == 403
    with pytest.raises(UserManagementError) as error:
        await UserManagementService(db_session, actor_id=admin.id).update_access(person,
            membership_scope_ids=[sales.id], expected_version=person.access_version + 1)
    assert error.value.code == 'version_conflict'
    assert await db_session.get(ScopeMembership, (finance.id, person.id))
    assert await db_session.get(ScopeMembership, (sales.id, person.id)) is None


async def test_removing_nonmember_does_not_revoke_explicit_cross_department_responsibility(db_session):
    admin, person, _, _, sales = await setup_accounts(db_session)
    other = await grant(db_session, person, sales, 'other', ['agent.update'])
    await ScopedAdministrationService(db_session, admin.id).set_member(sales.id, person.id,
        person.access_version, present=False)
    assert await db_session.get(RoleBinding, (person.id, other.id, sales.id))
