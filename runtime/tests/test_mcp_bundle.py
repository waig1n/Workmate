"""Gateway bundles must execute without the Worker checkout or Product imports."""
import io
from pathlib import Path
import subprocess
import zipfile

import pytest

from workmate_runtime.mcp.bundle import build_gateway_bundle


_RUNTIME_PYTHON = Path(__file__).absolute().parents[1] / '.venv' / 'bin' / 'python'


def test_local_bundle_runs_isolated_help_outside_checkout(tmp_path):
    bundle = tmp_path / 'gateway.pyz'
    bundle.write_bytes(build_gateway_bundle())
    result = subprocess.run([str(_RUNTIME_PYTHON), '-I', str(bundle), '--help'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert '--port' in result.stdout
    assert '--auth-token' in result.stdout
    assert '--instance-nonce' in result.stdout


@pytest.mark.parametrize('remote', [False, True])
def test_bundle_imports_its_own_modules_without_product_or_running_main(tmp_path, remote):
    bundle = tmp_path / 'gateway.pyz'
    data = build_gateway_bundle(remote=remote)
    assert isinstance(data, bytes)
    bundle.write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert '__main__.py' in archive.namelist()
        assert 'workmate_runtime/mcp/server.py' in archive.namelist()
        if remote:
            assert 'workmate_runtime/mcp/remote_server.py' in archive.namelist()
        assert not any(name.startswith(('workmate_product/', 'app/')) for name in archive.namelist())
    # Python -I ignores cwd/PYTHONPATH. A finder rejects accidental Product
    # imports even though the test interpreter has editable packages installed.
    script = '''
import importlib
import importlib.abc
import sys
class RejectProduct(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'workmate_product', 'workmate_contracts', 'app'}:
            raise AssertionError('Gateway imported a Product dependency: ' + fullname)
sys.meta_path.insert(0, RejectProduct())
sys.path.insert(0, sys.argv[1])
module_name = 'workmate_runtime.mcp.remote_server' if sys.argv[2] == 'True' else 'workmate_runtime.mcp.server'
module = importlib.import_module(module_name)
assert callable(module.build_remote_app if sys.argv[2] == 'True' else module.build_app)
for name, loaded in tuple(sys.modules.items()):
    if name == 'workmate_runtime' or name.startswith('workmate_runtime.'):
        assert loaded.__file__.startswith(sys.argv[1] + '/'), (name, loaded.__file__)
print('isolated gateway imports succeeded')
'''
    result = subprocess.run([str(_RUNTIME_PYTHON), '-I', '-c', script, str(bundle), str(remote)],
                            cwd=tmp_path, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'isolated gateway imports succeeded'
