"""Local wakeup semantics stay independent of transport and persistence adapters."""
import ast
import builtins
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[3]
COORDINATION = ROOT / 'packages/product/src/workmate_product/coordination'


def test_local_signals_load_without_redis_or_sqlalchemy(monkeypatch):
    original = builtins.__import__
    def isolated_import(name, *args, **kwargs):
        if name.split('.')[0] in {'redis', 'sqlalchemy'}:
            raise AssertionError('Local signals must not import a transport or persistence adapter')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', isolated_import)
    signals = runpy.run_path(str(COORDINATION / 'signals.py'))
    assert callable(signals['Pulse']) and callable(signals['wait_for_wakeup'])
    assert signals['FALLBACK_SECONDS'] > 0


def test_execution_and_stream_consumers_use_public_coordination_boundary():
    violations = []
    for directory in ('runtime/workmate_runtime/execution', 'backend/app/services',
                      'packages/product/src/workmate_product/projections'):
        for path in (ROOT / directory).rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                imports = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                           [item.name for item in node.names] if isinstance(node, ast.Import) else [])
                if any(name.startswith('workmate_product.coordination.wakeups') for name in imports):
                    violations.append(str(path.relative_to(ROOT)))
    assert not violations, f'Consumers import Redis adapter internals: {violations}'
