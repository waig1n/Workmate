import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.reliability.cleanup import attempt, restore_targets, remaining_resources


def test_failure_does_not_interrupt_cleanup():
    errors = []
    assert attempt(errors, 'broken', lambda: (_ for _ in ()).throw(RuntimeError('failed'))) is None
    assert attempt(errors, 'next', lambda: 42) == 42
    assert errors == [{'operation': 'broken', 'error': 'RuntimeError: failed'}]


def test_partial_restore_ledger_requires_owned_volume(tmp_path):
    identity = 'workmate-restore-' + 'a' * 32
    volume = identity + '-0'
    (tmp_path / (identity + '.json')).write_text(json.dumps({'source': volume}))
    def docker(*args):
        assert args == ('volume', 'inspect', volume)
        return json.dumps([{'Name': volume, 'Labels': {'workmate.reliability.backup': identity}}])
    assert restore_targets(tmp_path, docker) == [volume]
    with pytest.raises(ValueError):
        restore_targets(tmp_path, lambda *args: json.dumps([{'Name': volume, 'Labels': {}}]))


def test_remaining_checks_source_target_and_restore_volumes():
    calls = []
    def docker(*args):
        calls.append(args)
        return 'leftover' if args[-1] == 'label=com.docker.compose.project=source-restored' else ''
    result = remaining_resources(docker, ['source', 'source-restored'], ['workmate-restore-' + 'a'*32])
    assert result['containers'] == ['leftover']
    assert any('label=workmate.reliability.backup=' in args[-1] for args in calls)


def test_nginx_outer_owner_is_explicit_and_validated(monkeypatch, tmp_path):
    import importlib.util
    path = Path(__file__).resolve().parents[2] / 'backend/tests/gateway_lb.py'
    spec = importlib.util.spec_from_file_location('reliability_nginx_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    owner = 'gateway-drill-' + 'a' * 12
    monkeypatch.setenv('WORKMATE_TEST_ACCEPTANCE_OWNER', owner)
    command = module.container_command(tmp_path, 'sha256:'+'b'*64, 'workmate-lb-'+'c'*32)
    assert 'workmate.validation.owner=' + owner in command
    assert 'workmate.acceptance.role=proxy' in command
    monkeypatch.setenv('WORKMATE_TEST_ACCEPTANCE_OWNER', 'business')
    with pytest.raises(ValueError):
        module.container_command(tmp_path, 'sha256:'+'b'*64, 'workmate-lb-'+'c'*32)
