"""Immutable Skill package parsing, independent of App and host extraction."""
from io import BytesIO
import zipfile

import pytest

from workmate_product.application.skill_artifacts import parse_skill_archive,SkillArtifactError,discover_skill_directories


def archive(files=None,*,symlink=False):
    output=BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as bundle:
        for name,content in (files or {'SKILL.md':'---\nname: sample\ndescription: Example workflow\nversion: 1\n---\nDo the work.','scripts/run.py':'print(1)'}).items():
            info=zipfile.ZipInfo(name)
            if symlink:
                info.create_system=3
                info.external_attr=0o120777<<16
            bundle.writestr(info,content)
    return output.getvalue()


def test_archive_preserves_binary_files_metadata_and_stable_digest():
    raw=archive()
    parsed=parse_skill_archive(raw)
    assert parsed.name=='sample' and parsed.description=='Example workflow' and parsed.version=='1'
    assert parsed.markdown.strip()=='Do the work.'
    assert parsed.files['scripts/run.py']==b'print(1)'
    assert parsed.digest==parse_skill_archive(raw).digest
    assert len(parsed.digest)==64


@pytest.mark.parametrize('files',[
    {'../SKILL.md':'bad'}, {'/SKILL.md':'bad'}, {'a\\SKILL.md':'bad'},
    {'SKILL.md':'No metadata'}, {'SKILL.md':'---\nname: x\n---\nMissing description'},
    {'SKILL.md':'---\nname: x\ndescription: yes\n---\nInvalid bool'},
    {'one/SKILL.md':'x','two/SKILL.md':'x'}, {'SKILL.md':'x','outside/../file':'x'},
])
def test_malformed_or_ambiguous_archives_fail_closed(files):
    with pytest.raises(SkillArtifactError):
        parse_skill_archive(archive(files))


def test_symlinks_and_duplicate_paths_are_rejected():
    with pytest.raises(SkillArtifactError):
        parse_skill_archive(archive(symlink=True))
    raw=BytesIO()
    with zipfile.ZipFile(raw,'w') as bundle:
        bundle.writestr('SKILL.md','x')
        with pytest.warns(UserWarning):
            bundle.writestr('SKILL.md','y')
    with pytest.raises(SkillArtifactError):
        parse_skill_archive(raw.getvalue())


def test_one_wrapper_directory_is_normalized_without_losing_files():
    raw=archive({'sample/SKILL.md':'---\nname: sample\ndescription: Workflow\n---\nWork.',
                 'sample/scripts/run.py':'print(1)'})
    assert set(parse_skill_archive(raw).files)=={'SKILL.md','scripts/run.py'}


def test_builtin_discovery_uses_skill_md_directories_and_rejects_symlinks(tmp_path):
    (tmp_path/'sample').mkdir()
    (tmp_path/'sample'/'SKILL.md').write_text('source')
    (tmp_path/'unlisted').mkdir()
    (tmp_path/'fake').symlink_to(tmp_path/'sample',target_is_directory=True)
    assert discover_skill_directories(tmp_path)=={'sample':tmp_path/'sample'}


def test_builtin_packages_are_discovered_from_real_directories_and_are_deterministic():
    from pathlib import Path
    from workmate_product.application.skill_artifacts import package_skill_directory
    from workmate_product.skills.catalog import builtin_root
    root=builtin_root()
    directories=discover_skill_directories(root)
    assert 'agent-bootstrap' in directories and 'skill-creator' in directories
    for name,directory in directories.items():
        first=package_skill_directory(directory)
        assert first.name==name
        assert first.digest==package_skill_directory(directory).digest
        assert not any(part=='evals' for file in first.files for part in file.split('/'))


def test_source_directory_rejects_symlink_and_declared_name_mismatch(tmp_path):
    from workmate_product.application.skill_artifacts import package_skill_directory
    source=tmp_path/'sample'
    source.mkdir()
    (source/'SKILL.md').write_text('---\nname: mismatch\ndescription: Wrong name\n---\nWork')
    with pytest.raises(SkillArtifactError,match='match'):
        package_skill_directory(source)
    (source/'SKILL.md').write_text('---\nname: sample\ndescription: Correct\n---\nWork')
    (source/'leak').symlink_to(tmp_path/'elsewhere')
    with pytest.raises(SkillArtifactError,match='symbolic'):
        package_skill_directory(source)


def test_source_names_preserve_product_128_character_alias_limit():
    name='_'+('a'*127)
    parsed=parse_skill_archive(archive({'SKILL.md':f'---\nname: {name}\ndescription: Long source name\n---\nWork.'}))
    assert parsed.name==name
    with pytest.raises(SkillArtifactError):
        parse_skill_archive(archive({'SKILL.md':f'---\nname: {name}a\ndescription: Too long\n---\nWork.'}))
