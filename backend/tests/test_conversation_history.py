"""Bounded, owner-scoped conversation history with stable pagination."""
import base64
import json
from datetime import datetime, timezone

import pytest

from workmate_product.persistence.models import Conversation


@pytest.mark.asyncio
async def test_history_cursor_search_and_recent_limit(
    client_and_app, auth_headers, seeded_ids, db_session,
):
    http, _ = client_and_app
    agent_id, conv_id = seeded_ids
    original = await db_session.get(Conversation, conv_id)
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    original.updated_at = timestamp
    for index in range(5):
        db_session.add(Conversation(
            id=f"history-{index}", user_id=original.user_id,
            agent_id=agent_id, title=f"报告 {index}", updated_at=timestamp,
        ))
    await db_session.commit()
    first = await http.get('/api/conversations/history', params={
        'limit': 2, 'q': '报告',
    }, headers=auth_headers)
    assert first.status_code == 200, first.text
    page = first.json()
    assert [c['id'] for c in page['items']] == ['history-4', 'history-3']
    second = await http.get('/api/conversations/history', params={
        'limit': 2, 'q': '报告', 'cursor': page['next_cursor'],
    }, headers=auth_headers)
    assert [c['id'] for c in second.json()['items']] == ['history-2', 'history-1']
    last = await http.get('/api/conversations/history', params={
        'limit': 2, 'q': '报告', 'cursor': second.json()['next_cursor'],
    }, headers=auth_headers)
    assert [c['id'] for c in last.json()['items']] == ['history-0']
    assert last.json()['next_cursor'] is None
    recent = await http.get('/api/conversations?limit=2', headers=auth_headers)
    assert len(recent.json()) == 2
    literal = await http.get('/api/conversations/history?q=%25', headers=auth_headers)
    assert literal.json()['items'] == []
    for suffix in ('?cursor=broken', '?limit=101', '?limit=0'):
        response = await http.get('/api/conversations/history' + suffix, headers=auth_headers)
        assert response.status_code == 422
    for malformed in (None, 3, {}, {"2026-01-01": 1, "id": 2}, ["2026-01-01", None], ["99999-01-01", "id"], ["bad-timezone", "id"]):
        cursor = base64.urlsafe_b64encode(json.dumps(malformed).encode()).decode()
        response = await http.get('/api/conversations/history', params={
            'cursor': cursor,
        }, headers=auth_headers)
        assert response.status_code == 422
    other = await http.post('/api/auth/register', json={
        'email': 'other-history@test.dev', 'password': 'password123',
        'display_name': 'Other',
    })
    response = await http.get('/api/conversations/history', headers={
        'Authorization': f"Bearer {other.json()['access_token']}",
    })
    assert response.json()['items'] == []
