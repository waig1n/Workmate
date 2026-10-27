"""Private custom HTTP connection input; no executable command or public secrets."""
import ipaddress
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError
from .definitions import MCPCard, HttpMCPConfig, MCPDefinitionError

CUSTOM_HUB = 'workmate-custom-http'
_RESERVED_HEADERS = {'host', 'content-length', 'transfer-encoding', 'connection', 'upgrade',
                     'proxy-authorization', 'proxy-connection'}


class CustomHTTPConnection(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, strict=True)
    name: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    display_name: str | None = Field(default=None, max_length=200)
    description: str = Field(default='', max_length=2000)
    url: str = Field(min_length=1, max_length=2048, repr=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    timeout: float = Field(default=30, ge=1, le=120)
    is_stateful: bool = False

    @field_validator('url')
    @classmethod
    def safe_url(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or '?' in value or '#' in value
                or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
            raise ValueError('Invalid HTTP endpoint')
        # DNS and redirect policy belongs to the actual network transport.
        if parsed.port == 0 or parsed.hostname.lower().rstrip('.') in {
                'localhost', 'metadata.google.internal', 'gateway', 'runtime', 'postgres', 'redis', 'rustfs'}:
            raise PydanticCustomError('mcp_target_blocked', 'Reserved HTTP endpoint')
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and (address.is_loopback or address.is_link_local
                or address.is_unspecified or address.is_multicast or address.is_reserved
                or getattr(address, 'ipv4_mapped', None)):
            raise PydanticCustomError('mcp_target_blocked', 'Reserved HTTP endpoint')
        return value

    @field_validator('headers')
    @classmethod
    def safe_headers(cls, value):
        if len(value) > 32 or sum(len(key) + len(item) for key, item in value.items()) > 16384:
            raise ValueError('Too many HTTP headers')
        lowered = set()
        for key, item in value.items():
            if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", key)
                    or key.lower() in _RESERVED_HEADERS or key.lower() in lowered
                    or not item.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in item)):
                raise ValueError('Invalid HTTP header')
            lowered.add(key.lower())
        return value

    def card(self, *, card_id=None):
        return MCPCard(hub_id=CUSTOM_HUB, id=card_id or self.name, name=self.name,
            display_name=self.display_name, description=self.description, auth='none',
            is_stateful=self.is_stateful, inputs_schema={'type': 'object', 'additionalProperties': False},
            config_template=HttpMCPConfig(url=self.url, headers=self.headers, timeout=self.timeout))


def parse_custom(payload):
    try:
        return CustomHTTPConnection.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        if any(error['type'] == 'mcp_target_blocked' for error in errors):
            code, message = 'blocked_target', 'This address is not an allowed server-side MCP target'
        else:
            field = errors[0]['loc'][0] if errors and errors[0]['loc'] else None
            code, message = {
                'name': ('invalid_name', 'Connection name must use 1-128 letters, digits, underscores or hyphens'),
                'url': ('invalid_url', 'Enter an HTTP endpoint without embedded credentials, query or fragment'),
                'headers': ('invalid_headers', 'Check HTTP header names and values'),
                'timeout': ('invalid_timeout', 'Connection timeout must be between 1 and 120 seconds'),
            }.get(field, ('invalid_configuration', 'Invalid HTTP MCP configuration'))
        raise MCPDefinitionError(message, 422, code=code) from None
    except Exception:
        raise MCPDefinitionError('Invalid HTTP MCP configuration', 422, code='invalid_configuration') from None


def editable_configuration(installed, private):
    """Management-only view; all submitted credentials remain write-only."""
    client = private.client
    custom = private.card.hub_id == CUSTOM_HUB
    result = {'source': 'custom' if custom else 'catalog', 'name': installed.name,
        'display_name': installed.display_name, 'description': installed.metadata_.get('description', ''),
        'is_stateful': client.is_stateful, 'header_names': [],
        'configured_fields': sorted(private.values)}
    if custom:
        result.update(url=client.mcp_config.url, timeout=client.mcp_config.timeout,
                      header_names=sorted((client.mcp_config.headers or {}).keys()))
    else:
        schema = private.card.inputs_schema
        # Defaults/examples can carry credentials; do not return them to a browser.
        properties = {key: {name: value for name, value in spec.items()
                           if name in {'title', 'description', 'type', 'format', 'writeOnly'}}
                      for key, spec in schema.get('properties', {}).items() if isinstance(spec, dict)}
        result['inputs_schema'] = {'type': 'object', 'properties': properties,
                                  'required': schema.get('required', [])}
    return result
