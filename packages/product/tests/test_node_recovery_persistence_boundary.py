"""Node authority policy remains independent of SQL and repository imports."""
import ast
from pathlib import Path

from test_shared_authorization import run_python


ROOT = Path(__file__).resolve().parents[1] / 'src' / 'workmate_product'


def test_node_recovery_application_does_not_construct_sql():
    tree = ast.parse((ROOT / 'application/node_recovery.py').read_text())
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert 'sqlalchemy' not in imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert not (isinstance(node.func.value, ast.Name)
                        and node.func.value.id == 'db'
                        and node.func.attr in {'execute', 'scalar'})


def test_node_recovery_repository_imports_without_application_or_web():
    run_python('''
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.startswith('workmate_product.application')
                or fullname.split('.')[0] in {'fastapi','agentscope','app','backend'}):
            raise AssertionError('Forbidden dependency: '+fullname)
sys.meta_path.insert(0, Reject())
from workmate_product.persistence.repositories import node_recovery
assert callable(node_recovery.lock_resource_ledger)
''')


def test_node_recovery_repository_cannot_own_transaction_or_connection():
    tree = ast.parse((ROOT / 'persistence/repositories/node_recovery.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {
                'commit', 'rollback', 'begin', 'begin_nested', 'connect', 'connection',
            }
