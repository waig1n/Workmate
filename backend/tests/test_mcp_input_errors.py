"""Credential-safe field errors for connection forms and API callers."""
import pytest
from workmate_product.mcp.custom import parse_custom
from workmate_product.mcp.definitions import MCPDefinitionError
from app.api.routers.mcps import _failure


@pytest.mark.parametrize(('changes', 'code'), [
    ({'name': ''}, 'invalid_name'),
    ({'url': 'not-a-url'}, 'invalid_url'),
    ({'url': 'http://127.0.0.1:7676/mcp'}, 'blocked_target'),
    ({'url': 'http://localhost:7676/mcp'}, 'blocked_target'),
    ({'url': 'http://[::1]:7676/mcp'}, 'blocked_target'),
    ({'url': 'https://tools.example/mcp?key=private-test-token'}, 'invalid_url'),
    ({'headers': {'Authorization': 'private-test-token\r\n'}}, 'invalid_headers'),
    ({'timeout': 0}, 'invalid_timeout'),
    ({'command': 'private-test-token'}, 'invalid_configuration'),
])
def test_input_errors_identify_field_without_echoing_private_values(changes, code):
    with pytest.raises(MCPDefinitionError) as caught:
        parse_custom({'name': 'test', 'url': 'https://tools.example/mcp', **changes})
    error = _failure(caught.value)
    assert error.status_code == 422
    assert isinstance(error.detail, dict)
    assert error.detail['code'] == code
    assert 'private-test-token' not in str(error.detail)
