"""Public tooling requires explicit targets and preserves safe local defaults."""
import pytest
from scripts import runtime_install, sync_images_to_remote, image_cleanup


def test_sync_requires_explicit_remote():
    with pytest.raises(SystemExit):
        sync_images_to_remote.build_parser().parse_args(['--check'])


def test_remote_install_requires_explicit_remote():
    with pytest.raises(SystemExit):
        runtime_install.parse_args(['--target', 'remote', '--apt', 'curl'])


def test_remote_cleanup_requires_explicit_remote():
    with pytest.raises(SystemExit):
        image_cleanup.parse_args(['--target', 'remote', '--compose-dir', '/deployment'])


def test_local_install_has_no_ssh_target():
    args = runtime_install.parse_args(['--target', 'local', '--apt', 'curl'])
    assert args.remote is None
    assert args.project == 'workmate'


@pytest.mark.parametrize('value', ['', '-oProxyCommand=bad', 'host;command', 'user@host extra'])
def test_reliability_target_rejects_options_and_shell_syntax(value):
    from scripts.reliability.targets import remote_target
    with pytest.raises(ValueError):
        remote_target(value)


def test_reliability_target_has_no_implicit_host(monkeypatch):
    from scripts.reliability.targets import remote_target
    monkeypatch.delenv('WORKMATE_TEST_SSH_TARGET', raising=False)
    with pytest.raises(ValueError):
        remote_target()
    monkeypatch.setenv('WORKMATE_TEST_SSH_TARGET', 'operator@example.test')
    assert remote_target() == 'operator@example.test'


def test_remote_provision_requires_images_before_side_effects(monkeypatch, tmp_path):
    from scripts.reliability import remote_fixture
    monkeypatch.setattr(remote_fixture, 'provision', lambda _: pytest.fail('must validate before provisioning'))
    with pytest.raises(SystemExit):
        remote_fixture.main(['provision', '--run', '--remote', 'operator@example.test', '--output', str(tmp_path / 'run')])
    assert not (tmp_path / 'run').exists()
