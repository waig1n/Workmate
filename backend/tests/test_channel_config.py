import pytest
import yaml
from pathlib import Path
from app.config import Settings
from workmate_product.config.channels import ChannelPolicy

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text('version: 1\n')
    monkeypatch.setenv('WORKMATE_CONFIG_FILE', str(path))
    monkeypatch.delenv('WORKMATE_PUBLIC_URL', raising=False)
    for key in ChannelPolicy.model_fields:
        monkeypatch.delenv('CHANNEL_' + key.upper(), raising=False)
    return path

def test_defaults():
    assert ChannelPolicy.from_settings(Settings(_env_file=None)) == ChannelPolicy()
    config = yaml.safe_load((Path(__file__).parents[2] / 'config/workmate.yaml').read_text())
    assert config['product']['channels'] == ChannelPolicy().model_dump()

@pytest.mark.parametrize('field,value,override', [('reply_delay_secs',30,60),('stream_interval_secs',1,2),('max_live_replies',64,128),('enabled',False,True),('wechat_enabled',True,False),('poll_interval_secs',4,6),('batch_size',5,7),('lease_secs',40,80),('max_connections',4,5),('max_inbox_pending',6,9),('message_max_chars',500,900)])
def test_yaml_and_env(isolated, monkeypatch, field, value, override):
    isolated.write_text(yaml.safe_dump({'product': {'channels': {field:value}}}))
    assert getattr(Settings(_env_file=None),'channel_'+field) == value
    monkeypatch.setenv('CHANNEL_'+field.upper(),str(override))
    assert getattr(Settings(_env_file=None),'channel_'+field) == override

@pytest.mark.parametrize('field,value', [('reply_delay_secs',29),('reply_delay_secs',301),('stream_interval_secs',0),('stream_interval_secs',11),('max_live_replies',0),('max_live_replies',257),('poll_interval_secs',0),('batch_size',101),('lease_secs',29),('max_connections',257),('max_inbox_pending',0),('message_max_chars',64001)])
def test_invalid(isolated,monkeypatch,field,value):
    isolated.write_text(yaml.safe_dump({'product':{'channels':{field:value}}}))
    with pytest.raises(ValueError): Settings(_env_file=None)
    isolated.write_text('version: 1\n')
    monkeypatch.setenv('CHANNEL_'+field.upper(), str(value))
    with pytest.raises(ValueError): Settings(_env_file=None)

@pytest.mark.parametrize('url',['http://user:secret@example.com','https://user:secret@example.com','https://example.com?token=foo','https://example.com#x'])
def test_public_url_rejects_unsafe(url):
    with pytest.raises(ValueError): Settings(_env_file=None,workmate_public_url=url)

@pytest.mark.parametrize('url',['','https://example.com/work','http://192.0.2.10:9091','http://localhost:3000','http://127.0.0.1:3000'])
def test_public_url_accepts(url):
    assert Settings(_env_file=None,workmate_public_url=url).workmate_public_url == url

@pytest.mark.parametrize('field,default,minimum,maximum', [
    ('reply_delay_secs',30,30,300),
    ('stream_interval_secs',1,1,10),
    ('max_live_replies',64,1,256),
])
def test_stream_policy_defaults_and_bounds(field,default,minimum,maximum):
    assert getattr(ChannelPolicy(),field) == default
    for value in (minimum,maximum):
        assert getattr(ChannelPolicy(**{field:value}),field) == value
    for value in (minimum-1,maximum+1):
        with pytest.raises(ValueError):
            ChannelPolicy(**{field:value})
