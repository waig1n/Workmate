"""Run against an explicitly built wheel, isolated from repository source paths."""
import os
from pathlib import Path
import subprocess
import sys
import zipfile
import pytest


def test_installed_wheel_contains_discoverable_builtin_source_assets(tmp_path):
    wheel=os.environ.get('WORKMATE_TEST_PRODUCT_WHEEL')
    if not wheel:
        pytest.skip('Set WORKMATE_TEST_PRODUCT_WHEEL to the wheel produced by uv build')
    with zipfile.ZipFile(wheel) as bundle:
        bundle.extractall(tmp_path)
    script='''
import asyncio,importlib.abc,sys
from pathlib import Path
class ForbidExecutionImports(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in {'agentscope','workmate_runtime','app'}:
            raise AssertionError('Execution dependency imported: '+fullname)
sys.meta_path.insert(0,ForbidExecutionImports())
from workmate_product.skills.catalog import BuiltinCatalog,builtin_root
assert builtin_root().is_relative_to(Path(sys.argv[1]))
async def check():
    hub=BuiltinCatalog()
    cards=(await hub.list_skills('u',limit=100)).cards
    assert len(cards)>=15
    for card in cards:
        assert await hub.archive('u',card.id)
asyncio.run(check())
'''
    env={**os.environ,'PYTHONPATH':str(tmp_path)}
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],env=env,cwd=tmp_path,
                          capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
