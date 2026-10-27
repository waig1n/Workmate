"""Skill instructions remain inside Runtime, never in client tool results."""
import json
import uuid

import pytest
from sqlalchemy import select

from workmate_product.projections.translator import TranslationState, translate_event
from workmate_product.persistence.models import Task, TaskEvent

SECRET = '# Private Skill instructions: secret-workflow-849'


def _events(status='success'):
    return [
        {'type': 'REPLY_START', 'reply_id': 'reply'},
        {'type': 'TOOL_CALL_START', 'reply_id': 'reply', 'tool_call_id': 'skill-call', 'tool_call_name': 'Skill'},
        {'type': 'TOOL_RESULT_TEXT_DELTA', 'reply_id': 'reply', 'tool_call_id': 'skill-call', 'delta': SECRET},
        {'type': 'TOOL_RESULT_DATA', 'reply_id': 'reply', 'tool_call_id': 'skill-call', 'data': {'markdown': SECRET}},
        {'type': 'TOOL_RESULT_END', 'reply_id': 'reply', 'tool_call_id': 'skill-call', 'state': status, 'content': SECRET},
    ]


@pytest.mark.parametrize('status', ['success', 'error'])
def test_skill_result_is_safe_status_only_and_read_remains_visible(status):
    state = TranslationState()
    output = [item for raw in _events(status) for item in translate_event(raw, 'conversation', state)]
    assert SECRET not in json.dumps(output)
    assert state.tool_result_buffers == {}
    assert any(item.get('value', {}).get('state') == status for item in output)
    assert any(item.get('toolCallName') == 'Skill' for item in output)
    translate_event({'type': 'TOOL_CALL_START', 'reply_id': 'reply', 'tool_call_id': 'read', 'tool_call_name': 'Read'}, 'conversation', state)
    ordinary = translate_event({'type': 'TOOL_RESULT_TEXT_DELTA', 'reply_id': 'reply', 'tool_call_id': 'read', 'delta': 'ordinary file content'}, 'conversation', state)
    assert 'ordinary file content' in json.dumps(ordinary)


async def test_legacy_skill_audit_payload_is_redacted_without_deleting_history(
    client_and_app, auth_headers, seeded_ids, db_session,
):
    http, _ = client_and_app
    _, conversation_id = seeded_ids
    task = Task(id=str(uuid.uuid4()), conversation_id=conversation_id, status='completed', title='Legacy task')
    db_session.add(task)
    await db_session.flush()
    for seq, kind, payload in [
        (1, 'tool_call', {'toolCallId': 'skill', 'tool': 'Skill'}),
        (2, 'tool_result', {'toolCallId': 'skill', 'status': 'success', 'content': SECRET}),
        (3, 'tool_result', {'toolCallId': 'named', 'tool': 'Skill', 'status': 'error', 'browser_projection': {'tool': 'Skill', 'content': SECRET}, 'content': SECRET}),
        (4, 'tool_result', {'toolCallId': 'read', 'tool': 'Read', 'content': 'ordinary content'}),
        (5, 'tool_call', {'browser_projection': {'toolCallId': 'nested', 'tool': 'Skill'}}),
        (6, 'tool_result', {'browser_projection': {'toolCallId': 'nested', 'status': 'success', 'content': SECRET}}),
        (7, 'tool_result', {'browser_projection': {'tool_call_id': 'nested-named', 'tool_call_name': 'Skill', 'status': 'error', 'content': SECRET}}),
    ]:
        db_session.add(TaskEvent(task_id=task.id, conversation_id=conversation_id, seq=seq, event_type=kind, payload=payload))
    await db_session.commit()
    result = await http.get(f'/api/tasks/{task.id}/events?event_type=tool_result&offset=0&limit=100', headers=auth_headers)
    assert result.status_code == 200, result.text
    assert SECRET not in result.text
    assert 'ordinary content' in result.text
    assert len(result.json()) == 5
    assert result.json()[-2]['payload']['status'] == 'success'
    assert result.json()[-1]['payload']['tool_call_id'] == 'nested-named'
    stored = (await db_session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))).all()
    assert any(SECRET in json.dumps(event.payload) for event in stored)


def test_reconnect_without_named_start_does_not_emit_unclassified_skill_content():
    state = TranslationState()
    for raw in _events()[2:]:
        assert translate_event(raw, 'conversation', state) == []
    assert state.tool_result_buffers == {}
