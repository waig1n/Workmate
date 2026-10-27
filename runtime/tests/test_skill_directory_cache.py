"""Worker-only immutable directory cache and Task stdin copy boundary."""
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from workmate_runtime.assembly.skill_cache import SkillDirectoryCache, SkillCacheError
from workmate_runtime.assembly.skills import _MATERIALIZE_SCRIPT
from workmate_product.application.skill_artifacts import parse_skill_archive
from test_worker_skills import artifact


def metadata(package):
    from types import SimpleNamespace
    def validate(value):
        if (value.digest,value.manifest,value.name,value.description,value.version)!=(package.digest,package.manifest,package.name,package.description,package.version):
            raise ValueError('metadata mismatch')
    return SimpleNamespace(digest=package.digest,validate=validate)


def cached(cache, package):
    result=cache.load(metadata(package))
    return (result if result is not None else cache.store(package)).archive


def payload_files(payload):
    return dict(parse_skill_archive(payload).files)


def process_load(root, archive):
    return cached(SkillDirectoryCache(Path(root)),parse_skill_archive(archive))


def test_cache_hit_and_deleted_cache_reconstruct_exact_files(tmp_path):
    package=artifact()
    cache=SkillDirectoryCache(tmp_path/'cache')
    first=cached(cache,package)
    entry=tmp_path/'cache'/package.digest
    inode=(entry/'tree'/'SKILL.md').stat().st_ino
    assert payload_files(first)==dict(package.files)
    assert cached(cache,package)==first
    assert (entry/'tree'/'SKILL.md').stat().st_ino==inode
    import shutil
    shutil.rmtree(entry)
    assert cached(cache,package)==first


@pytest.mark.parametrize('damage',['content','missing','extra','mode','symlink','hardlink','marker','partial'])
def test_corrupt_cache_is_rebuilt_without_following_links(tmp_path,damage):
    package=artifact()
    cache=SkillDirectoryCache(tmp_path/'cache')
    expected=cached(cache,package)
    entry=tmp_path/'cache'/package.digest
    member=entry/'tree'/'SKILL.md'
    victim=tmp_path/'victim'
    victim.write_bytes(b'outside')
    if damage=='content':
        member.chmod(0o600)
        member.write_bytes(b'changed')
    elif damage=='missing': member.unlink()
    elif damage=='extra': (entry/'tree'/'extra').write_text('injected')
    elif damage=='mode': member.chmod(0o777)
    elif damage in {'symlink','hardlink'}:
        member.unlink()
        if damage=='symlink': member.symlink_to(victim)
        else: member.hardlink_to(victim)
    elif damage=='marker': (entry/'manifest.json').unlink()
    elif damage=='partial':
        import shutil
        shutil.rmtree(entry)
        entry.mkdir()
    assert cached(cache,package)==expected
    assert victim.read_bytes()==b'outside'


def test_cache_entry_symlink_does_not_modify_target(tmp_path):
    package=artifact()
    root=tmp_path/'cache'
    root.mkdir(mode=0o700)
    outside=tmp_path/'outside'
    outside.mkdir()
    (outside/'keep').write_text('safe')
    (root/package.digest).symlink_to(outside,target_is_directory=True)
    assert payload_files(cached(SkillDirectoryCache(root),package))==dict(package.files)
    assert (outside/'keep').read_text()=='safe'


@pytest.mark.parametrize('location',['root','ancestor','lock'])
def test_rejects_symlink_cache_root_ancestor_or_lock(tmp_path,location):
    package=artifact()
    outside=tmp_path/'outside'
    outside.mkdir()
    root=tmp_path/'cache'
    if location=='root': root.symlink_to(outside,target_is_directory=True)
    elif location=='ancestor':
        root.symlink_to(outside,target_is_directory=True)
        root=root/'nested'
    else:
        root.mkdir(mode=0o700)
        (root/(package.digest+'.lock')).symlink_to(outside/'victim')
    with pytest.raises(SkillCacheError): cached(SkillDirectoryCache(root),package)
    assert list(outside.iterdir())==[]


def test_multi_process_publish_has_one_complete_entry(tmp_path):
    package=artifact()
    root=tmp_path/'cache'
    with ProcessPoolExecutor(max_workers=4) as pool:
        jobs=[pool.submit(process_load,str(root),package.archive) for _ in range(8)]
        results=[job.result(timeout=20) for job in jobs]
    assert all(result==results[0] for result in results)
    assert payload_files(results[0])==dict(package.files)
    assert sorted(p.name for p in root.iterdir())==sorted([package.digest,package.digest+'.lock'])


def test_task_copy_is_independent_and_resume_rejects_task_tampering(tmp_path):
    package=artifact()
    cache=SkillDirectoryCache(tmp_path/'cache')
    archive=cached(cache,package)
    encoded=json.dumps(package.manifest).encode()
    payload=len(encoded).to_bytes(4,'big')+encoded+archive
    destination=tmp_path/'task'/'skills'
    args=[sys.executable,'-I','-c',_MATERIALIZE_SCRIPT,'install',str(destination),package.digest]
    installed=subprocess.run(args,input=payload,capture_output=True,timeout=20)
    assert installed.returncode==0,installed.stderr
    target=destination/'SKILL.md'
    assert target.stat().st_ino!=(tmp_path/'cache'/package.digest/'tree'/'SKILL.md').stat().st_ino
    args[4]='verify'
    assert subprocess.run(args,input=payload,capture_output=True,timeout=20).returncode==0
    target.chmod(0o600)
    target.write_text('task mutation')
    assert subprocess.run(args,input=payload,capture_output=True,timeout=20).returncode!=0
    assert cached(cache,package)==archive


def test_digest_mismatch_never_publishes(tmp_path):
    package=artifact()
    with pytest.raises(SkillCacheError):
        SkillDirectoryCache(tmp_path/'cache').store(replace(package,digest='0'*64))
    assert not (tmp_path/'cache'/('0'*64)).exists()


def test_failed_publish_does_not_expose_half_written_entry_and_can_retry(tmp_path,monkeypatch):
    import workmate_runtime.assembly.skill_cache as module
    package=artifact()
    cache=SkillDirectoryCache(tmp_path/'cache')
    original=module._write
    def fail(parent,name,content,mode=0o444):
        if name=='manifest.json':
            raise OSError('disk failure')
        return original(parent,name,content,mode)
    monkeypatch.setattr(module,'_write',fail)
    with pytest.raises(SkillCacheError): cache.store(package)
    assert not (cache.root/package.digest).exists()
    assert list(cache.root.glob('.partial-*'))==[]
    assert cache.load(metadata(package)) is None
    monkeypatch.setattr(module,'_write',original)
    assert cached(cache,package)==package.archive


def test_metadata_mismatch_is_not_accepted_as_cache_hit(tmp_path):
    from types import SimpleNamespace
    package=artifact()
    cache=SkillDirectoryCache(tmp_path/'cache')
    cache.store(package)
    def reject(value): raise ValueError('changed database metadata')
    assert cache.load(SimpleNamespace(digest=package.digest,validate=reject)) is None


@pytest.mark.parametrize('path',['tree','tree/scripts'])
def test_cache_directory_permission_tampering_is_rebuilt(tmp_path,path):
    package=artifact()
    cache=SkillDirectoryCache(tmp_path/'cache')
    cache.store(package)
    (cache.root/package.digest/path).chmod(0o777)
    assert cache.load(metadata(package)) is None
    assert cached(cache,package)==package.archive
    assert (cache.root/package.digest/path).stat().st_mode&0o777==0o700


@pytest.mark.parametrize('bad_root',['relative','/tmp/../unsafe'])
def test_cache_rejects_noncanonical_root(bad_root):
    with pytest.raises(SkillCacheError): SkillDirectoryCache(bad_root)


def test_cache_rejects_public_root_and_hardlinked_lock(tmp_path):
    package=artifact()
    root=tmp_path/'cache'
    root.mkdir(mode=0o755)
    cache=SkillDirectoryCache(root)
    with pytest.raises(SkillCacheError): cache.load(metadata(package))
    root.chmod(0o700)
    victim=tmp_path/'lock-victim'
    victim.write_bytes(b'outside')
    victim.chmod(0o600)
    (root/(package.digest+'.lock')).hardlink_to(victim)
    with pytest.raises(SkillCacheError): cache.load(metadata(package))
    assert victim.read_bytes()==b'outside'


def test_cache_preserves_executable_file_mode(tmp_path):
    from io import BytesIO
    import zipfile
    original=artifact()
    raw=BytesIO()
    with zipfile.ZipFile(raw,'w') as bundle:
        for name,content in original.files.items():
            info=zipfile.ZipInfo(name)
            info.external_attr=(0o100755 if name.endswith('.py') else 0o100644)<<16
            bundle.writestr(info,content)
    package=parse_skill_archive(raw.getvalue())
    cache=SkillDirectoryCache(tmp_path/'cache')
    assert cached(cache,package)==package.archive
    assert (cache.root/package.digest/'tree/scripts/run.py').stat().st_mode&0o777==0o555
    assert cache.load(metadata(package))==package
