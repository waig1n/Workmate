import pytest
from pydantic import ValidationError
from app.channels.rules import ChannelCreate, ChannelUpdate, credentials_for, render_reply


def test_group_requires_allowlist():
    with pytest.raises(ValidationError):
        ChannelCreate(name='Test', provider='wecom', agent_id='a', group_enabled=True,
                      credentials={'bot_id':'b','secret':'s'})


def test_credentials_are_strict_and_blank_is_not_a_secret():
    assert credentials_for('wecom', {'bot_id':'b','secret':'s'}) == {'bot_id':'b','secret':'s'}
    for data in ({'bot_id':'b','secret':''}, {'bot_id':'b','secret':'s','url':'https://evil.test'}):
        with pytest.raises(ValueError):
            credentials_for('wecom', data)


def test_group_render_does_not_include_any_model_or_task_text():
    text = render_reply('group','completed','private salary secret','https://work.test/workspace/c/c')
    assert 'private' not in text and 'salary' not in text
    assert 'https://work.test/workspace/c/c' in text


def test_private_render_has_bounded_body_and_workbench_link():
    text = render_reply('private','completed','x'*20000,'https://work.test/workspace/c/c')
    assert len(text.encode()) <= 3500 and text.endswith('https://work.test/workspace/c/c')


def test_only_final_private_response_has_model_content():
    for state in ('awaiting_permission','failed','running','created'):
        assert 'secret' not in render_reply('private',state,'secret','https://work.test')


def test_wechat_disallows_group_and_client_supplied_token():
    with pytest.raises(ValidationError):
        ChannelCreate(name='Mine',provider='wechat',agent_id='a',group_enabled=True,group_allowlist=['g'])
    with pytest.raises(ValueError):
        credentials_for('wechat',{'bot_token':'forged'})


def test_multibyte_private_render_fits_platform_byte_limit():
    text = render_reply('private','completed','汉字🙂'*10000,'https://work.test/workspace/c/c')
    assert len(text.encode('utf-8')) <= 3500
    assert text.endswith('https://work.test/workspace/c/c')


def test_dingtalk_template_is_optional_bounded_and_unknown_fields_rejected():
    values={'client_id':'id','client_secret':'secret'}
    assert credentials_for('dingtalk',values)==values
    assert credentials_for('dingtalk',{**values,'card_template_id':'  template  '})['card_template_id']=='template'
    assert credentials_for('dingtalk',{**values,'card_template_id':''})==values
    for extra in ({'card_template_id':None},{'card_template_id':'x'*257},{'other':'bad'}):
        with pytest.raises(ValueError): credentials_for('dingtalk',{**values,**extra})


def test_artifact_delivery_has_optional_workbench_entry():
    text=render_reply('private','completed','Files are ready.','https://work.test',has_artifacts=True)
    assert text=='Files are ready.\n查看文件：https://work.test'
