"""The schedule-only approval profile cannot be submitted or elevated by chat."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.api.schemas import TaskCreate, ConfirmBatchRequest
from app.api.routers import tasks
from app.approval import permission_mode_for


def test_interactive_task_cannot_request_unattended():
    with pytest.raises(ValidationError, match='unattended'):
        TaskCreate(message='hello', approval_profile='unattended')


def test_unattended_maps_to_dont_ask():
    assert permission_mode_for('unattended') == 'dont_ask'


async def test_shared_interactive_security_check_rejects_unattended():
    with pytest.raises(HTTPException) as caught:
        await tasks._defer_initial_security_check(None, None, 'unattended')
    assert caught.value.status_code == 422


@pytest.mark.parametrize('endpoint', ['full_access', 'bypass_remaining'])
async def test_http_full_access_rejects_unattended_before_resume(monkeypatch, endpoint):
    monkeypatch.setattr(tasks, '_get_owned_task', AsyncMock(return_value=(
        SimpleNamespace(approval_profile='unattended'), SimpleNamespace())))
    body = ConfirmBatchRequest(decisions=[{'approved':True,'tool_call_id':'call'}], decision_version='version')
    with pytest.raises(HTTPException) as caught:
        if endpoint == 'full_access':
            await tasks.enable_full_access('t', body, None, None, None)
        else:
            await tasks.confirm_task('t', body, None, True, None, None)
    assert caught.value.status_code == 409
    assert 'unattended' in str(caught.value.detail).lower()
