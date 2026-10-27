"""Real filesystem and process locks; no automatic dirty Workspace adoption."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from workmate_runtime.assembly.posix_workspace_ownership import PosixWorkspaceOwnership
from workmate_runtime.assembly.workspace_ownership import WorkspaceOwnershipError


IDENTITY={'session_id':'session','conversation_id':'conversation','storage_node_id':'node'}


def acquire(root,identity=None):
    return PosixWorkspaceOwnership.acquire(root,'workspace',IDENTITY if identity is None else identity)


def child(root,mode):
    script='''
import json,os,sys
from pathlib import Path
from workmate_runtime.assembly.posix_workspace_ownership import PosixWorkspaceOwnership
from workmate_runtime.assembly.workspace_ownership import WorkspaceOwnershipError
try:
    lease=PosixWorkspaceOwnership.acquire(Path(sys.argv[1]),'workspace',json.loads(sys.argv[3]))
except WorkspaceOwnershipError:
    print('rejected',flush=True)
    sys.exit(42)
print(lease.generation,flush=True)
if sys.argv[2]=='crash': os._exit(0)
lease.close(clean=True)
'''
    environment={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1])}
    return subprocess.run([sys.executable,'-c',script,str(root),mode,json.dumps(IDENTITY)],
        env=environment,capture_output=True,text=True,timeout=10)


def test_clean_release_retains_generation_and_control_files_outside_workspace(tmp_path):
    lease=acquire(tmp_path)
    assert lease.path==tmp_path/'workspace'
    assert lease.path.is_dir()
    generation=lease.generation
    assert isinstance(generation,str) and generation
    controls=tmp_path/'.workmate-ownership'
    locks=list(controls.glob('*.lock'))
    metadata=list(controls.glob('*.json'))
    assert len(locks)==len(metadata)==1
    lock_inode=locks[0].stat().st_ino
    assert not controls.is_relative_to(lease.path)
    lease.close(clean=True)
    reopened=acquire(tmp_path)
    assert reopened.generation==generation
    assert locks[0].stat().st_ino==lock_inode
    reopened.close(clean=True)


def test_existing_empty_directory_can_be_initialized(tmp_path):
    (tmp_path/'workspace').mkdir()
    lease=acquire(tmp_path)
    lease.close(clean=True)


def test_unregistered_nonempty_workspace_is_not_adopted(tmp_path):
    directory=tmp_path/'workspace';directory.mkdir()
    (directory/'existing.txt').write_text('original')
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)
    assert (directory/'existing.txt').read_text()=='original'


def test_live_owner_excludes_second_handle_and_other_process(tmp_path):
    owner=acquire(tmp_path)
    try:
        with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)
        result=child(tmp_path,'clean')
        assert result.returncode==42,result.stderr
        assert result.stdout.strip()=='rejected'
    finally:
        owner.close(clean=True)
    result=child(tmp_path,'clean')
    assert result.returncode==0,result.stderr


def test_dirty_close_releases_process_lock_but_refuses_adoption(tmp_path):
    owner=acquire(tmp_path)
    owner.close(clean=False)
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)
    result=child(tmp_path,'clean')
    assert result.returncode==42,result.stderr


def test_process_crash_leaves_dirty_workspace_unrecoverable(tmp_path):
    result=child(tmp_path,'crash')
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()
    # OS released the flock, but durable metadata still forbids adoption.
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)


def test_identity_change_does_not_adopt_clean_workspace(tmp_path):
    owner=acquire(tmp_path);owner.close(clean=True)
    for field in IDENTITY:
        with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path,{**IDENTITY,field:'other'})
    original=acquire(tmp_path);original.close(clean=True)


def test_replaced_workspace_directory_is_rejected(tmp_path):
    owner=acquire(tmp_path);owner.close(clean=True)
    (tmp_path/'workspace').rename(tmp_path/'original')
    (tmp_path/'workspace').mkdir()
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)


def test_replaced_root_with_copied_control_files_is_rejected(tmp_path):
    import shutil
    root=tmp_path/'root';root.mkdir()
    owner=acquire(root);owner.close(clean=True)
    root.rename(tmp_path/'old-root')
    shutil.copytree(tmp_path/'old-root',root)
    with pytest.raises(WorkspaceOwnershipError): acquire(root)


@pytest.mark.parametrize('target',['root','workspace','controls'])
def test_symlink_paths_are_not_followed(tmp_path,target):
    root=tmp_path/'root';root.mkdir()
    other=tmp_path/'other';other.mkdir()
    if target=='root':
        root.rmdir();root.symlink_to(other,target_is_directory=True)
    else:
        (root/('workspace' if target=='workspace' else '.workmate-ownership')).symlink_to(other,target_is_directory=True)
    with pytest.raises(WorkspaceOwnershipError): acquire(root)
    assert list(other.iterdir())==[]


@pytest.mark.parametrize('content',['{bad json','null','{}'])
def test_corrupt_metadata_is_not_silently_reinitialized(tmp_path,content):
    owner=acquire(tmp_path);owner.close(clean=True)
    metadata=next((tmp_path/'.workmate-ownership').glob('*.json'))
    metadata.write_text(content)
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)
    assert metadata.read_text()==content


@pytest.mark.parametrize('workspace_id',['../escape','/absolute','nested/workspace','.','..','.workmate-ownership'])
def test_workspace_identifier_cannot_escape_or_alias_control_directory(tmp_path,workspace_id):
    with pytest.raises((WorkspaceOwnershipError,ValueError)):
        PosixWorkspaceOwnership.acquire(tmp_path,workspace_id,IDENTITY)


@pytest.mark.parametrize('suffix',['.lock','.json'])
def test_control_file_symlink_is_rejected_without_touching_target(tmp_path,suffix):
    owner=acquire(tmp_path);owner.close(clean=True)
    control=next((tmp_path/'.workmate-ownership').glob('*'+suffix))
    outside=tmp_path/'outside';outside.write_text('do not change')
    control.unlink();control.symlink_to(outside)
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)
    assert outside.read_text()=='do not change'


def test_repeated_clean_close_cannot_clear_successor_dirty_latch(tmp_path):
    original=acquire(tmp_path)
    original.close(clean=True)
    successor=acquire(tmp_path)
    original.close(clean=True)
    successor.close(clean=False)
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)


def test_live_directory_replacement_cannot_be_marked_clean(tmp_path):
    owner=acquire(tmp_path)
    owner.path.rename(tmp_path/'original')
    owner.path.mkdir()
    try:
        with pytest.raises(WorkspaceOwnershipError): owner.close(clean=True)
    finally:
        owner.close(clean=False)
    # Restoring the original inode still cannot clear a failed clean close.
    owner.path.rmdir();(tmp_path/'original').rename(owner.path)
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)


@pytest.mark.parametrize('field,value',[('dirty',0),('dirty','false'),('generation','bad'),('version',True),
    ('identity',{}),('workspace',[0,0]),('root',[0,0])])
def test_corrupt_metadata_fields_fail_closed(tmp_path,field,value):
    owner=acquire(tmp_path);owner.close(clean=True)
    metadata=next((tmp_path/'.workmate-ownership').glob('*.json'))
    record=json.loads(metadata.read_text());record[field]=value
    metadata.write_text(json.dumps(record))
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)


def test_replaced_metadata_inode_cannot_be_overwritten_by_clean_close(tmp_path):
    owner=acquire(tmp_path)
    metadata=next((tmp_path/'.workmate-ownership').glob('*.json'))
    original=metadata.read_text()
    replacement=metadata.with_suffix('.replacement')
    replacement.write_text(original)
    replacement.replace(metadata)
    try:
        with pytest.raises(WorkspaceOwnershipError): owner.close(clean=True)
        assert metadata.read_text()==original
    finally:
        owner.close(clean=False)
    with pytest.raises(WorkspaceOwnershipError): acquire(tmp_path)
