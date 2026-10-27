"""Execute the actual isolated packaging script against hostile filesystem entries."""
import base64,json,subprocess,sys
from workmate_runtime.assembly.platform_skill_package import PACKAGE_SCRIPT
from workmate_product.application.skill_artifacts import parse_skill_archive


def run(root):
    return subprocess.run([sys.executable,'-I','-c',PACKAGE_SCRIPT,str(root)],capture_output=True,timeout=15)


def test_package_deterministic_and_rejects_links(tmp_path):
    root=tmp_path/'skill'
    root.mkdir()
    (root/'SKILL.md').write_text('---\nname: sample\ndescription: Sample\n---\nBody')
    first=run(root)
    assert first.returncode==0,first.stderr
    assert run(root).stdout==first.stdout
    package=parse_skill_archive(base64.b64decode(json.loads(first.stdout)['archive']))
    assert package.name=='sample'
    (root/'link').symlink_to('/etc/passwd')
    assert run(root).returncode!=0


def test_package_rejects_parent_link_and_extra_entry_count(tmp_path):
    source=tmp_path/'source'
    source.mkdir()
    (tmp_path/'linked').symlink_to(source)
    assert run(tmp_path/'linked').returncode!=0
    for index in range(1001):
        (source/str(index)).mkdir()
    assert run(source).returncode!=0


async def test_wrapper_uses_tracked_backend_and_enforces_working_path(tmp_path):
    import asyncio
    from types import SimpleNamespace
    import pytest
    from workmate_runtime.assembly.platform_skill_package import package_skill
    from workmate_runtime.execution.resources import TrustedTaskBackend
    from workmate_runtime.transport.postgres import ExecutionOwner
    owner=ExecutionOwner('s','t','worker',1)
    class Backend(TrustedTaskBackend):
        def __init__(self): self._execution_owner=owner
        async def _exec_with_input(self,argv,data):
            process=await asyncio.create_subprocess_exec(*argv,stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await process.communicate(data)
            return SimpleNamespace(exit_code=process.returncode,stdout=stdout,stderr=stderr)
    binding=SimpleNamespace(owner=owner,backend=Backend(),workspace=SimpleNamespace(workdir=str(tmp_path)))
    directory=tmp_path/'working'/'example'
    directory.mkdir(parents=True)
    (directory/'SKILL.md').write_text('---\nname: sample\ndescription: Sample\n---\nBody')
    raw,summary=await package_skill(binding,'working/example')
    assert summary['sha256']==parse_skill_archive(raw).digest
    assert summary['files']==1 and summary['name']=='sample'
    for path in ('/etc','working/../elsewhere','working//sample','input/sample'):
        with pytest.raises(ValueError): await package_skill(binding,path)
    with pytest.raises(RuntimeError): await package_skill(None,'working/example')
    with pytest.raises(RuntimeError): await package_skill(binding,'working/missing')
    binding.workspace.workdir='relative'
    with pytest.raises(RuntimeError): await package_skill(binding,'working/example')
