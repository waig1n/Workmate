"""Cold bootstrap spans expose latency without credentials or changed ownership."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from workmate_runtime.mcp.gateway import MCPGatewayError
from workmate_runtime.mcp.workspace import WorkerMCPWorkspace


@pytest.mark.asyncio
@pytest.mark.parametrize('healthy',[True,False])
async def test_bootstrap_health_and_spawn_are_timed_without_logging_commands(tmp_path,monkeypatch,capfd,healthy):
    process=SimpleNamespace(returncode=None,pid=1234)
    workspace=WorkerMCPWorkspace(workspace_id='timing-fixture',host_workdir=str(tmp_path),gateway_port=12345)
    workspace._backend=SimpleNamespace(join_path=lambda *parts:'/'.join(parts),
        write_file=AsyncMock(),start_process=AsyncMock(return_value=process))
    monkeypatch.setattr(workspace,'_bootstrap_is_ready',AsyncMock(return_value=True))
    monkeypatch.setattr(workspace,'_wait_gateway_health',AsyncMock(return_value=healthy))
    monkeypatch.setattr(workspace,'_rotate_gateway_credentials',lambda:None)
    workspace._gateway_token='never-log-gateway-token'
    workspace._gateway_nonce='never-log-gateway-nonce'
    try:
        if healthy:await workspace._setup_mcp_gateway()
        else:
            with pytest.raises(MCPGatewayError):await workspace._setup_mcp_gateway()
        assert workspace._gateway_process is process
        text=capfd.readouterr().err
        assert 'stage=gateway_spawn' in text and 'stage=gateway_health' in text
        assert 'workspace_id=timing-fixture' in text and 'duration_ms=' in text
        assert 'never-log' not in text and '--auth-token' not in text
    finally:
        if workspace._gateway is not None:await workspace._gateway.aclose()
