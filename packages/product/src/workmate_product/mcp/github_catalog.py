"""Product-owned GitHub MCP Registry catalog; no AgentScope App imports.

Registry mapping adapted from AgentScope 2.0.7, source
src/agentscope/app/hub/_mcp/_github_hub.py (Apache-2.0).
The license is shipped alongside this module as LICENSE.agentscope.
Changes: native DTOs, bounded streaming, lifecycle ownership, encoded card IDs,
and sanitized network/registry errors. Catalog credentials never enter cards.
"""
import json
import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from .definitions import MCPCard, MCPHubPage, MCPDefinitionError

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CARD_ID_LENGTH = 255
MAX_CURSOR_LENGTH = 4096
MAX_QUERY_LENGTH = 2000

DEFAULT_BASE_URL = "https://api.mcp.github.com"

# The registry writes secret placeholders as ``{TOKEN}``; the renderer
# uses ``string.Template``, which wants ``${TOKEN}``.
_PLACEHOLDER = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}")

# How to run a package, by the registry's ``runtime_hint``. Anything not
# listed here cannot be turned into a command, so the entry is skipped.
_RUNTIMES: dict[str, list[str]] = {
    "npx": ["-y"],
    "uvx": [],
    "uv": ["tool", "run"],
    "docker": ["run", "-i", "--rm"],
}


def _substitute(value: Any) -> Any:
    """Rewrite ``{VAR}`` placeholders to the ``${VAR}`` the renderer wants.

    Args:
        value (`Any`):
            A string, list or dict from the registry payload.

    Returns:
        `Any`:
            The same structure with placeholders rewritten.
    """
    if isinstance(value, str):
        return _PLACEHOLDER.sub(r"${\1}", value)
    if isinstance(value, list):
        return [_substitute(item) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item) for key, item in value.items()}
    return value


def _input_property(name: str, spec: dict) -> dict:
    """Convert one registry variable definition to JSON Schema."""
    prop: dict = {
        "type": "string",
        "title": name,
        "description": spec.get("description") or "",
    }
    if spec.get("is_secret"):
        prop["writeOnly"] = True
        prop["format"] = "password"
    if spec.get("choices"):
        prop["enum"] = [str(choice) for choice in spec["choices"]]
    if spec.get("default") is not None:
        prop["default"] = str(spec["default"])
    return prop


def _inputs_schema(inputs: dict[str, dict]) -> dict:
    """Build the install form schema for registry variables."""
    if not inputs:
        return {}

    schema: dict = {
        "type": "object",
        "properties": {
            name: _input_property(name, spec) for name, spec in inputs.items()
        },
    }
    required = sorted(
        name for name, spec in inputs.items() if spec.get("is_required")
    )
    if required:
        schema["required"] = required
    return schema


class GitHubMCPCatalog:
    hub_id = 'github'
    display_name = 'GitHub MCP Registry'
    description = "MCP servers published to GitHub's registry."
    icon_url = 'https://avatars.githubusercontent.com/u/9919?s=64'

    def __init__(self, api_token=None, *, base_url=DEFAULT_BASE_URL, timeout=30.0,
                 transport=None):
        try:
            url=httpx.URL(base_url)
            if (url.scheme not in {'http','https'} or not url.host or url.userinfo
                    or url.query or url.fragment or url.path not in {'','/'}
                    or type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout<=0
                    or (api_token is not None and not isinstance(api_token,str))):
                raise ValueError
        except (TypeError,ValueError,httpx.InvalidURL):
            raise MCPDefinitionError('Invalid MCP registry configuration') from None
        self.base_url=str(url).rstrip('/')
        self.timeout=timeout
        self._api_token=api_token
        self._transport=transport
        self._client=None

    async def __aenter__(self):
        if self._client is not None:
            raise MCPDefinitionError('MCP registry client is already open')
        self._client=httpx.AsyncClient(timeout=self.timeout,follow_redirects=False,transport=self._transport)
        return self

    async def __aexit__(self,*exc):
        if self._client is not None:
            await self._client.aclose()
            self._client=None

    async def _request(self,path,params=None):
        if self._client is None:
            raise MCPDefinitionError('MCP registry lifecycle is not open',503)
        headers={'Accept':'application/json'}
        if self._api_token:
            headers['Authorization']=f'Bearer {self._api_token}'
        try:
            async with self._client.stream('GET',self.base_url+path,params=params,headers=headers) as response:
                if response.status_code!=200:
                    code=response.status_code if 400<=response.status_code<600 else 502
                    raise MCPDefinitionError('MCP registry request failed',code)
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw)+len(chunk)>MAX_RESPONSE_BYTES:
                        raise MCPDefinitionError('MCP registry response exceeds size limit',502)
                    raw.extend(chunk)
            payload=json.loads(raw)
            if not isinstance(payload,dict):
                raise ValueError
            return payload
        except MCPDefinitionError:
            raise
        except (httpx.HTTPError,httpx.InvalidURL,ValueError,UnicodeError,RecursionError):
            raise MCPDefinitionError('MCP registry response is unavailable or invalid',502) from None

    @staticmethod
    def _from_remote(remote: dict) -> tuple[dict, dict]:
        """Build an HTTP config plus its inputs from a ``remotes`` entry.

        Args:
            remote (`dict`):
                One entry of the server's ``remotes`` list.

        Returns:
            `tuple[dict, dict]`:
                The config template and registry definitions of the
                install inputs it references.
        """
        inputs: dict[str, dict] = {}
        headers = {}
        for header in remote.get("headers") or []:
            value = header.get("value") or ""
            # The registry omits the header name when the value already
            # spells out the scheme, which in practice means bearer auth.
            key = header.get("name") or "Authorization"
            headers[key] = _substitute(value)
            for match in _PLACEHOLDER.finditer(value):
                inputs[match.group(1)] = {
                    "description": header.get("description") or "",
                    "is_required": True,
                    "is_secret": header.get("is_secret", True),
                }

        config: dict = {"type": "http_mcp", "url": remote["url"]}
        if headers:
            config["headers"] = headers
        return config, inputs

    @staticmethod
    def _from_package(package: dict) -> tuple[dict, dict] | None:
        """Build a stdio config plus its inputs from a ``packages`` entry.

        Args:
            package (`dict`):
                One entry of the server's ``packages`` list.

        Returns:
            `tuple[dict, dict] | None`:
                The config template and registry definitions of its
                install inputs, or ``None`` when the registry does not
                say how to run the package.
        """
        runtime = package.get("runtime_hint")
        if runtime not in _RUNTIMES:
            return None

        name = package.get("name")
        if not name:
            return None
        version = package.get("version")
        spec = f"{name}@{version}" if version and runtime == "npx" else name

        env = {}
        inputs: dict[str, dict] = {}
        for variable in package.get("environment_variables") or []:
            key = variable.get("name")
            if not key:
                continue

            nested = variable.get("variables")
            value = variable.get("value")
            if isinstance(nested, dict):
                env[key] = str(_substitute(value or ""))
                inputs.update(
                    {
                        input_name: input_spec
                        for input_name, input_spec in nested.items()
                        if isinstance(input_spec, dict)
                    },
                )
            elif value is not None:
                env[key] = str(value)
            else:
                env[key] = f"${{{key}}}"
                inputs[key] = variable

        config: dict = {
            "type": "stdio_mcp",
            "command": runtime,
            "args": [*_RUNTIMES[runtime], spec],
        }
        if env:
            config["env"] = env
        return config, inputs

    def _to_card(self, entry: dict, readme: bool = False) -> MCPCard | None:
        """Build a card from one registry entry.

        Args:
            entry (`dict`):
                One record of the ``servers`` list.
            readme (`bool`, defaults to `False`):
                Whether to carry the README. Left out when listing —
                they run to tens of kilobytes each, so a page of twenty
                would be megabytes of text nothing on screen shows.

        Returns:
            `MCPCard | None`:
                The card, or ``None`` when the entry describes no way to
                reach the server.
        """
        server = entry.get("server") or {}
        github = entry.get("x-github") or {}

        built: tuple[dict, dict] | None = None
        for remote in server.get("remotes") or []:
            if remote.get("url"):
                built = self._from_remote(remote)
                break
        if built is None:
            for package in server.get("packages") or []:
                built = self._from_package(package)
                if built is not None:
                    break
        if built is None:
            return None

        config, inputs = built
        inputs_schema = _inputs_schema(inputs)

        name = server.get("name") or ""
        owner = name.rsplit("/", 1)[0] if "/" in name else None
        repository = server.get("repository") or {}

        # Client names reach the model as ``mcp__{name}__{tool}`` and so
        # must match [a-zA-Z0-9_-]+, while registry names look like
        # ``io.github.upstash/context7``. Keep the last segment.
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.rsplit("/", 1)[-1])

        updated_at = None
        stamp = server.get("updated_at")
        if stamp:
            try:
                updated_at = datetime.fromisoformat(
                    stamp.replace("Z", "+00:00"),
                ).timestamp()
            except ValueError:
                pass

        return MCPCard(
            hub_id=self.hub_id,
            id=server["id"],
            name=slug.strip("-") or "mcp",
            display_name=github.get("display_name") or name,
            description=server.get("description") or "",
            tags=[github["primary_language"]]
            if github.get("primary_language")
            else [],
            version=(server.get("version_detail") or {}).get("version"),
            updated_at=updated_at,
            author=github.get("name_with_owner", "").rsplit("/", 1)[0]
            or owner,
            icon_url=github.get("preferred_image")
            or github.get("owner_avatar_url"),
            url=repository.get("url"),
            # The registry mirrors the repository README on both blocks;
            # they agree, so either will do.
            readme=(repository.get("readme") or github.get("readme"))
            if readme
            else None,
            # STDIO servers must be stateful — the process is the session.
            is_stateful=config["type"] == "stdio_mcp",
            auth="inputs" if inputs else "none",
            inputs_schema=inputs_schema,
            config_template=config,  # type: ignore[arg-type]
        )

    async def list_mcps(self,user_id,q=None,cursor=None,limit=20):
        if (type(limit) is not int or not 1<=limit<=100
                or (q is not None and (not isinstance(q,str) or len(q)>MAX_QUERY_LENGTH))
                or (cursor is not None and (not isinstance(cursor,str) or len(cursor)>MAX_CURSOR_LENGTH))):
            raise MCPDefinitionError('Invalid MCP catalog pagination or query')
        params={'limit':limit}
        if cursor is not None:
            params['cursor']=cursor
        payload=await self._request('/v0/servers',params)
        try:
            entries=payload.get('servers') or []
            metadata=payload.get('metadata') or {}
            if not isinstance(entries,list) or not isinstance(metadata,dict):
                raise ValueError
            cursor=metadata.get('next_cursor')
            if cursor is not None and (not isinstance(cursor,str) or len(cursor)>MAX_CURSOR_LENGTH):
                raise ValueError
            cards=[]
            needle=(q or '').lower()
            for entry in entries:
                card=self._to_card(entry)
                if card is None:
                    continue
                if needle and needle not in f'{card.name} {card.display_name} {card.description}'.lower():
                    continue
                cards.append(card)
            return MCPHubPage(cards=cards,next_cursor=cursor)
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
            raise MCPDefinitionError('MCP registry returned an invalid catalog',502) from None

    async def get_mcp(self,user_id,card_id):
        if (not isinstance(card_id,str) or not 1<=len(card_id)<=MAX_CARD_ID_LENGTH
                or card_id in {'.','..'} or any(ord(c)<32 for c in card_id)):
            raise MCPDefinitionError('Invalid MCP card identity')
        try:
            payload=await self._request('/v0/servers/'+quote(card_id,safe=''))
        except MCPDefinitionError as error:
            if error.status_code==404:
                raise KeyError('MCP card not found') from None
            raise
        try:
            card=self._to_card(payload,readme=True)
            if card is None:
                raise KeyError('MCP card not found')
            if card.id!=card_id:
                raise ValueError
            return card
        except KeyError:
            raise KeyError('MCP card not found') from None
        except (ValueError,TypeError,AttributeError,OverflowError):
            raise MCPDefinitionError('MCP registry returned an invalid card',502) from None
