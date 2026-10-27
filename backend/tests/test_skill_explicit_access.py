"""Skill ownership never grants use; sharing is the only admission path."""
import uuid
from product_catalog_fakes import skill_archive

import pytest
from sqlalchemy import select, text

from workmate_product.persistence.models import (
    CapabilityInstallation,
    CapabilityType,
    ResourceShare,
    ScopeKind,
    ScopeMembership,
)
from app.services.access_decision import AccessDecisionService
from app.services.bootstrap import create_scope, get_root_scope


@pytest.mark.asyncio
async def test_upload_is_private_until_department_or_organization_share(
    client_and_app, auth_headers, db_session,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    department = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Recipient', parent=root)
    child = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Child', parent=department)
    await db_session.commit()
    registered = (await http.post('/api/auth/register', json={
        'email': 'skill-use@example.com', 'password': 'password123', 'display_name': 'Member',
    })).json()
    headers = {'Authorization': f"Bearer {registered['access_token']}"}
    member = (await http.get('/api/auth/me', headers=headers)).json()
    db_session.add(ScopeMembership(scope_id=department.id, user_id=member['id']))
    await db_session.commit()
    uploaded = await http.post('/api/skills/upload', headers=auth_headers,
                               data={'scope_id': root.id}, files={'archive': ('skill.zip', skill_archive(), 'application/zip')})
    assert uploaded.status_code == 201, uploaded.text
    skill = uploaded.json()
    assert skill['usable'] is False
    assert skill['id'] not in {item['id'] for item in (await http.get('/api/skills', headers=headers)).json()['items']}
    assert (await http.get(f"/api/skills/{skill['id']}/description", headers=headers)).status_code == 404
    access = AccessDecisionService(db_session)
    assert skill['id'] not in await access.bindable_capability_ids(root)
    assert skill['id'] not in await access.bindable_capability_ids(department)
    # Even a manager cannot attach an unpublished Skill to an Agent.
    denied = await http.post('/api/agents', headers=auth_headers, json={
        'slug': 'unpublished', 'display_name': 'Unpublished', 'scope_id': root.id,
        'skill_binding_ids': [skill['id']],
    })
    assert denied.status_code == 422, denied.text
    url = f"/api/sharing/skills/{skill['id']}"
    for target in (department, root):
        body = {'target_scope_id': target.id}
        preview = await http.post(url + '/preview', headers=auth_headers, json=body)
        assert preview.status_code == 200, preview.text
        applied = await http.post(url, headers=auth_headers, json={**body, 'confirmation': preview.json()['confirmation']})
        assert applied.status_code == 200, applied.text
        assert skill['id'] in await access.accessible_capability_ids(member['id'])
        if target == department:
            assert skill['id'] not in await access.bindable_capability_ids(child)
        else:
            future = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Future', parent=root)
            await db_session.commit()
            assert skill['id'] in await access.bindable_capability_ids(future)
    # Removing one grant leaves the independent department grant intact.
    assert (await http.delete(url + '/' + root.id, headers=auth_headers)).status_code == 204
    assert skill['id'] in await access.accessible_capability_ids(member['id'])
    assert skill['id'] not in await access.bindable_capability_ids(child)
    assert (await http.delete(url + '/' + department.id, headers=auth_headers)).status_code == 204
    assert skill['id'] not in await access.accessible_capability_ids(member['id'])


@pytest.mark.asyncio
async def test_capability_ownership_is_not_use_and_mcp_descendants_require_explicit_grant(client_and_app, db_session):
    root = await get_root_scope(db_session)
    department = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Owned', parent=root)
    records = [CapabilityInstallation(
        id=str(uuid.uuid4()), capability_type=kind, scope_id=root.id,
          name=kind,
    ) for kind in (CapabilityType.SKILL, CapabilityType.MCP)]
    db_session.add_all(records)
    await db_session.commit()
    access = AccessDecisionService(db_session)
    for scope in (root, department):
        assert await access.bindable_capability_ids(scope) == set()
    mcp_grant = ResourceShare(id=str(uuid.uuid4()), capability_id=records[1].id,
        target_scope_id=root.id, include_descendants=False)
    db_session.add(mcp_grant)
    await db_session.commit()
    assert await access.bindable_capability_ids(root) == {records[1].id}
    assert await access.bindable_capability_ids(department) == set()
    mcp_grant.include_descendants = True
    await db_session.commit()
    for scope in (root, department):
        assert await access.bindable_capability_ids(scope) == {records[1].id}
    # Database defaults must also fail closed for inserts without ORM defaults.
    await db_session.execute(text(
        'INSERT INTO resource_shares (id, capability_id, target_scope_id) VALUES (:id, :skill, :scope)'
    ), {'id': str(uuid.uuid4()), 'skill': records[0].id, 'scope': root.id})
    await db_session.commit()
    assert records[0].id in await access.bindable_capability_ids(root)
    assert records[0].id not in await access.bindable_capability_ids(department)


@pytest.mark.asyncio
async def test_department_can_receive_organization_library_skill(
    client_and_app, auth_headers, db_session,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    department = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Owner', parent=root)
    await db_session.commit()
    uploaded = await http.post('/api/skills/upload', headers=auth_headers,
        data={'scope_id': root.id}, files={'archive': ('skill.zip', skill_archive(), 'application/zip')})
    assert uploaded.status_code == 201, uploaded.text
    url = f"/api/sharing/skills/{uploaded.json()['id']}"
    targets = (await http.get(url, headers=auth_headers)).json()['targets']
    assert {root.id, department.id} <= {item['id'] for item in targets}
    body = {'target_scope_id': department.id}
    preview = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    assert (await http.post(url, headers=auth_headers, json={**body, 'confirmation': preview['confirmation']})).status_code == 200
    grant = await db_session.scalar(select(ResourceShare).where(ResourceShare.capability_id == uploaded.json()['id']))
    assert grant.target_scope_id == department.id


@pytest.mark.asyncio
async def test_legacy_organization_grant_upgrade_is_explicit_idempotent_and_audited(
    client_and_app, auth_headers, db_session,
):
    from workmate_product.persistence.models import SecurityAuditEvent
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    department = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='New department', parent=root)
    skill = CapabilityInstallation(id=str(uuid.uuid4()), capability_type='skill', scope_id=root.id,
          name='legacy')
    db_session.add(skill)
    await db_session.flush()
    legacy = ResourceShare(id=str(uuid.uuid4()), capability_id=skill.id, target_scope_id=root.id)
    db_session.add(legacy)
    await db_session.commit()
    access = AccessDecisionService(db_session)
    assert skill.id not in await access.bindable_capability_ids(department)
    url = f'/api/sharing/skills/{skill.id}'
    state = (await http.get(url, headers=auth_headers)).json()
    assert state['grants'] == [{'target_scope_id': root.id, 'include_descendants': False, 'allow_download': False}]
    body = {'target_scope_id': root.id}
    assert (await http.post(url, headers=auth_headers, json=body)).status_code == 409
    preview = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    assert preview['include_descendants'] is True
    await db_session.refresh(legacy)
    assert legacy.include_descendants is False
    for _ in range(2):
        preview = (await http.post(url + "/preview", headers=auth_headers, json=body)).json()
        result = await http.post(url, headers=auth_headers, json={**body, 'confirmation': preview['confirmation']})
        assert result.status_code == 200, result.text
    await db_session.refresh(legacy)
    assert legacy.include_descendants is True
    assert skill.id in await access.bindable_capability_ids(department)
    grants = (await db_session.scalars(select(ResourceShare).where(ResourceShare.capability_id == skill.id))).all()
    assert len(grants) == 1
    audits = (await db_session.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == 'resource.share'))).all()
    assert len(audits) == 1
    assert audits[0].metadata_['include_descendants'] is True


@pytest.mark.asyncio
async def test_skill_sharing_rejects_foreign_and_personal_targets(
    client_and_app, auth_headers, db_session,
):
    from workmate_product.persistence.models import Scope
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    personal = await db_session.scalar(select(Scope).where(Scope.kind == ScopeKind.PERSONAL))
    foreign_root = await create_scope(db_session, kind=ScopeKind.ORGANIZATION, name='Foreign')
    await db_session.commit()
    uploaded = await http.post('/api/skills/upload', headers=auth_headers,
        data={'scope_id': root.id}, files={'archive': ('skill.zip', skill_archive(), 'application/zip')})
    assert uploaded.status_code == 201, uploaded.text
    url = f"/api/sharing/skills/{uploaded.json()['id']}"
    for body in ({'target_scope_id': foreign_root.id}, {'target_scope_id': personal.id}):
        result = await http.post(url + '/preview', headers=auth_headers, json=body)
        assert result.status_code == 422, result.text
    assert not (await db_session.scalars(select(ResourceShare))).all()
