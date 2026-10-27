from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
import hashlib
import pytest
from workmate_contracts import snapshot_digest
from workmate_runtime.assembly.workspace_recovery import restore_recovery_workspace, WorkspaceRecoveryError


@pytest.fixture
def case(tmp_path):
    manifest = dict(version=1, directories=['working'], files=[], total_bytes=0)
    fork = NS(id='fork', conversation_id='conv', target_task_id='task', target_session_id='session', actor_user_id='user', target_node_id='node', manifest=manifest)
    session = NS(superseded_at=None, conversation_id='conv', storage_node_id='node', owner_id='worker',execution_epoch=2,active_task_id='task')
    context = NS(conversation_id='conv',session_id='session',task_id='task',actor_user_id='user',initial_spec=NS(recovery_fork_id='fork',recovery_manifest_sha256=snapshot_digest(manifest)))
    root=tmp_path/'workspace';root.mkdir()
    for name in ['input','working','output']: (root/name).mkdir()
    binding=NS(workspace=NS(host_workdir=root),owner=NS(session_id='session',task_id='task',worker_id='worker',execution_epoch=2))
    active=[]
    class DB:
        async def get(self, model, id): return fork if id=='fork' else session
    @asynccontextmanager
    async def sessions():
        active.append(True)
        try: yield DB()
        finally: active.pop()
    policy=NS(workspace_checkpoint_enabled=True,workspace_checkpoint_max_files=10,workspace_checkpoint_max_bytes=100,workspace_checkpoint_max_file_bytes=100)
    return NS(fork=fork,session=session,context=context,binding=binding,sessions=sessions,policy=policy,root=root,active=active)


async def run(c, store=None):
    return await restore_recovery_workspace(c.context,c.binding,c.sessions,store or NS(),c.policy,storage_node_id='node')


@pytest.mark.asyncio
async def test_restore_after_db_closed(case,tmp_path):
    content=b'file';digest=hashlib.sha256(content).hexdigest()
    case.fork.manifest['files']=[dict(path='working/a',storage_key=f'.objects/sha256/{digest[:2]}/{digest}',sha256=digest,size=4,executable=False)]
    case.fork.manifest['total_bytes']=4
    case.context.initial_spec.recovery_manifest_sha256=snapshot_digest(case.fork.manifest)
    class Store:
        def snapshot(self,*args):
            assert not case.active
            path=tmp_path/'object';path.write_bytes(content)
            return NS(path=path,close=lambda:path.unlink())
    await run(case,Store())
    assert (case.root/'working/a').read_bytes()==content
    with pytest.raises(WorkspaceRecoveryError): await run(case,Store())


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['superseded','session','digest','actor','node','owner','epoch','nonempty','unsupported','missing'])
async def test_reject_without_modifying_destination(case,change):
    if change=='superseded': case.session.superseded_at='retired'
    if change=='session': case.fork.target_session_id='old-session'
    if change=='digest': case.context.initial_spec.recovery_manifest_sha256='bad'
    if change=='actor': case.fork.actor_user_id='foreign'
    if change=='node': case.session.storage_node_id='foreign'
    if change=='owner': case.session.owner_id='foreign'
    if change=='epoch': case.session.execution_epoch=3
    if change=='nonempty': (case.root/'working/keep').write_text('keep')
    if change=='unsupported': case.binding.workspace.host_workdir=None
    if change=='missing':
        case.fork.manifest['files']=[dict(path='working/a',size=1,sha256='a'*64,storage_key='.objects/sha256/aa/'+'a'*64,executable=False)]
        case.fork.manifest['total_bytes']=1
        case.context.initial_spec.recovery_manifest_sha256=snapshot_digest(case.fork.manifest)
    before=sorted(str(p.relative_to(case.root)) for p in case.root.rglob('*'))
    with pytest.raises(WorkspaceRecoveryError): await run(case)
    assert sorted(str(p.relative_to(case.root)) for p in case.root.rglob('*'))==before


@pytest.mark.asyncio
async def test_later_task_leaves_existing_files(case):
    case.fork.target_task_id='earlier'
    (case.root/'working/keep').write_text('keep')
    await run(case)
    assert (case.root/'working/keep').read_text()=='keep'


@pytest.mark.asyncio
async def test_ordinary_context_noop(case):
    case.context.initial_spec.recovery_fork_id=None
    await run(case)


@pytest.mark.asyncio
async def test_cancellation_joins_filesystem_operation(case, monkeypatch):
    import asyncio
    import threading
    from workmate_runtime.assembly import workspace_recovery
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def install(*args):
        started.set()
        release.wait(5)
        finished.set()
    monkeypatch.setattr(workspace_recovery, '_install', install)
    task = asyncio.create_task(run(case))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_corrupt_object_never_installs(case,tmp_path):
    digest=hashlib.sha256(b'good').hexdigest()
    case.fork.manifest['files']=[dict(path='working/a',storage_key=f'.objects/sha256/{digest[:2]}/{digest}',sha256=digest,size=4,executable=False)]
    case.fork.manifest['total_bytes']=4
    case.context.initial_spec.recovery_manifest_sha256=snapshot_digest(case.fork.manifest)
    class Store:
        def snapshot(self,*args):
            path=tmp_path/'bad-object';path.write_bytes(b'evil')
            return NS(path=path,close=lambda:path.unlink())
    with pytest.raises(WorkspaceRecoveryError): await run(case,Store())
    assert not (case.root/'.workmate-recovery-attempt').exists()
    assert list((case.root/'working').iterdir())==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['docker', 'kubernetes'])
async def test_remote_restore_uses_control_after_database_closed(case, monkeypatch, provider):
    from unittest.mock import AsyncMock, Mock
    from workmate_runtime.sandbox.base import TrustedSandboxControlBackend
    control = TrustedSandboxControlBackend()
    case.binding.workspace = NS(host_workdir=None, workdir='/workspace', _workmate_provider=provider,
        _workmate_control=control, get_backend=Mock(side_effect=AssertionError('Do not use Task backend')))
    async def restore(manifest):
        assert not case.active
        assert manifest == case.fork.manifest
    remote = NS(restore=AsyncMock(side_effect=restore))
    factory = Mock(return_value=remote)
    monkeypatch.setattr('workmate_runtime.delivery.remote_workspace_checkpoint.RemoteWorkspaceCheckpoint', factory)
    await run(case)
    assert factory.call_args.args[:2] == (control, '/workspace')
    remote.restore.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_restore_failure_is_not_ignored(case, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from workmate_runtime.sandbox.base import TrustedSandboxControlBackend
    case.binding.workspace = NS(host_workdir=None, workdir='/workspace', _workmate_provider='docker',
        _workmate_control=TrustedSandboxControlBackend())
    remote = NS(restore=AsyncMock(side_effect=ValueError('missing bytes')))
    monkeypatch.setattr('workmate_runtime.delivery.remote_workspace_checkpoint.RemoteWorkspaceCheckpoint', Mock(return_value=remote))
    with pytest.raises(WorkspaceRecoveryError, match='tools must not run'):
        await run(case)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,control', [('e2b', True), ('docker', False)])
async def test_remote_restore_rejects_unsupported_or_untrusted(case, provider, control):
    from workmate_runtime.sandbox.base import TrustedSandboxControlBackend
    case.binding.workspace = NS(host_workdir=None, workdir='/workspace', _workmate_provider=provider,
        _workmate_control=TrustedSandboxControlBackend() if control else object())
    with pytest.raises(WorkspaceRecoveryError, match='tools must not run'):
        await run(case)
