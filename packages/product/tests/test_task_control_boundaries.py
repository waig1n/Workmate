"""Task use cases must be reusable without HTTP or Runtime implementation imports."""
import ast
from pathlib import Path
from test_shared_authorization import run_python

ROOT = Path(__file__).resolve().parents[3]


def test_task_controls_are_shared_and_sql_free():
    run_python('''
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi', 'app', 'backend', 'workmate_runtime', 'agentscope'}:
            raise AssertionError('Forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Reject())
from workmate_product.application.task_controls import TaskControls
''')
    for name in ('commands', 'task_controls'):
        tree = ast.parse((ROOT / f'packages/product/src/workmate_product/application/{name}.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'sqlalchemy':
                assert not {'text', 'select', 'update', 'insert', 'delete'} & {a.name for a in node.names}


def test_command_repositories_import_without_application():
    run_python('''
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.split('.')[0] in {'fastapi', 'app', 'backend', 'workmate_runtime', 'agentscope'}
            or fullname.startswith('workmate_product.application')):
            raise AssertionError('Forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Reject())
from workmate_product.persistence.repositories import start_admission, task_controls
''')


def test_repositories_never_own_transactions_or_open_connections():
    """All repositories share caller locks and rollback, including future modules."""
    directory = ROOT / 'packages/product/src/workmate_product/persistence/repositories'
    forbidden = {'commit', 'rollback', 'begin', 'begin_nested', 'connection', 'connect',
                 'create_engine', 'create_async_engine', 'AsyncSession', 'Session',
                 'sessionmaker', 'async_sessionmaker'}
    for path in directory.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
                assert name not in forbidden, f'{path.name}:{node.lineno}: repository owns {name}'


def test_all_repositories_have_one_way_dependencies():
    run_python('''
import importlib
import pkgutil
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.split('.')[0] in {'fastapi', 'app', 'backend', 'workmate_runtime', 'agentscope'}
            or fullname.startswith('workmate_product.application')):
            raise AssertionError('Forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Reject())
from workmate_product.persistence import repositories
for module in pkgutil.iter_modules(repositories.__path__, repositories.__name__ + '.'):
    importlib.import_module(module.name)
''')


def test_governed_applications_do_not_pass_sql_to_helpers():
    """A helper must not hide a new SQL statement in the use-case layer."""
    import re
    names = ('commands', 'task_controls', 'control_commands', 'full_access',
             'external_results', 'delivery', 'delivery_unstarted',
             'execution_quarantine', 'node_recovery', 'resource_lists', 'catalog_status')
    sql_start = re.compile(r'^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\s', re.I)
    for name in names:
        path = ROOT / f'packages/product/src/workmate_product/application/{name}.py'
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        assert not sql_start.match(argument.value), f'{name}:{node.lineno}: embedded SQL'
