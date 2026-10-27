"""Control use cases retain decisions but delegate SQL to persistence."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1].parent / 'packages/product/src/workmate_product'


def test_control_use_cases_do_not_construct_or_execute_sql():
    for name in ('control_commands', 'full_access', 'external_results'):
        tree = ast.parse((ROOT / 'application' / f'{name}.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Name) and node.func.id in {'text', '_row'}), name
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr in {'execute', 'scalar'}), name


def test_control_repositories_have_no_application_or_transaction_ownership():
    for name in ('control_admission', 'permission_upgrades', 'external_results'):
        tree = ast.parse((ROOT / 'persistence/repositories' / f'{name}.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert 'application' not in (node.module or '')
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {'commit', 'rollback', 'begin', 'begin_nested'}
