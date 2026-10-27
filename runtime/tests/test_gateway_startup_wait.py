"""Gateway readiness stays responsive while retaining nonce-bound health checks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from workmate_runtime.mcp.workspace import WorkerMCPWorkspace


@pytest.mark.asyncio
async def test_ready_gateway_is_not_hidden_behind_long_backoff(monkeypatch):
    workspace=WorkerMCPWorkspace(workspace_id='startup')
    workspace._gateway=SimpleNamespace(health=AsyncMock(side_effect=[False,False,False,True]))
    workspace._gateway_process=SimpleNamespace(returncode=None)
    waits=[]
    async def sleep(seconds):waits.append(seconds)
    monkeypatch.setattr('workmate_runtime.mcp.workspace.asyncio.sleep',sleep)
    assert await workspace._wait_gateway_health(30)
    assert len(waits)==3 and max(waits)<=.1


@pytest.mark.asyncio
async def test_unresponsive_health_cannot_exceed_startup_deadline():
    workspace=WorkerMCPWorkspace(workspace_id='deadline')
    async def health():await asyncio.Event().wait()
    workspace._gateway=SimpleNamespace(health=health)
    workspace._gateway_process=SimpleNamespace(returncode=None)
    assert await asyncio.wait_for(workspace._wait_gateway_health(.02),.5) is False


@pytest.mark.asyncio
async def test_exited_gateway_is_never_treated_as_ready():
    workspace=WorkerMCPWorkspace(workspace_id='exited')
    workspace._gateway=SimpleNamespace(health=AsyncMock(return_value=True))
    workspace._gateway_process=SimpleNamespace(returncode=1)
    assert not await workspace._wait_gateway_health(30)
    workspace._gateway.health.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_startup_retains_process_for_owner_cleanup():
    workspace=WorkerMCPWorkspace(workspace_id='cancelled')
    entered=asyncio.Event()
    async def health():entered.set();await asyncio.Event().wait()
    workspace._gateway=SimpleNamespace(health=health)
    process=SimpleNamespace(returncode=None);workspace._gateway_process=process
    task=asyncio.create_task(workspace._wait_gateway_health(30))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert workspace._gateway_process is process
