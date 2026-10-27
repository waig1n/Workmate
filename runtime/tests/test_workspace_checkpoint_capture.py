"""Checkpoint failure is explicit; stale owners cannot publish recovery metadata."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from workmate_runtime.transport.checkpoints import CheckpointStore
from workmate_product.config.artifact_storage import ArtifactStoragePolicy, artifact_storage_overrides


class DB:
    def __init__(self):
        self.execute = AsyncMock()
    @asynccontextmanager
    async def transaction(self):
        yield


@pytest.mark.asyncio
async def test_fenced_metadata(monkeypatch):
    db = DB()
    check = AsyncMock(return_value=(None, None, {'conversation_id':'c'}))
    monkeypatch.setattr('workmate_runtime.transport.checkpoints.DeliveryStore._owner', check)
    owner = SimpleNamespace(task_id='t',session_id='s',execution_epoch=3)
    await CheckpointStore(db).record(owner,'n',boundary='terminal',manifest={'files':[]})
    assert check.await_count == 2
    args = db.execute.call_args.args
    assert args[8] == 'available' and len(args[10]) == 64


@pytest.mark.asyncio
async def test_capture_failure_metadata(monkeypatch):
    from workmate_runtime.delivery.checkpoints import CheckpointCoordinator
    db = DB()
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield db
    record = AsyncMock()
    monkeypatch.setattr('workmate_runtime.delivery.checkpoints.CheckpointStore.record',record)
    coordinator = CheckpointCoordinator(Pool(),'n',Mock())
    coordinator.files.capture = Mock(side_effect=ValueError('secret-path'))
    await coordinator.capture(SimpleNamespace(task_id='t'),'/workspace',boundary='approval')
    assert record.call_args.kwargs == {'boundary':'approval','manifest':None,'error_code':'checkpoint_capture_failed'}


@pytest.mark.asyncio
async def test_metadata_fence_loss_does_not_break_normal_delivery(monkeypatch):
    from workmate_runtime.delivery.checkpoints import CheckpointCoordinator
    db = DB()
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield db
    monkeypatch.setattr('workmate_runtime.delivery.checkpoints.CheckpointStore.record',AsyncMock(side_effect=ValueError('stale')))
    coordinator = CheckpointCoordinator(Pool(),'n',Mock())
    coordinator.files.capture = Mock(return_value={'files':[]})
    assert await coordinator.capture(SimpleNamespace(task_id='t'),'/workspace',boundary='terminal') is None


def test_checkpoint_policy_defaults_overrides():
    assert not ArtifactStoragePolicy().workspace_checkpoint_enabled
    value = ArtifactStoragePolicy(**artifact_storage_overrides({'ARTIFACT_STORAGE_BACKEND':'s3',
        'ARTIFACT_STORAGE_WORKSPACE_CHECKPOINT_ENABLED':'true',
        'ARTIFACT_STORAGE_WORKSPACE_CHECKPOINT_MAX_FILES':'7'}))
    assert value.workspace_checkpoint_enabled and value.workspace_checkpoint_max_files == 7


@pytest.mark.parametrize('values',[{'workspace_checkpoint_enabled':True},
    {'workspace_checkpoint_max_files':0},{'workspace_checkpoint_max_file_bytes':0},
    {'workspace_checkpoint_max_bytes':1}])
def test_checkpoint_policy_invalid(values):
    with pytest.raises(ValueError): ArtifactStoragePolicy(**values)


def test_checkpoint_yaml_loading(tmp_path, monkeypatch):
    from workmate_runtime.config import load_runtime_settings
    path=tmp_path/'config.yaml'
    path.write_text('product:\n  artifacts:\n    storage:\n      backend: s3\n      workspace_checkpoint_enabled: true\n      workspace_checkpoint_max_files: 9\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE',str(path))
    settings=load_runtime_settings()
    assert settings.artifact_storage.workspace_checkpoint_enabled
    assert settings.artifact_storage.workspace_checkpoint_max_files == 9


@pytest.mark.asyncio
async def test_stale_owner_never_publishes(monkeypatch):
    db=DB()
    monkeypatch.setattr('workmate_runtime.transport.checkpoints.DeliveryStore._owner',AsyncMock(side_effect=ValueError('stale')))
    with pytest.raises(ValueError):
        await CheckpointStore(db).record(SimpleNamespace(), 'n', boundary='terminal', manifest={})
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_boundary_rejected_before_io():
    db=DB()
    with pytest.raises(ValueError):
        await CheckpointStore(db).record(None,'n',boundary='running',manifest={})
    db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['docker', 'kubernetes'])
async def test_remote_capture_uses_trusted_control_not_sealed_task_backend(monkeypatch, provider):
    from workmate_runtime.delivery.checkpoints import CheckpointCoordinator
    from workmate_runtime.sandbox.base import TrustedSandboxControlBackend
    db = DB()
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield db
    control = TrustedSandboxControlBackend()
    workspace = SimpleNamespace(host_workdir=None, workdir='/workspace', _workmate_provider=provider,
        _workmate_control=control, get_backend=Mock(side_effect=AssertionError('Task backend must not be used')))
    record = AsyncMock()
    monkeypatch.setattr('workmate_runtime.delivery.checkpoints.CheckpointStore.record', record)
    remote = SimpleNamespace(capture=AsyncMock(return_value={'files':[]}))
    factory = Mock(return_value=remote)
    monkeypatch.setattr('workmate_runtime.delivery.remote_workspace_checkpoint.RemoteWorkspaceCheckpoint', factory)
    coordinator = CheckpointCoordinator(Pool(), 'node', Mock())
    await coordinator.capture(SimpleNamespace(task_id='t'), workspace, boundary='terminal')
    assert factory.call_args.args[:2] == (control, '/workspace')
    assert factory.call_args.kwargs['provider'] == provider
    assert record.call_args.kwargs['manifest'] == {'files':[]}
    remote.capture.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,control', [('e2b', True), ('docker', False)])
async def test_unsupported_or_untrusted_remote_capture_records_failure(monkeypatch, provider, control):
    from workmate_runtime.delivery.checkpoints import CheckpointCoordinator
    from workmate_runtime.sandbox.base import TrustedSandboxControlBackend
    db = DB()
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield db
    record = AsyncMock()
    monkeypatch.setattr('workmate_runtime.delivery.checkpoints.CheckpointStore.record', record)
    workspace = SimpleNamespace(host_workdir=None, workdir='/workspace', _workmate_provider=provider,
        _workmate_control=TrustedSandboxControlBackend() if control else object(),
        get_backend=Mock(side_effect=AssertionError('No implicit control fallback')))
    await CheckpointCoordinator(Pool(),'node',Mock()).capture(SimpleNamespace(task_id='t'), workspace, boundary='terminal')
    assert record.call_args.kwargs['manifest'] is None
    assert record.call_args.kwargs['error_code'] == 'checkpoint_capture_failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['capture', 'metadata', 'missing_receipt'])
async def test_required_checkpoint_never_accepts_unavailable_or_missing_receipt(monkeypatch, failure):
    from workmate_runtime.delivery.checkpoints import CheckpointCoordinator
    db = DB()
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield db
    record = AsyncMock(return_value=None if failure == 'missing_receipt' else 'checkpoint-id',
                       side_effect=RuntimeError('metadata failure') if failure == 'metadata' else None)
    monkeypatch.setattr('workmate_runtime.delivery.checkpoints.CheckpointStore.record', record)
    coordinator = CheckpointCoordinator(Pool(), 'node', Mock())
    coordinator.files.capture = Mock(return_value={'files':[]},
                                    side_effect=RuntimeError('capture failure') if failure == 'capture' else None)
    with pytest.raises(RuntimeError):
        await coordinator.capture_required(SimpleNamespace(task_id='t'), '/workspace', boundary='approval')
    if failure == 'capture': record.assert_not_awaited()
