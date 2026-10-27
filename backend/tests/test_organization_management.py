"""Organization administration: atomic saves and concurrency boundaries."""


async def test_onboarding_is_atomic_and_access_is_versioned(client_and_app, auth_headers):
    http, _ = client_and_app
    root = (await http.get('/api/auth/me/permissions', headers=auth_headers)).json()['root_scope_id']
    department = (await http.post('/api/admin/scopes', headers=auth_headers, json={
        'parent_id': root, 'name': 'Finance',
    })).json()['id']
    body = {'email': 'onboard@test.dev', 'display_name': 'New user',
            'initial_password': 'initial-password-123', 'membership_scope_ids': [department],
            'role_bindings': [{'scope_id': department, 'role_key': 'missing'}]}
    assert (await http.post('/api/admin/users', headers=auth_headers, json=body)).status_code == 422
    assert (await http.get('/api/admin/users?q=onboard', headers=auth_headers)).json()['items'] == []
    body['role_bindings'] = []
    response = await http.post('/api/admin/users', headers=auth_headers, json=body)
    assert response.status_code == 201, response.text
    user = response.json()
    detail = (await http.get(f"/api/admin/users/{user['id']}", headers=auth_headers)).json()
    assert department in detail['memberships']
    access = {'membership_scope_ids': [department, root], 'role_bindings': [],
              'expected_version': detail['access_version']}
    url = f"/api/admin/users/{user['id']}/access"
    assert (await http.put(url, headers=auth_headers, json=access)).status_code == 200
    assert (await http.put(url, headers=auth_headers, json=access)).status_code == 409


async def test_role_atomic_save_rejects_stale_version(client_and_app, auth_headers):
    http, _ = client_and_app
    role = (await http.post('/api/admin/roles', headers=auth_headers, json={
        'key': 'finance_editor', 'display_name': 'Finance editor',
        'permission_keys': ['agent.create'],
    })).json()
    url = f"/api/admin/roles/{role['id']}"
    body = {'display_name': 'Changed', 'permission_keys': ['not.real'],
            'expected_version': role['version']}
    assert (await http.patch(url, headers=auth_headers, json=body)).status_code == 422
    unchanged = next(r for r in (await http.get('/api/admin/roles', headers=auth_headers)).json() if r['id'] == role['id'])
    assert unchanged['display_name'] == 'Finance editor'
    body['permission_keys'] = ['agent.update']
    saved = await http.patch(url, headers=auth_headers, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['permission_keys'] == ['agent.update']
    assert (await http.patch(url, headers=auth_headers, json=body)).status_code == 409


async def test_self_profile_edit_invalidates_admin_baseline(client_and_app, auth_headers):
    http, _ = client_and_app
    me = (await http.get('/api/auth/me', headers=auth_headers)).json()
    url = f"/api/admin/users/{me['id']}"
    baseline = (await http.get(url, headers=auth_headers)).json()['profile_version']
    assert (await http.patch('/api/auth/me', headers=auth_headers, json={'display_name': 'Self edited'})).status_code == 200
    response = await http.patch(url, headers=auth_headers, json={
        'display_name': 'Stale admin edit', 'expected_version': baseline,
    })
    assert response.status_code == 409
