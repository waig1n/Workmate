"""Gateway-generated URL-safe credentials are values even when starting with '-'."""
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from workmate_runtime.mcp import server
from workmate_runtime.mcp.workspace import WorkerMCPWorkspace


@pytest.mark.asyncio
@pytest.mark.parametrize(('token','nonce'),[
    ('-fixture','nonce'),('token','-fixture'),('-token','-nonce'),
    ('--option-like','--other-option'),('space ; $() `fixture`',"quote' nonce")])
async def test_bootstrap_cli_preserves_credentials_through_shell_and_real_parser(tmp_path,monkeypatch,token,nonce):
    process=SimpleNamespace(returncode=None,pid=1234)
    backend=SimpleNamespace(join_path=lambda *p:'/'.join(p),write_file=AsyncMock(),
                            start_process=AsyncMock(return_value=process))
    workspace=WorkerMCPWorkspace(workspace_id='cli-fixture',host_workdir=str(tmp_path),gateway_port=12345)
    workspace._backend=backend
    monkeypatch.setattr(workspace,'_bootstrap_is_ready',AsyncMock(return_value=True))
    monkeypatch.setattr(workspace,'_wait_gateway_health',AsyncMock(return_value=True))
    def credentials():workspace._gateway_token=token;workspace._gateway_nonce=nonce
    monkeypatch.setattr(workspace,'_rotate_gateway_credentials',credentials)
    parsed=[]
    monkeypatch.setattr(server,'run',lambda port,auth,identity:parsed.append((port,auth,identity)))
    monkeypatch.setattr(server.asyncio,'run',lambda value:value)
    try:
        await workspace._setup_mcp_gateway()
        command=backend.start_process.await_args.args[0][-1]
        argv=shlex.split(command);start=argv.index(workspace._gateway_script)+1
        stop=argv.index('>')
        monkeypatch.setattr(sys,'argv',['gateway.py',*argv[start:stop]])
        server.main()
        assert parsed==[(12345,token,nonce)]
        backend.start_process.assert_awaited_once()
    finally:
        if workspace._gateway:await workspace._gateway.aclose()
