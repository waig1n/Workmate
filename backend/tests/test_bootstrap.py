"""Startup seeds RBAC definitions without migrating or granting existing users."""

import pytest
from sqlalchemy import select

from workmate_product.authorization.permissions import SYSTEM_PERMISSION_LABELS
from workmate_product.persistence.models import Permission, Role, RoleBinding, ScopeMembership, User
from app.services.bootstrap import ensure_bootstrap, ensure_user_scopes, get_personal_scope, get_root_scope


async def test_startup_does_not_provision_existing_accounts(db_session):
    user = User(id='existing', email='existing@example.invalid', password_hash='x', display_name='Test account')
    db_session.add(user)
    await db_session.commit()

    await ensure_bootstrap(db_session)
    await ensure_bootstrap(db_session)

    assert await get_root_scope(db_session) is not None
    assert await db_session.scalar(select(Role).where(Role.key == 'admin')) is not None
    assert await get_personal_scope(db_session, user.id) is None
    assert not (await db_session.scalars(select(ScopeMembership))).all()
    assert not (await db_session.scalars(select(RoleBinding))).all()


async def test_bootstrap_maintains_chinese_names_for_system_permissions(db_session):
    await ensure_bootstrap(db_session)
    permissions = {
        item.key: item
        for item in (await db_session.scalars(select(Permission))).all()
    }

    assert {
        key: permissions[key].display_name for key in SYSTEM_PERMISSION_LABELS
    } == SYSTEM_PERMISSION_LABELS
    assert all(permissions[key].is_system for key in SYSTEM_PERMISSION_LABELS)


async def test_bootstrap_repairs_legacy_system_names_without_changing_custom_permissions(db_session):
    await ensure_bootstrap(db_session)
    built_in = await db_session.scalar(
        select(Permission).where(Permission.key == "mcp.manage"),
    )
    built_in.display_name = built_in.key
    custom = Permission(
        id="custom-permission",
        key="finance.export",
        display_name="Export financial reports",
        is_system=False,
    )
    db_session.add(custom)
    await db_session.commit()

    await ensure_bootstrap(db_session)

    await db_session.refresh(built_in)
    await db_session.refresh(custom)
    assert built_in.display_name == "管理工具连接"
    assert custom.display_name == "Export financial reports"
    assert custom.is_system is False


@pytest.mark.parametrize('first_user', [False, True])
async def test_registration_explicitly_provisions_user_scopes(db_session, first_user):
    await ensure_bootstrap(db_session)
    user = User(id='registered', email='registered@example.invalid', password_hash='x', display_name='Test account')
    db_session.add(user)
    await db_session.flush()
    await ensure_user_scopes(db_session, user, first_user=first_user)

    root = await get_root_scope(db_session)
    assert await get_personal_scope(db_session, user.id) is not None
    assert await db_session.get(ScopeMembership, (root.id, user.id)) is not None
    role = await db_session.scalar(select(Role).where(Role.key == 'admin'))
    assert bool(await db_session.get(RoleBinding, (user.id, role.id, root.id))) is first_user
    assert await db_session.scalar(select(Role).where(Role.key == 'member')) is None
