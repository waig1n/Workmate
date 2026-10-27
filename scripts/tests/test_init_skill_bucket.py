"""Local default initialization never changes an external S3 bucket's policy."""
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import init_skill_bucket as init


@pytest.mark.parametrize('endpoint,expected_calls', [('http://rustfs:9000', 1), ('https://s3.example', 0)])
def test_only_local_rustfs_is_initialized(monkeypatch, endpoint, expected_calls):
    calls = []
    monkeypatch.setattr(init.bucket.Settings, 'from_environment', lambda: SimpleNamespace(endpoint=endpoint))
    monkeypatch.setattr(init.bucket, 'main', lambda: calls.append('initialize') or 0)
    assert init.main() == 0
    assert len(calls) == expected_calls


def test_invalid_initializer_configuration_fails_safely(monkeypatch, capsys):
    def fail():
        raise init.bucket.InitializationError('secret')
    monkeypatch.setattr(init.bucket.Settings, 'from_environment', fail)
    assert init.main() == 1
    assert 'secret' not in capsys.readouterr().err
