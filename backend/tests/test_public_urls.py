"""Shared deployment URL validation, link construction and actionable enablement errors."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi import HTTPException
from app.config import Settings
from app.channels.management import ChannelManagement
from app.channels.rules import ChannelUpdate


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    monkeypatch.delenv('WORKMATE_PUBLIC_URL', raising=False)
    monkeypatch.delenv('CHANNEL_PUBLIC_BASE_URL', raising=False)


def test_public_url_defaults_empty_and_env_override(monkeypatch):
    assert Settings(_env_file=None).workmate_public_url == ''
    monkeypatch.setenv('WORKMATE_PUBLIC_URL', 'https://work.example/workmate')
    assert Settings(_env_file=None).workmate_public_url == 'https://work.example/workmate'
    monkeypatch.setenv('WORKMATE_PUBLIC_URL', 'http://192.0.2.10:9091')
    assert Settings(_env_file=None).workmate_public_url == 'http://192.0.2.10:9091'
    assert Settings(_env_file=None, workmate_public_url='https://override.example').workmate_public_url == 'https://override.example'


@pytest.mark.parametrize('value', [
    'http://user:secret@example.com', 'https://user:secret@example.com',
    'https://example.com?token=foo', 'https://example.com#fragment',
    'https://example.com:bad', 'https://example.com:65536',
    'https://', 'ftp://localhost', '//example.com',
    'http://192.0.2.10:9091?token=foo', 'http://example.com#fragment',
    'http://example.com:bad', 'http://example.com:65536',
    'http://example.com/a b', 'http://example.com/a\\b',
])
def test_invalid_public_urls_fail_early(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, workmate_public_url=value)


@pytest.mark.parametrize('value', [
    '', 'https://example.com', 'https://example.com/workmate/',
    'http://localhost:3000', 'http://127.0.0.1:3000',
    'http://192.0.2.10:9091', 'http://example.com/workmate/',
])
def test_safe_public_urls_are_accepted(value):
    assert Settings(_env_file=None, workmate_public_url=value).workmate_public_url == value.rstrip('/')


@pytest.mark.parametrize('base,conversation,channels', [
    ('https://example.com', 'https://example.com/workspace/c/c1', 'https://example.com/workspace/channels'),
    ('https://example.com/workmate/', 'https://example.com/workmate/workspace/c/c1', 'https://example.com/workmate/workspace/channels'),
    ('http://192.0.2.10:9091', 'http://192.0.2.10:9091/workspace/c/c1', 'http://192.0.2.10:9091/workspace/channels'),
    ('http://localhost:3000/', 'http://localhost:3000/workspace/c/c1', 'http://localhost:3000/workspace/channels'),
])
def test_links_preserve_deployment_prefix(base, conversation, channels):
    from app.services.public_urls import WorkmateLinks
    links = WorkmateLinks(base)
    assert links.configured
    assert links.conversation('c1') == conversation
    assert links.channels() == channels


def test_links_report_unconfigured_url():
    from app.services.public_urls import WorkmateLinks
    assert WorkmateLinks('').configured is False


async def enabling_error(monkeypatch, public_url, credentials):
    service = ChannelManagement(None, None, SimpleNamespace(enabled=True, wechat_enabled=True), public_url)
    channel = SimpleNamespace(id='ch', provider='wecom', version=1, enabled=False,
        agent_id='agent', credentials=credentials)
    monkeypatch.setattr('app.channels.management.lock_capacity', AsyncMock())
    monkeypatch.setattr(service, 'get', AsyncMock(return_value=channel))
    monkeypatch.setattr(service, 'validate_agent', AsyncMock())
    db = MagicMock()
    db.commit = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await service.update(db, 'user', 'ch', ChannelUpdate(version=1, name='Bot', agent_id='agent', enabled=True))
    assert error.value.status_code == 422
    db.commit.assert_not_awaited()
    return error.value.detail


async def test_enable_missing_address_names_deployment_variable(monkeypatch):
    detail = await enabling_error(monkeypatch, '', {'bot_id':'bot', 'secret':'private'})
    assert 'WORKMATE_PUBLIC_URL' in detail
    assert '凭据' not in detail and 'private' not in detail


async def test_enable_missing_credentials_is_separate_from_address(monkeypatch):
    detail = await enabling_error(monkeypatch, 'https://work.example', {})
    assert '凭据' in detail
    assert '公开地址' not in detail and 'WORKMATE_PUBLIC_URL' not in detail


@pytest.mark.parametrize('method,args', [('channels', ()), ('conversation', ('c1',))])
def test_unconfigured_links_never_emit_relative_notification_urls(method, args):
    from app.services.public_urls import WorkmateLinks
    with pytest.raises(ValueError, match='WORKMATE_PUBLIC_URL'):
        getattr(WorkmateLinks(''), method)(*args)


def test_conversation_identifier_is_encoded_as_one_path_segment():
    from app.services.public_urls import WorkmateLinks
    assert WorkmateLinks('https://work.example/').conversation('a/b?x#y') == 'https://work.example/workspace/c/a%2Fb%3Fx%23y'


async def test_supervisor_without_public_url_waits_without_connecting(monkeypatch):
    from app.channels.supervisor import ChannelSupervisor
    from app.services.public_urls import WorkmateLinks
    service = SimpleNamespace(sessions=None, policy=SimpleNamespace(enabled=True), links=WorkmateLinks(''))
    supervisor = ChannelSupervisor(service)
    monkeypatch.setattr(supervisor, 'connections', AsyncMock())
    shutdown = SimpleNamespace(wait=AsyncMock())
    await supervisor.serve(shutdown)
    shutdown.wait.assert_awaited_once()
    supervisor.connections.assert_not_awaited()


def test_deployment_passes_shared_frontend_url_only_to_gateway():
    from pathlib import Path
    import yaml
    root = Path(__file__).parents[2]
    services = yaml.safe_load((root / 'docker-compose.yml').read_text())['services']
    assert services['gateway']['environment']['WORKMATE_PUBLIC_URL'] == '${WORKMATE_PUBLIC_URL:-}'
    for name, service in services.items():
        if name != 'gateway':
            assert 'WORKMATE_PUBLIC_URL' not in service.get('environment', {})
    assert 'WORKMATE_PUBLIC_URL=' in (root / '.env.example').read_text()


def test_old_channel_setting_does_not_define_product_address(monkeypatch):
    monkeypatch.setenv('CHANNEL_PUBLIC_BASE_URL', 'https://old.example')
    assert Settings(_env_file=None).workmate_public_url == ''
