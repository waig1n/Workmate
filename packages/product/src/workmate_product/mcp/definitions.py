"""Core-compatible MCP declarations with no runtime imports or secret-bearing errors."""
from dataclasses import dataclass, field
from string import Template
from typing import Literal
import jsonschema
from referencing import Registry
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MCPDefinitionError(ValueError):
    def __init__(self,detail,status_code=400,*,code=None):
        self.detail,self.status_code=detail,status_code
        self.code=code
        super().__init__(detail)


class _Private(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)


class StdioMCPConfig(_Private):
    type: Literal['stdio_mcp']='stdio_mcp'
    command: str=Field(min_length=1,repr=False)
    args: list[str]|None=Field(default=None,repr=False)
    env: dict[str,str]|None=Field(default=None,repr=False)
    cwd: str|None=Field(default=None,repr=False)
    encoding_error_handler: Literal['strict','ignore','replace']='strict'


class HttpMCPConfig(_Private):
    type: Literal['http_mcp']='http_mcp'
    url: str=Field(min_length=1,repr=False)
    headers: dict[str,str]|None=Field(default=None,repr=False)
    timeout: float|None=Field(default=30.0,gt=0)


class MCPConfiguration(_Private):
    name: str=Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    is_stateful: bool
    mcp_config: StdioMCPConfig|HttpMCPConfig=Field(discriminator='type',repr=False)
    enable_tools: list[str]|None=None
    disable_tools: list[str]|None=None
    execution_timeout: float|None=Field(default=None,gt=0)

    @model_validator(mode='after')
    def consistent(self):
        if self.mcp_config.type=='stdio_mcp' and not self.is_stateful:
            raise ValueError('STDIO MCP requires a stateful connection')
        if set(self.enable_tools or []) & set(self.disable_tools or []):
            raise ValueError('Enabled and disabled tools overlap')
        return self


class MCPCard(BaseModel):
    model_config=ConfigDict(hide_input_in_errors=True)
    hub_id: str
    id: str=''
    name: str
    display_name: str|None=None
    description: str=''
    tags: list[str]=Field(default_factory=list)
    version: str|None=None
    updated_at: float|None=None
    author: str|None=None
    icon_url: str|None=None
    url: str|None=None
    readme: str|None=None
    installs: int|None=None
    downloads: int|None=None
    is_stateful: bool=True
    auth: Literal['none','inputs']='inputs'
    inputs_schema: dict=Field(default_factory=dict)
    config_template: StdioMCPConfig|HttpMCPConfig=Field(discriminator='type')

    @model_validator(mode='after')
    def identity(self):
        if not self.id:
            self.id=self.name
        return self


class MCPHubPage(BaseModel):
    cards: list[MCPCard]
    next_cursor: str|None=None


@dataclass(frozen=True)
class RenderedMCP:
    client: MCPConfiguration=field(repr=False)
    values: dict=field(repr=False)


def _substitute(node,values,declared,missing):
    if isinstance(node,str):
        template=Template(node)
        missing.update((set(template.get_identifiers()) & declared)-values.keys())
        return template.safe_substitute(values)
    if isinstance(node,dict):
        return {key:_substitute(value,values,declared,missing) for key,value in node.items()}
    if isinstance(node,list):
        return [_substitute(value,values,declared,missing) for value in node]
    return node


def render_mcp(card:MCPCard,values:dict,*,name=None)->RenderedMCP:
    """Validate and render while keeping write-only answers out of public records.

    Matches current defaults/merged-values/optional-env semantics. JSON Schema
    local references work; no remote schema fetch may occur inside a transaction.
    Undeclared dollar placeholders remain literal, as in the existing renderer.
    """
    try:
        properties=card.inputs_schema.get('properties',{})
        effective={key:prop['default'] for key,prop in properties.items()
                   if isinstance(prop,dict) and 'default' in prop}
        effective.update(values)
        validator=jsonschema.validators.validator_for(card.inputs_schema)
        validator.check_schema(card.inputs_schema)
        validator(card.inputs_schema,registry=Registry()).validate(effective)
        declared=set(properties)
        required=set(card.inputs_schema.get('required',[]))
        config=card.config_template.model_dump(mode='json')
        env=config.get('env')
        if isinstance(env,dict):
            for key,value in list(env.items()):
                missing_optional=(set(Template(value).get_identifiers()) & (declared-required))-effective.keys()
                if missing_optional:
                    del env[key]
        missing=set()
        config=_substitute(config,effective,declared,missing)
        if missing:
            raise ValueError('Unfilled declared inputs')
        client=MCPConfiguration(name=name or card.name,is_stateful=card.is_stateful,mcp_config=config)
        return RenderedMCP(client,effective)
    except Exception as exc:
        # Validator errors can contain the submitted token, URL or env values.
        raise MCPDefinitionError('Invalid MCP inputs or connection declaration') from None
