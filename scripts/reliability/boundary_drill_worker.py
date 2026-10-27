"""Observe exact production barriers; no fake storage or execution authority."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import runpy
import sys
import common
from workmate_runtime.assembly.agent import WorkerAgentAssembler
from workmate_runtime.transport.suspension import SuspensionStore
from workmate_runtime.execution.suspension import SuspensionController

name=sys.argv[1]
mode=name.removeprefix('a-') if name.startswith('a-') else None
E=Path('/evidence')
original_settings=common.settings

def settings():
    configured=original_settings()
    configured.runtime.worker.lease_seconds=6
    return configured
common.settings=settings

async def barrier(value):
    (E/(name+'-barrier.json')).write_text(json.dumps(value,default=str,indent=2))
    await asyncio.Event().wait()


def workspace(self,owner):
    bound=self._bound[owner]
    lease=self.workspaces._ownership[bound.identity]
    path=Path(lease.path)
    stat=path.stat()
    return {'owner':asdict(owner),'identity':asdict(bound.identity),'generation':lease.generation,
            'path':str(path),'inode':[stat.st_dev,stat.st_ino], 'provider':self.workspaces.provider.name}

prepare=WorkerAgentAssembler.prepare_start
async def observed_prepare(self,owner,snapshot,guard):
    value=await prepare(self,owner,snapshot,guard)
    proof=workspace(self,owner)
    assert proof['provider']=='bubblewrap'
    (E/(name+'-workspace.json')).write_text(json.dumps(proof,indent=2))
    if mode=='preparing': await barrier({'boundary':'preparing','workspace':proof})
    return value
WorkerAgentAssembler.prepare_start=observed_prepare

resume=WorkerAgentAssembler.prepare_resume
async def observed_resume(self,owner,snapshot,guard):
    result=await resume(self,owner,snapshot,guard)
    (E/(name+'-resume.json')).write_text(json.dumps(workspace(self,owner),indent=2))
    return result
WorkerAgentAssembler.prepare_resume=observed_resume

last_release={}
release=SuspensionStore.release
async def observed_release(self,claim,receipt):
    value={'boundary':mode,'claim':asdict(claim),'receipt':receipt.model_dump()}
    if mode=='release-before': await barrier(value)
    await release(self,claim,receipt)
    last_release[claim.owner.session_id]=value
SuspensionStore.release=observed_release
suspend=SuspensionController.try_suspend
async def observed_suspend(self,owner,node_id):
    result=await suspend(self,owner,node_id)
    if result and mode=='release-after': await barrier(last_release[owner.session_id])
    return result
SuspensionController.try_suspend=observed_suspend
def validate_expected_shutdown(app):
    allowed=json.loads((E/'allowed-blocked.json').read_text()) if (E/'allowed-blocked.json').exists() else {}
    assert all(reason in allowed.get(sid,[]) for sid,reason in app.service.failures.items()),app.service.failures
    assert not app.workspaces._ownership

# The load fixture asserts zero failures. FAULT deliberately retains exact expired
# Sessions; accept only their explicit startup observation, never erase it.
source=Path('/support/original_worker.py').read_text()
assert source.count('assert apps and not apps[0].service.failures')==1
source=source.replace('assert apps and not apps[0].service.failures','assert apps; validate_expected_shutdown(apps[0])')
exec(compile(source,'/support/original_worker.py','exec'),{'__name__':'__main__','validate_expected_shutdown':validate_expected_shutdown})
