"""New Skill ingestion always belongs to the organization library."""

import uuid
from product_catalog_fakes import skill_archive, seed_skill_artifact

from sqlalchemy import select

from workmate_product.persistence.models import (
    CapabilityInstallation,
    CapabilityType,
    SecurityAuditEvent,
    Role,
    RoleBinding,
    ScopeKind,
)
from app.services.bootstrap import create_scope, get_root_scope
from test_capabilities import SKILL_CARD, _register


async def test_skill_import_without_scope_and_builtin_lifecycle(
    client_and_app, auth_headers, fake_client, db_session,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    fake_client.skill_catalog = [SKILL_CARD]
    response = await http.post('/api/skills', headers=auth_headers,
        json={'hub_id': 'clawhub', 'card_id': 'c-1'})
    assert response.status_code == 201, response.text
    assert response.json()['scope_id'] == root.id
    assert not response.json()['usable']
    duplicate = await http.post('/api/skills', headers=auth_headers,
        json={'hub_id': 'clawhub', 'card_id': 'c-1', 'name': 'another-name'})
    assert duplicate.status_code == 409, duplicate.text
    builtin_request = await http.post('/api/skills', headers=auth_headers,
        json={'hub_id': 'workmate-builtin', 'card_id': 'builtin'})
    assert builtin_request.status_code == 422, builtin_request.text
    builtin = CapabilityInstallation(id=str(uuid.uuid4()), scope_id=root.id,
        capability_type=CapabilityType.SKILL,
         name='builtin',
        metadata_={'hub_id': 'workmate-builtin', 'card_id': 'builtin'})
    db_session.add(builtin)
    await db_session.commit()
    for enabled in (False, True):
        patched = await http.patch('/api/skills/' + builtin.id, headers=auth_headers, json={'enabled': enabled})
        assert patched.status_code == 200, patched.text
        assert patched.json()['enabled'] is enabled
    actor_id = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    events = (await db_session.scalars(select(SecurityAuditEvent).where(
        SecurityAuditEvent.event_type == 'capability.update',
    ))).all()
    assert len(events) == 2
    assert {event.metadata_['enabled'] for event in events} == {True, False}
    assert all(event.actor_user_id == actor_id and event.metadata_['capability_id'] == builtin.id for event in events)
    deleted = await http.delete('/api/skills/' + builtin.id, headers=auth_headers)
    assert deleted.status_code == 409, deleted.text


async def test_skill_upload_defaults_root_and_rejects_department_even_for_root_admin(
    client_and_app, auth_headers, db_session,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    department = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Department', parent=root)
    await db_session.commit()
    response = await http.post('/api/skills/upload', headers=auth_headers,
        files={'archive': ('skill.zip', skill_archive(), 'application/zip')})
    assert response.status_code == 201, response.text
    assert response.json()['scope_id'] == root.id
    for path, kwargs in (
        ('/api/skills/upload', {'data': {'scope_id': department.id}, 'files': {'archive': ('other.zip', skill_archive())}}),
        ('/api/skills', {'json': {'scope_id': department.id, 'hub_id': 'workmate-builtin', 'card_id': 'x'}}),
    ):
        assert (await http.post(path, headers=auth_headers, **kwargs)).status_code == 422


async def test_department_manager_cannot_add_to_organization_library(
    client_and_app, auth_headers, db_session,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    department = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Managed', parent=root)
    await db_session.commit()
    headers, user_id = await _register(http, 'dept-library@example.com')
    role = await db_session.scalar(select(Role).where(Role.key == 'admin'))
    db_session.add(RoleBinding(user_id=user_id, role_id=role.id, scope_id=department.id, include_descendants=True))
    await db_session.commit()
    response = await http.post('/api/skills/upload', headers=headers,
        files={'archive': ('skill.zip', skill_archive(), 'application/zip')})
    assert response.status_code == 403, response.text
    uploaded = await http.post('/api/skills/upload', headers=auth_headers,
        files={'archive': ('admin.zip', skill_archive(), 'application/zip')})
    assert uploaded.status_code == 201, uploaded.text
    denied = await http.patch('/api/skills/' + uploaded.json()['id'], headers=headers, json={'enabled': False})
    assert denied.status_code == 403, denied.text
    invalid = await http.patch('/api/skills/' + uploaded.json()['id'], headers=auth_headers, json={'enabled': False, 'scope_id': department.id})
    assert invalid.status_code == 422, invalid.text


async def test_same_name_skill_sources_bind_individually_but_never_together(
    client_and_app, auth_headers, db_session,
):
    from sqlalchemy import func
    from workmate_product.persistence.models import Agent, ResourceShare

    http, _ = client_and_app
    root = await get_root_scope(db_session)
    skills = [CapabilityInstallation(
        id=str(uuid.uuid4()), scope_id=root.id, capability_type=CapabilityType.SKILL,

        name='same-skill', metadata_={'hub_id': hub, 'card_id': 'same-skill'},
    ) for principal, hub in [('workmate-scope', 'upload'), ('workmate-builtin', 'workmate-builtin')]]
    db_session.add_all(skills)
    await db_session.flush()
    for skill in skills:
        await seed_skill_artifact(db_session, skill)
    db_session.add_all([ResourceShare(id=str(uuid.uuid4()), capability_id=skill.id,
        target_scope_id=root.id, include_descendants=True) for skill in skills])
    await db_session.commit()
    for index, skill in enumerate(skills):
        response = await http.post('/api/agents', headers=auth_headers, json={
            'slug': f'one-source-{index}', 'display_name': 'One source',
            'scope_id': root.id, 'skill_binding_ids': [skill.id],
        })
        assert response.status_code == 201, response.text
    before = await db_session.scalar(select(func.count()).select_from(Agent))
    response = await http.post('/api/agents', headers=auth_headers, json={
        'slug': 'ambiguous-skill', 'display_name': 'Ambiguous skill', 'scope_id': root.id,
        'skill_binding_ids': [skill.id for skill in skills],
    })
    assert response.status_code == 422, response.text
    assert 'same name' in response.json()['detail']
    assert await db_session.scalar(select(func.count()).select_from(Agent)) == before
