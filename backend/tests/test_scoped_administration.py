"""API security regression for bounded department administration."""
async def test_member_summary_counts_unique_people_and_respects_read_scope(client_and_app, auth_headers):
    http, _ = client_and_app
    _, parent, _, child = await organization(http, auth_headers)
    person, _ = await account(http, auth_headers, 'nested@test.dev', [parent, child])
    child_only, _ = await account(http, auth_headers, 'child-only@test.dev', [child])
    direct = (await http.get(f'/api/admin/scopes/{parent}/members', headers=auth_headers)).json()
    assert {u['id'] for u in direct['items']} == {person['id']}
    assert direct['counts'] == {'direct': 1, 'total': 2, 'limited': False}
    all_members = (await http.get(f'/api/admin/scopes/{parent}/members?include_descendants=true&limit=1', headers=auth_headers)).json()
    assert len(all_members['items']) == 1
    assert all_members['counts']['total'] == 2
    reader = await role(http, auth_headers, 'local_reader', ['membership.read'])
    _, headers = await account(http, auth_headers, 'reader@test.dev', [], [
        {'scope_id': parent, 'role_key': reader['key'], 'include_descendants': False}])
    limited = (await http.get(f'/api/admin/scopes/{parent}/members?include_descendants=true', headers=headers)).json()
    assert limited['counts'] == {'direct': 1, 'total': 1, 'limited': True}
    assert child_only['id'] not in {u['id'] for u in limited['items']}


async def test_root_default_lists_only_unassigned_without_removing_membership(client_and_app, auth_headers):
    http, _ = client_and_app
    root, _, _, child = await organization(http, auth_headers)
    assigned, _ = await account(http, auth_headers, 'assigned@test.dev', [child])
    unassigned, _ = await account(http, auth_headers, 'unassigned@test.dev', [])
    direct = (await http.get(f'/api/admin/scopes/{root}/members?q=assigned', headers=auth_headers)).json()
    assert assigned['id'] not in {u['id'] for u in direct['items']}
    assert unassigned['id'] in {u['id'] for u in direct['items']}
    all_members = (await http.get(f'/api/admin/scopes/{root}/members?include_descendants=true', headers=auth_headers)).json()
    assert assigned['id'] in {u['id'] for u in all_members['items']}
    assert all_members['counts']['direct'] == direct['counts']['direct']
    assert all_members['counts']['total'] == direct['counts']['direct'] + 1
    detail = (await http.get(f"/api/admin/users/{assigned['id']}", headers=auth_headers)).json()
    assert root in detail['memberships'] and child in detail['memberships']


async def organization(http, headers):
    root = (await http.get('/api/auth/me/permissions', headers=headers)).json()['root_scope_id']
    async def department(name, parent=root):
        response = await http.post('/api/admin/scopes', headers=headers, json={'name': name, 'parent_id': parent})
        assert response.status_code == 201, response.text
        return response.json()['id']
    finance, sales = await department('Finance'), await department('Sales')
    child = await department('Accounting', finance)
    return root, finance, sales, child


async def account(http, headers, email, scopes, bindings=None):
    response = await http.post('/api/admin/users', headers=headers, json={
        'email': email, 'display_name': email.split('@')[0], 'initial_password': 'testing-password-123',
        'must_change_password': False, 'membership_scope_ids': scopes, 'role_bindings': bindings or [],
    })
    assert response.status_code == 201, response.text
    user = response.json()
    login = await http.post('/api/auth/login', json={'email': email, 'password': 'testing-password-123'})
    assert login.status_code == 200, login.text
    return user, {'Authorization': f"Bearer {login.json()['access_token']}"}


async def role(http, headers, key, permissions, delegatable=True):
    response = await http.post('/api/admin/roles', headers=headers, json={
        'key': key, 'display_name': key, 'permission_keys': permissions, 'delegatable': delegatable,
    })
    assert response.status_code == 201, response.text
    return response.json()


async def test_member_removal_preserves_other_scopes_and_revokes_departed_roles(client_and_app, auth_headers):
    http, _ = client_and_app
    root, finance, sales, _ = await organization(http, auth_headers)
    editor = await role(http, auth_headers, 'finance_editor', ['agent.update'])
    user, _ = await account(http, auth_headers, 'employee@test.dev', [finance, sales], [
        {'scope_id': finance, 'role_key': editor['key']},
    ])
    url = f"/api/admin/scopes/{finance}/members/{user['id']}?expected_version={user['access_version']}"
    assert (await http.delete(url, headers=auth_headers)).status_code == 200
    assert (await http.delete(url, headers=auth_headers)).status_code == 409
    detail = (await http.get(f"/api/admin/users/{user['id']}", headers=auth_headers)).json()
    assert sales in detail['memberships'] and finance not in detail['memberships']
    assert not any(binding['role_key'] == editor['key'] for binding in detail['role_bindings'])
    effective = await http.get(f"/api/admin/users/{user['id']}/effective-access?scope_id={finance}", headers=auth_headers)
    assert effective.status_code == 200, effective.text
    assert 'agent.update' not in effective.json()['permissions']


async def test_delegate_cannot_cross_scope_or_expand_inheritance_and_role_ceiling(client_and_app, auth_headers):
    http, _ = client_and_app
    root, finance, sales, child = await organization(http, auth_headers)
    manager = await role(http, auth_headers, 'department_manager', [
        'membership.read', 'membership.manage', 'role.bind', 'agent.update',
    ])
    editor = await role(http, auth_headers, 'local_editor', ['agent.update'])
    delegate, delegate_headers = await account(http, auth_headers, 'delegate@test.dev', [finance], [
        {'scope_id': finance, 'role_key': manager['key'], 'include_descendants': False},
    ])
    employee, _ = await account(http, auth_headers, 'employee@test.dev', [finance])
    outsider, _ = await account(http, auth_headers, 'outsider@test.dev', [sales])
    assert (await http.get(f'/api/admin/scopes/{sales}/members', headers=delegate_headers)).status_code == 403
    assert (await http.get(f'/api/admin/scopes/{child}/members', headers=delegate_headers)).status_code == 403
    candidates = (await http.get(f'/api/admin/scopes/{finance}/member-candidates', headers=delegate_headers)).json()['items']
    assert outsider['id'] not in {u['id'] for u in candidates}
    assert (await http.put(f"/api/admin/scopes/{finance}/members/{outsider['id']}", headers=delegate_headers,
        json={'expected_version': outsider['access_version']})).status_code == 403
    url = f"/api/admin/scopes/{finance}/role-bindings/{employee['id']}/{editor['key']}"
    payload = {'expected_version': employee['access_version'], 'include_descendants': True}
    assert (await http.put(url, headers=delegate_headers, json=payload)).status_code == 403
    payload['include_descendants'] = False
    saved = await http.put(url, headers=delegate_headers, json=payload)
    assert saved.status_code == 200, saved.text
    expand = await http.patch(f"/api/admin/roles/{editor['id']}", headers=auth_headers,
        json={'expected_version': editor['version'], 'permission_keys': ['agent.update', 'agent.delete']})
    assert expand.status_code == 409, expand.text
    assert expand.json()['detail']['code'] == 'delegation_ceiling_exceeded'
    assert (await http.put(f"/api/admin/scopes/{finance}/role-bindings/{employee['id']}/admin",
        headers=delegate_headers, json={'expected_version': saved.json()['access_version']})).status_code == 403
    removed = await http.delete(f"/api/admin/scopes/{finance}/members/{employee['id']}?expected_version={saved.json()['access_version']}&retain_departure_roles=true", headers=delegate_headers)
    assert removed.status_code == 200, removed.text
    # Explicitly retained cross-department responsibilities must
    # still be revocable even though the employee is no longer a member.
    revoked = await http.delete(f"{url}?expected_version={removed.json()['access_version']}", headers=delegate_headers)
    assert revoked.status_code == 200, revoked.text


async def test_root_only_roles_rejected_in_departments_and_onboarding_rolls_back(client_and_app, auth_headers):
    http, _ = client_and_app
    _, finance, _, _ = await organization(http, auth_headers)
    response = await http.post('/api/admin/users', headers=auth_headers, json={
        'email': 'illegal@test.dev', 'display_name': 'Illegal', 'initial_password': 'testing-password-123',
        'role_bindings': [{'scope_id': finance, 'role_key': 'admin'}],
    })
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == 'root_permission_scope'
    assert (await http.get('/api/admin/users?q=illegal', headers=auth_headers)).json()['items'] == []
    response = await http.post('/api/admin/roles', headers=auth_headers, json={
        'key': 'invalid_delegate', 'display_name': 'Invalid', 'delegatable': True,
        'permission_keys': ['user.manage'],
    })
    assert response.status_code == 422


async def test_descendant_members_require_read_authority_not_only_parent(client_and_app, auth_headers):
    http, _ = client_and_app
    _, finance, _, child = await organization(http, auth_headers)
    reader = await role(http, auth_headers, 'local_reader', ['membership.read'])
    _, reader_headers = await account(http, auth_headers, 'reader@test.dev', [finance], [
        {'scope_id': finance, 'role_key': reader['key'], 'include_descendants': False},
    ])
    hidden, _ = await account(http, auth_headers, 'child@test.dev', [child])
    response = await http.get(f'/api/admin/scopes/{finance}/members?include_descendants=true', headers=reader_headers)
    assert response.status_code == 200
    assert hidden['id'] not in {u['id'] for u in response.json()['items']}


async def test_effective_access_reader_sees_only_target_actions_and_redacted_ancestor(client_and_app, auth_headers):
    http, _ = client_and_app
    root, finance, _, _ = await organization(http, auth_headers)
    reader = await role(http, auth_headers, 'scope_reader', ['membership.read'])
    _, reader_headers = await account(http, auth_headers, 'reader@test.dev', [finance], [
        {'scope_id': finance, 'role_key': reader['key']},
    ])
    admin, _ = await account(http, auth_headers, 'otheradmin@test.dev', [finance], [
        {'scope_id': root, 'role_key': 'admin', 'include_descendants': True},
    ])
    response = await http.get(f"/api/admin/users/{admin['id']}/effective-access?scope_id={finance}", headers=reader_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert 'agent.update' in body['permissions']
    assert 'user.manage' not in body['permissions']
    inherited = next(source for source in body['sources'] if 'agent.update' in source['permission_keys'])
    assert 'user.manage' not in inherited['permission_keys']
    assert inherited['scope_id'] is None
    assert inherited['role_key'] == 'inherited'


async def test_scope_directory_assignment_pages_and_root_candidates(client_and_app, auth_headers):
    http, _ = client_and_app
    _, finance, sales, _ = await organization(http, auth_headers)
    editor = await role(http, auth_headers, 'directory_editor', ['agent.update'])
    user, _ = await account(http, auth_headers, 'directory@test.dev', [sales])
    candidates = (await http.get(f'/api/admin/scopes/{finance}/member-candidates?q=directory', headers=auth_headers)).json()
    assert [item['id'] for item in candidates['items']] == [user['id']]
    member = await http.put(f"/api/admin/scopes/{finance}/members/{user['id']}", headers=auth_headers,
        json={'expected_version': user['access_version']})
    assert member.status_code == 200, member.text
    members = (await http.get(f'/api/admin/scopes/{finance}/members?q=directory&limit=1', headers=auth_headers)).json()
    assert members['items'][0]['membership_scope_ids'] == [finance]
    roles = (await http.get(f'/api/admin/scopes/{finance}/assignable-roles', headers=auth_headers)).json()
    assert editor['key'] in {item['key'] for item in roles}
    assert 'admin' not in {item['key'] for item in roles}
    saved = await http.put(f"/api/admin/scopes/{finance}/role-bindings/{user['id']}/{editor['key']}",
        headers=auth_headers, json={'expected_version': member.json()['access_version'], 'include_descendants': True})
    assert saved.status_code == 200, saved.text
    bindings = (await http.get(f'/api/admin/scopes/{finance}/role-bindings', headers=auth_headers)).json()
    assert bindings['items'][0]['user_id'] == user['id']
    references = (await http.get(f"/api/admin/roles/{editor['id']}/bindings", headers=auth_headers)).json()
    assert references['items'][0]['scope_id'] == finance
    templates = await http.get('/api/admin/roles/templates', headers=auth_headers)
    assert templates.status_code == 200 and templates.json()


async def test_department_manager_access_put_omits_roles_and_keeps_organization_identity(client_and_app, auth_headers):
    http, _ = client_and_app
    root, finance, sales, _ = await organization(http, auth_headers)
    manager_role = await role(http, auth_headers, 'membership_editor', ['membership.manage', 'user.read'], False)
    _, manager_headers = await account(http, auth_headers, 'manager@test.dev', [], [
        {'scope_id': root, 'role_key': manager_role['key']}])
    employee, _ = await account(http, auth_headers, 'employee@test.dev', [finance])
    response = await http.put(f"/api/admin/users/{employee['id']}/access", headers=manager_headers,
        json={'membership_scope_ids': [sales], 'expected_version': employee['access_version']})
    assert response.status_code == 200, response.text
    assert set(response.json()['memberships']) == {root, sales}
    assert response.json()['role_bindings'] == []
    denied = await http.put(f"/api/admin/users/{employee['id']}/access", headers=manager_headers,
        json={'membership_scope_ids': [finance], 'expected_version': response.json()['access_version'],
              'role_bindings': [{'scope_id': root, 'role_key': 'admin'}]})
    assert denied.status_code == 403
    unchanged = (await http.get(f"/api/admin/users/{employee['id']}", headers=auth_headers)).json()
    assert set(unchanged['memberships']) == {root, sales}
    assert unchanged['role_bindings'] == []
    forbidden = await http.delete(f"/api/admin/scopes/{root}/members/{employee['id']}", headers=auth_headers,
        params={'expected_version': unchanged['access_version']})
    assert forbidden.status_code == 422
    assert forbidden.json()['detail']['code'] == 'organization_membership_managed'


async def test_user_detail_and_access_response_include_role_names_for_member_manager(client_and_app, auth_headers):
    http, _ = client_and_app
    root, finance, sales, _ = await organization(http, auth_headers)
    manager_role = await role(http, auth_headers, 'membership_editor', ['membership.manage', 'user.read'], False)
    _, manager_headers = await account(http, auth_headers, 'manager@test.dev', [], [
        {'scope_id': root, 'role_key': manager_role['key']}])
    editor = await http.post('/api/admin/roles', headers=auth_headers, json={
        'key': 'agent_editor', 'display_name': 'Finance assistant administrator', 'permission_keys': ['agent.update'],
    })
    assert editor.status_code == 201, editor.text
    employee, _ = await account(http, auth_headers, 'employee@test.dev', [finance], [
        {'scope_id': finance, 'role_key': 'agent_editor'}])
    detail = await http.get(f"/api/admin/users/{employee['id']}", headers=manager_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()['departments'] == [{'id': finance, 'name': 'Finance'}]
    assert detail.json()['roles'][0]['role_name'] == 'Finance assistant administrator'
    assert detail.json()['roles'][0]['scope_id'] == finance
    assert (await http.get(f'/api/admin/scopes/{finance}/assignable-roles', headers=manager_headers)).status_code == 403
    # Adding another department preserves the existing responsibility without role.bind.
    updated = await http.put(f"/api/admin/users/{employee['id']}/access", headers=manager_headers,
        json={'membership_scope_ids': [finance, sales], 'expected_version': detail.json()['access_version']})
    assert updated.status_code == 200, updated.text
    assert {item['id'] for item in updated.json()['departments']} == {finance, sales}
    assert updated.json()['roles'][0]['role_name'] == 'Finance assistant administrator'
