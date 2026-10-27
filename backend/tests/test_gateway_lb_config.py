"""Safety and streaming contract of the local Nginx acceptance fixture."""
import pytest
from gateway_lb import nginx_config


def test_nginx_keeps_streams_unbuffered_and_preserves_upstream_identity(tmp_path):
    config = nginx_config(tmp_path, 19000, [19001, 19002])
    assert 'proxy_buffering off;' in config
    assert 'proxy_request_buffering off;' in config
    assert '127.0.0.1:19001' in config and '127.0.0.1:19002' in config
    assert 'X-Acceptance-Upstream $upstream_addr always' in config
    assert 'non_idempotent' not in config
    assert 'listen 127.0.0.1:19000;' in config


@pytest.mark.parametrize('listen,upstreams', [(0,[1,2]), (65536,[1,2]), (1,[]), (1,[2]), (1,[2,2]), (1,[1,2])])
def test_nginx_rejects_invalid_or_ambiguous_ports(tmp_path, listen, upstreams):
    with pytest.raises(ValueError):
        nginx_config(tmp_path, listen, upstreams)


def test_container_command_is_owned_and_pinned(tmp_path):
    from gateway_lb import container_command
    image = 'sha256:' + 'a' * 64
    command = container_command(tmp_path, image, 'workmate-lb-' + 'b' * 32)
    assert '--network=host' in command
    assert '--read-only' in command
    assert '--pull=never' in command
    assert '--mount' in command
    assert f'type=bind,src={tmp_path},dst={tmp_path}' in command
    assert image in command
    assert '/var/run/docker.sock' not in ' '.join(command)


@pytest.mark.parametrize('image,name', [('nginx:latest', 'workmate-lb-' + 'b'*32),
    ('sha256:'+'a'*64, 'business-nginx')])
def test_container_command_rejects_mutable_images_or_unowned_names(tmp_path, image, name):
    from gateway_lb import container_command
    with pytest.raises(ValueError):
        container_command(tmp_path, image, name)
