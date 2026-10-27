"""Run inside the built Worker image with its documented Bubblewrap settings.

No project mounts or network services are needed. The container's loopback hosts
an ephemeral MCP gateway; all test files live under a temporary directory.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from importlib.abc import MetaPathFinder
import sys


class NoLegacyRole(MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name=='app' or name.startswith('app.') or name=='workmate_runtime.main':
            raise AssertionError('Worker imported a legacy role or Product HTTP package')


async def main():
    sys.meta_path.insert(0,NoLegacyRole())
    from workmate_runtime.roles.worker import create_worker_app
    from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
    from workmate_runtime.workspaces import WorkmateSandboxWorkspace
    await WorkmateSandboxWorkspace._probe_bubblewrap()
    with tempfile.TemporaryDirectory(prefix='worker-image-smoke-') as folder:
        root=Path(folder)
        workdir=root/'workspace'
        workdir.mkdir()
        workspace=WorkerMCPWorkspace(workspace_id='smoke',host_workdir=str(workdir),gateway_port=None)
        try:
            await workspace.initialize()
            backend=workspace.get_backend()
            script='''import os
from mcp.server.fastmcp import FastMCP
server=FastMCP('counter')
value=0
@server.tool()
def increment()->dict:
    global value
    value+=1
    return {'value':value,'namespace':os.readlink('/proc/self/ns/pid'),
            'configured':bool(os.environ.get('MCP_SMOKE_SECRET'))}
server.run(transport='stdio')
'''
            await backend.write_file('/workspace/counter.py',script.encode())
            gateway=workspace._gateway
            params={'agent_id':'agent','session_id':'session'}
            secret='smoke-memory-only-configuration'
            status,body=await gateway.exec_request('POST','/mcps',params=params,body={
                'name':'counter','is_stateful':True,'mcp_config':{'type':'stdio_mcp',
                'command':'/usr/local/bin/python','args':['-u','/workspace/counter.py'],
                'env':{'MCP_SMOKE_SECRET':secret}}})
            assert status==200,body
            for expected in (1,2):
                status,body=await gateway.exec_request('POST','/mcps/counter/tools/increment',
                    params=params,body={'arguments':{}})
                assert status==200,body
                result=json.loads(json.loads(body)['chunk']['content'][0]['text'])
                assert result['value']==expected and result['configured']
                assert result['namespace']!=os.readlink('/proc/self/ns/pid')
            status,body=await gateway.exec_request('DELETE','/mcps/counter',params=params)
            assert status==200,body
            for area in (workdir,Path(workspace._tmpdir.name)):
                assert not any(secret.encode() in p.read_bytes() for p in area.rglob('*') if p.is_file())
        finally:
            await workspace.close()
        assert workspace._gateway_process is None and not workspace.is_alive
    print('Worker image: isolated imports, real Bubblewrap, stateful MCP and cleanup passed')


asyncio.run(main())
