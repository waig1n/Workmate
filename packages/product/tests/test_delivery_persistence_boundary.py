"""Delivery orchestration must not acquire its own database implementation."""
import ast
from pathlib import Path


def test_delivery_applications_delegate_sql_to_persistence():
    base = Path(__file__).parents[1] / 'src/workmate_product/application'
    for name in ('delivery.py', 'delivery_unstarted.py', 'execution_quarantine.py'):
        tree = ast.parse((base / name).read_text())
        assert not any(isinstance(node, ast.ImportFrom) and node.module == 'sqlalchemy'
                       for node in ast.walk(tree)), name
        assert not any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                       and node.value.lstrip().upper().startswith(('SELECT ', 'UPDATE ', 'INSERT '))
                       for node in ast.walk(tree)), name


def test_delivery_repositories_do_not_import_application():
    from test_shared_authorization import run_python
    run_python('''
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('workmate_product.application', 'app.', 'workmate_runtime.')):
            raise AssertionError('Forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Reject())
from workmate_product.persistence.repositories import delivery, execution_quarantine
''')
