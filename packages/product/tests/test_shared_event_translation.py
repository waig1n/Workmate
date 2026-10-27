"""The event converter must remain importable by Runtime without Backend."""
import os
from pathlib import Path
import subprocess
import sys


def test_converter_import_and_permission_translation_without_backend(tmp_path):
    root=Path(__file__).resolve().parents[3]
    env={**os.environ,'PYTHONPATH':os.pathsep.join(str(root/p) for p in (
        'packages/product/src','packages/contracts/src'))}
    result=subprocess.run([sys.executable,'-c', '''
import sys
class RejectBackend:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app' or fullname.startswith('app.'):
            raise AssertionError('Shared converter cannot import Backend')
sys.meta_path.insert(0,RejectBackend())
from workmate_product.projections.translator import TranslationState, translate_event
from workmate_product.approval import approval_request_version
state=TranslationState()
translate_event({'type':'REPLY_START','reply_id':'r'},'c',state)
frames=translate_event({'type':'TEXT_BLOCK_START','reply_id':'r','block_id':'block'},'c',state)
assert frames[0]['messageId']=='block'
assert approval_request_version('r',[{'id':'x','name':'Write','input':'{}'}]).startswith('approval-')
assert 'app' not in sys.modules
'''],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
