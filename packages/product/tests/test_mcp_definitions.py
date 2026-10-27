"""MCP templates and private values are independent of AgentScope App."""
import pytest
from workmate_product.mcp.definitions import MCPCard, render_mcp, MCPDefinitionError


def card(**overrides):
    return MCPCard(hub_id='github',id='example',name='example',
        inputs_schema={'type':'object','properties':{'TOKEN':{'type':'string'},'OPTIONAL':{'type':'string'}},'required':['TOKEN']},
        config_template={'type':'stdio_mcp','command':'uvx','args':['example'],
                         'env':{'API_KEY':'${TOKEN}','OPTIONAL':'${OPTIONAL}'}},**overrides)


def test_render_uses_validated_defaults_and_omits_unset_optional_env():
    rendered=render_mcp(card(),{'TOKEN':'private-token'})
    assert rendered.client.name=='example'
    assert rendered.client.mcp_config.env=={'API_KEY':'private-token'}
    assert 'private-token' not in repr(rendered)
    assert 'private-token' not in repr(rendered.client)


def test_invalid_inputs_never_echo_secret_values():
    with pytest.raises(MCPDefinitionError) as error:
        render_mcp(card(),{'TOKEN':{'private-token':'secret-value'}})
    assert 'private-token' not in str(error.value) and 'secret-value' not in str(error.value)
    with pytest.raises(MCPDefinitionError):
        render_mcp(card(),{'TOKEN':'value'},name='bad mcp name')


def test_http_url_and_headers_are_confidential_and_stdio_requires_stateful():
    public=MCPCard(hub_id='github',id='remote',name='remote',is_stateful=False,
        config_template={'type':'http_mcp','url':'https://example.test/mcp?key=${TOKEN}',
                         'headers':{'Authorization':'Bearer ${TOKEN}'}},
        inputs_schema={'properties':{'TOKEN':{'type':'string'}},'required':['TOKEN']})
    rendered=render_mcp(public,{'TOKEN':'private-value'})
    assert rendered.client.mcp_config.url.endswith('private-value')
    assert 'private-value' not in repr(rendered.client.mcp_config)
    with pytest.raises(MCPDefinitionError):
        render_mcp(card(is_stateful=False),{'TOKEN':'private-value'})


def test_local_schema_refs_defaults_and_literal_undeclared_dollars_are_preserved():
    item=card()
    item.inputs_schema={'type':'object','$defs':{'token':{'type':'string'}},
        'properties':{'TOKEN':{'$ref':'#/$defs/token','default':'default-token'}}}
    item.config_template.args=['$HOME','${TOKEN}']
    rendered=render_mcp(item,{})
    assert rendered.client.mcp_config.args==['$HOME','default-token']
    item.inputs_schema['properties']['TOKEN']={'$ref':'https://example.invalid/private-schema'}
    with pytest.raises(MCPDefinitionError):
        render_mcp(item,{'TOKEN':'value'})
