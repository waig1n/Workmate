"""Personal Schedule HTTP authorization and server-owned execution policy."""
import pytest


def body(agent_id, **changes):
    return {'name': 'Morning report', 'agent_id': agent_id, 'prompt': 'Prepare a report',
            'timezone': 'Asia/Shanghai', 'rule': {'kind': 'daily', 'time': '09:00'}, **changes}


async def test_create_schedule_requires_authentication(client_and_app):
    http, _ = client_and_app
    response = await http.post('/api/schedules', json=body('missing'))
    assert response.status_code in {401, 403}


async def test_schedule_and_history_hidden_from_other_user(client_and_app, auth_headers, seeded_ids):
    http, _ = client_and_app
    agent_id, _ = seeded_ids
    created = await http.post('/api/schedules', json=body(agent_id), headers=auth_headers)
    assert created.status_code == 201, created.text
    schedule_id = created.json()['id']
    registered = await http.post('/api/auth/register', json={
        'email': 'other-schedule@test.dev', 'password': 'password123', 'display_name': 'Other'})
    assert registered.status_code == 200, registered.text
    other = {'Authorization': 'Bearer ' + registered.json()['access_token']}
    for path in (f'/api/schedules/{schedule_id}', f'/api/schedules/{schedule_id}/occurrences'):
        response = await http.get(path, headers=other)
        assert response.status_code == 404, response.text
    response = await http.patch(f'/api/schedules/{schedule_id}',
                                json={**body(agent_id), 'version': created.json()['version']}, headers=other)
    assert response.status_code == 404, response.text
    own = await http.get(f'/api/schedules/{schedule_id}', headers=auth_headers)
    assert own.status_code == 200 and own.json()['name'] == 'Morning report'


@pytest.mark.parametrize('extra', [{'owner_user_id': 'other'}, {'origin': 'schedule'}, {'approval_profile': 'full_access'}])
async def test_schedule_http_rejects_forged_execution_authority(client_and_app, auth_headers, seeded_ids, extra):
    http, _ = client_and_app
    agent_id, _ = seeded_ids
    response = await http.post('/api/schedules', json=body(agent_id, **extra), headers=auth_headers)
    assert response.status_code == 422, response.text


async def test_interactive_task_http_rejects_system_unattended_policy(client_and_app, auth_headers, seeded_ids):
    http, _ = client_and_app
    _, conversation_id = seeded_ids
    response = await http.post(f'/api/tasks/conversations/{conversation_id}/tasks',
                              json={'message': 'Run', 'approval_profile': 'unattended'}, headers=auth_headers)
    assert response.status_code == 422, response.text


async def test_schedule_http_versioned_pause_resume_delete(client_and_app, auth_headers, seeded_ids):
    http, _ = client_and_app
    agent_id, _ = seeded_ids
    created = await http.post('/api/schedules', json=body(agent_id), headers=auth_headers)
    assert created.status_code == 201, created.text
    schedule = created.json()
    path = '/api/schedules/' + schedule['id']
    paused = await http.post(path + '/pause', json={'version': schedule['version']}, headers=auth_headers)
    assert paused.status_code == 200 and paused.json()['status'] == 'paused', paused.text
    stale = await http.post(path + '/resume', json={'version': schedule['version']}, headers=auth_headers)
    assert stale.status_code == 409, stale.text
    resumed = await http.post(path + '/resume', json={'version': paused.json()['version']}, headers=auth_headers)
    assert resumed.status_code == 200 and resumed.json()['status'] == 'active', resumed.text
    deleted = await http.delete(path + f"?version={resumed.json()['version']}", headers=auth_headers)
    assert deleted.status_code == 204, deleted.text
    assert (await http.get(path, headers=auth_headers)).status_code == 404


async def test_time_preview_returns_server_calculated_instants(client_and_app, auth_headers):
    from datetime import datetime, timezone
    http, _ = client_and_app
    response = await http.post('/api/schedules/preview', json={
        'timezone': 'Asia/Shanghai', 'rule': {'kind': 'weekly', 'time': '09:00', 'weekday': 0}}, headers=auth_headers)
    assert response.status_code == 200, response.text
    times = [datetime.fromisoformat(value) for value in response.json()['times']]
    assert len(times) == 3 and times == sorted(times)
    assert all(value > datetime.now(timezone.utc) for value in times)
    from zoneinfo import ZoneInfo
    assert all(value.astimezone(ZoneInfo('Asia/Shanghai')).weekday() == 0 for value in times)

async def test_notification_targets_requires_authentication(client_and_app):
    http,_=client_and_app
    response=await http.get('/api/schedules/notification-targets')
    assert response.status_code in {401,403}

async def test_notification_targets_static_route_and_retry_validation(client_and_app,auth_headers):
    http,_=client_and_app
    response=await http.get('/api/schedules/notification-targets',headers=auth_headers)
    assert response.status_code==200,response.text
    assert response.json()=={'items':[]}
    path='/api/schedules/missing/occurrences/missing/notification/retry'
    response=await http.post(path,json={'confirm_duplicates':'true'},headers=auth_headers)
    assert response.status_code==422
    response=await http.post(path,json={},headers=auth_headers)
    assert response.status_code==404
