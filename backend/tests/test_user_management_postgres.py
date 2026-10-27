# -*- coding: utf-8 -*-
"""Opt-in PostgreSQL concurrency tests for the last-root-admin
invariant (user management plan §3.6).

Run with WORKMATE_TEST_POSTGRES_URL pointing at a disposable database, e.g.
postgresql+asyncpg://workmate:workmate@localhost:5432/workmate_concurrency_test.
SQLite unit tests cover the branch semantics only; the row-lock
evidence lives here.
"""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4
import os

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from workmate_product.persistence.models import (
    Base,
    Role,
    RoleBinding,
    Scope,
    ScopeKind,
    ScopeMembership,
    User,
    UserStatus,
)
from app.db import get_sessionmaker as _app_sessionmaker
from app.services.bootstrap import ensure_user_scopes
from app.services.user_management import (
    UserManagementError,
    UserManagementService,
)

pytestmark = pytest.mark.integration


def _postgres_url() -> str:
    url = os.getenv("WORKMATE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set WORKMATE_TEST_POSTGRES_URL to a disposable database")
    return url


@asynccontextmanager
async def _setup_engine(database_url: str):
    # Never drop tables installed by the independent Alembic test. Metadata
    # fixtures own their schema, including their own immutable-record triggers.
    schema = 'user_management_' + uuid4().hex
    engine = create_async_engine(database_url,
        connect_args={'server_settings': {'search_path': schema}})
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await engine.dispose()


async def _make_admin(sessionmaker, email: str) -> User:
    async with sessionmaker() as db:
        user = User(
            id=email,
            email=email,
            normalized_email=email,
            password_hash="x",
            display_name=email,
        )
        db.add(user)
        await db.flush()
        await ensure_user_scopes(db, user, first_user=False)
        root = await db.scalar(
            select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
        )
        admin_role = await db.scalar(
            select(Role).where(Role.key == "admin"),
        )
        db.add(RoleBinding(
            user_id=user.id, role_id=admin_role.id, scope_id=root.id,
        ))
        await db.commit()
        return user


async def test_concurrent_mutual_disable_allows_at_most_one(
    monkeypatch,
):
    """Two admins disabling each other concurrently: at most one
    succeeds, the system never loses its last active root admin."""
    async with _setup_engine(_postgres_url()) as (engine, sessionmaker):
        admin_a = await _make_admin(sessionmaker, "a@admintest.dev")
        admin_b = await _make_admin(sessionmaker, "b@admintest.dev")

        results: dict[str, Exception | None] = {}

        async def disable(actor_id: str, target_id: str, key: str):
            async with sessionmaker() as db:
                actor = await db.get(User, actor_id)
                target = await db.get(User, target_id)
                service = UserManagementService(db)
                try:
                    await service.disable_user(actor=actor, user=target)
                    results[key] = None
                except UserManagementError as error:
                    results[key] = error

        await asyncio.gather(
            disable(admin_a.id, admin_b.id, "a_disables_b"),
            disable(admin_b.id, admin_a.id, "b_disables_a"),
        )

        failures = [
            key for key, value in results.items() if value is not None
        ]
        assert len(failures) == 1, "exactly one mutual disable must fail"

        async with sessionmaker() as db:
            statuses = dict((await db.execute(
                select(User.id, User.status),
            )).all())
        active = [
            user_id for user_id, status in statuses.items()
            if status == UserStatus.ACTIVE
        ]
        assert len(active) == 1, "exactly one active root admin survives"
        actor_ids = {"a_disables_b": admin_a.id, "b_disables_a": admin_b.id}
        for key in failures:
            error = results[key]
            assert error.code in {"last_root_admin", "permission_required"}
            if error.code == "permission_required":
                # The shared lock rechecks the actor after the winning disable.
                # This denial must specifically concern the now-disabled actor.
                assert statuses[actor_ids[key]] == UserStatus.DISABLED
                assert error.status_code == 403


async def test_concurrent_access_put_never_empties_admin_pool():
    """Concurrent access PUTs stripping each admin's binding: at least
    one admin keeps effective root authority."""
    async with _setup_engine(_postgres_url()) as (engine, sessionmaker):
        admin_a = await _make_admin(sessionmaker, "a@accesstest.dev")
        admin_b = await _make_admin(sessionmaker, "b@accesstest.dev")
        root_id = None
        async with sessionmaker() as db:
            root = await db.scalar(
                select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
            )
            root_id = root.id

        async def strip_admin(admin_id: str):
            async with sessionmaker() as db:
                target = await db.get(User, admin_id)
                service = UserManagementService(db)
                try:
                    await service.update_access(
                        target,
                        membership_scope_ids=[root_id],
                        role_bindings=[],
                    )
                    return None
                except UserManagementError as error:
                    return error

        outcomes = await asyncio.gather(
            strip_admin(admin_a.id), strip_admin(admin_b.id),
        )
        failures = [
            error for error in outcomes if error is not None
        ]
        assert len(failures) >= 1
        for error in failures:
            assert error.code == "last_root_admin"

        # At least one admin still holds the admin binding on root.
        async with sessionmaker() as db:
            admin_role = await db.scalar(
                select(Role).where(Role.key == "admin"),
            )
            bindings = (await db.scalars(
                select(RoleBinding).where(
                    RoleBinding.role_id == admin_role.id,
                    RoleBinding.scope_id == root_id,
                ),
            )).all()
            active_ids = {
                user.id for user in (await db.scalars(
                    select(User).where(User.status == UserStatus.ACTIVE),
                )).all()
            }
            surviving = [
                binding for binding in bindings
                if binding.user_id in active_ids
            ]
            assert len(surviving) >= 1


async def test_concurrent_mutual_move_allows_at_most_one():
    """Two departments swapping parents concurrently: the tree stays
    acyclic and closure-consistent (user management plan Task 7)."""
    async with _setup_engine(_postgres_url()) as (engine, sessionmaker):
        admin = await _make_admin(sessionmaker, "scope@test.dev")
        root_id, dept_a, dept_b = None, None, None
        async with sessionmaker() as db:
            from app.services.scope_management import ScopeManagementService
            service = ScopeManagementService(db, admin.id)
            root = await _root_scope(db)
            root_id = root.id
            dept_a = await service.create_department(
                parent_id=root_id, name="alpha",
            )
            dept_b = await service.create_department(
                parent_id=root_id, name="beta",
            )

        from app.services.scope_management import (
            ScopeManagementError,
            ScopeManagementService,
        )

        outcomes: dict[str, Exception | None] = {}

        async def move(scope_id: str, new_parent_id: str, key: str):
            async with sessionmaker() as db:
                scope = await db.get(Scope, scope_id)
                service = ScopeManagementService(db, admin.id)
                try:
                    await service.move(scope, new_parent_id)
                    outcomes[key] = None
                except ScopeManagementError as error:
                    outcomes[key] = error

        await asyncio.gather(
            move(dept_a.id, dept_b.id, "a_under_b"),
            move(dept_b.id, dept_a.id, "b_under_a"),
        )

        failures = [key for key, value in outcomes.items() if value]
        assert len(failures) >= 1, "at least one mutual move must fail"

        # The surviving tree is acyclic and closure-consistent.
        async with sessionmaker() as db:
            from workmate_product.persistence.models import ScopeClosure

            parents = dict((await db.execute(
                select(Scope.id, Scope.parent_id),
            )).all())
            for start in (dept_a.id, dept_b.id, root_id):
                seen = set()
                current = start
                for _ in range(len(parents) + 1):
                    current = parents[current]
                    if current is None:
                        break
                    assert current not in seen or current == start, (
                        "scope parent chain must not cycle"
                    )
                    if current == start:
                        raise AssertionError("scope parent chain cycles")
                    seen.add(current)
                else:
                    raise AssertionError("scope parent chain too long")

            # Closure: every scope reaches itself at depth 0 only, and
            # ancestor chains match parent pointers.
            edges = (await db.scalars(
                select(ScopeClosure),
            )).all()
            self_edges = {
                edge.descendant_id for edge in edges if edge.depth == 0
            }
            assert self_edges == set(parents)
            for edge in edges:
                if edge.depth > 0:
                    assert edge.ancestor_id != edge.descendant_id


async def test_concurrent_same_name_create_allows_one():
    async with _setup_engine(_postgres_url()) as (engine, sessionmaker):
        admin = await _make_admin(sessionmaker, "names@test.dev")
        root_id = None
        async with sessionmaker() as db:
            root_id = (await _root_scope(db)).id

        from app.services.scope_management import (
            ScopeManagementError,
            ScopeManagementService,
        )

        outcomes: list[Exception | None] = []

        async def create():
            async with sessionmaker() as db:
                service = ScopeManagementService(db, admin.id)
                try:
                    await service.create_department(
                        parent_id=root_id, name="Engineering",
                    )
                    outcomes.append(None)
                except ScopeManagementError as error:
                    outcomes.append(error)

        await asyncio.gather(create(), create())

        successes = [o for o in outcomes if o is None]
        assert len(successes) == 1, "exactly one same-name create wins"
        failures = [o for o in outcomes if o is not None]
        assert all(
            error.code == "scope_name_taken" for error in failures
        )


async def _root_scope(db) -> Scope:
    return await db.scalar(
        select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
    )
