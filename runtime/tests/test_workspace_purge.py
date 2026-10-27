"""Permanent cleanup is separate from idle closure and never follows links."""
from pathlib import Path
from dataclasses import replace
import pytest
from test_worker_workspaces import manager, IDENTITY
from workmate_runtime.assembly.workspaces import WorkspaceBindingError


async def test_purge_after_retirement_removes_content_but_not_other_workspace(tmp_path):
    mgr = manager(tmp_path)
    ws = await mgr.get_workspace(IDENTITY)
    other = await mgr.get_workspace(replace(IDENTITY, session_id='other', conversation_id='other'))
    target = Path(ws.host_workdir)
    (target/'output/result.txt').write_text('secret')
    outside = tmp_path/'outside'; outside.mkdir(); (outside/'keep').write_text('shared')
    (target/'working/link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceBindingError):
        await mgr.purge_workspace(IDENTITY)
    await mgr.retire_session(IDENTITY.session_id)
    await mgr.purge_workspace(IDENTITY)
    await mgr.purge_workspace(IDENTITY)
    assert list(target.iterdir()) == []
    assert (outside/'keep').read_text() == 'shared'
    assert Path(other.host_workdir, 'output').is_dir()
    await mgr.close()


async def test_restart_can_retire_clean_storage_without_starting_compute(tmp_path):
    first = manager(tmp_path)
    ws = await first.get_workspace(IDENTITY)
    (Path(ws.host_workdir)/'output/file').write_text('secret')
    await first.close()
    restarted = manager(tmp_path)
    assert await restarted.retire_stored_workspace(IDENTITY)
    assert not restarted._entries
    await restarted.purge_workspace(IDENTITY)
    assert not list(Path(ws.host_workdir).iterdir())
    await restarted.close()


async def test_restart_rejects_dirty_or_held_workspace(tmp_path):
    first = manager(tmp_path)
    await first.get_workspace(IDENTITY)
    restarted = manager(tmp_path)
    with pytest.raises(WorkspaceBindingError):
        await restarted.retire_stored_workspace(IDENTITY)
    lease = first._ownership[IDENTITY]
    lease.close(clean=False)
    first._ownership.pop(IDENTITY)
    with pytest.raises(WorkspaceBindingError):
        await restarted.retire_stored_workspace(IDENTITY)
    # Test leaves an intentionally dirty generation; no other process owns it.
    await first.close()
    await restarted.close()


def test_storage_cleanup_rejects_escaping_paths_and_symlink_parents(tmp_path):
    from workmate_product.application.storage_cleanup import remove_storage_path
    outside = tmp_path/'outside'; outside.mkdir(); (outside/'keep').write_text('keep')
    root = tmp_path/'root'; root.mkdir(); (root/'link').symlink_to(outside, target_is_directory=True)
    for path in ('../outside', '/outside', 'bad\\path'):
        with pytest.raises(ValueError):
            remove_storage_path(root, path)
    with pytest.raises(OSError):
        remove_storage_path(root, 'link/keep')
    remove_storage_path(root, 'missing/path')
    remove_storage_path(root, 'missing')
    assert (outside/'keep').read_text() == 'keep'


async def test_purge_retains_files_when_provider_configuration_changed(tmp_path):
    mgr = manager(tmp_path)
    ws = await mgr.get_workspace(IDENTITY)
    target = Path(ws.host_workdir)/'output/keep'; target.write_text('keep')
    await mgr.retire_session(IDENTITY.session_id)
    mgr.provider.name = 'docker'
    with pytest.raises(WorkspaceBindingError, match='original provider'):
        await mgr.purge_workspace(IDENTITY)
    assert target.exists()
    await mgr.close()



async def test_missing_ownership_record_cannot_certify_provisioned_workspace(tmp_path):
    from workmate_runtime.assembly.posix_workspace_ownership import OWNERSHIP_DIRECTORY_NAME
    first = manager(tmp_path)
    ws = await first.get_workspace(IDENTITY)
    await first.close()
    digest = Path(ws.host_workdir).name
    (tmp_path/OWNERSHIP_DIRECTORY_NAME/(digest+'.json')).unlink()
    restarted = manager(tmp_path)
    with pytest.raises(WorkspaceBindingError, match='no cleanup ownership evidence'):
        await restarted.retire_stored_workspace(IDENTITY)
    await restarted.close()
