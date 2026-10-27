"""Private credential persistence; callers establish current authority first.

No HTTP or model surface should expose this interface. A future vault adapter
can preserve opaque version references without changing Product DTOs.
"""
from dataclasses import dataclass,field
from typing import Protocol
from .definitions import MCPCard,MCPConfiguration,MCPDefinitionError,RenderedMCP
from ..persistence.models import MCPSecretRevision,CapabilityInstallation


def revision_scope_matches(*, capability_id, requested_scope, revision_scope, installation=None):
    """Match immutable revision ownership, including trusted migration provenance.

    Callers must separately verify revision capability identity and current
    authorization. The origin is server-owned metadata, never request input.
    """
    if revision_scope==requested_scope:
        return True
    if (installation is None or installation.id!=capability_id
            or installation.capability_type!='mcp' or installation.scope_id!=requested_scope):
        return False
    origin=(installation.metadata_ or {}).get('central_library_origin',{})
    return isinstance(origin,dict) and origin.get('scope_id')==revision_scope


@dataclass(frozen=True)
class PrivateMCPDefinition:
    capability_id: str
    scope_id: str
    secret_version: str
    client: MCPConfiguration=field(repr=False)
    values: dict=field(repr=False)
    card: MCPCard=field(repr=False)


class MCPSecretStore(Protocol):
    async def put(self,db,*,capability_id,scope_id,secret_version,rendered,card): ...
    async def get(self,db,*,capability_id,scope_id,secret_version)->PrivateMCPDefinition: ...


class PostgresMCPSecretStore:
    """Matches existing DB credential storage; no encryption claim or new key."""
    async def put(self,db,*,capability_id,scope_id,secret_version,rendered,card):
        db.add(MCPSecretRevision(id=secret_version,capability_id=capability_id,scope_id=scope_id,
            payload={'format_version':1,'client':rendered.client.model_dump(mode='json'),
                     'values':rendered.values,'card':card.model_dump(mode='json')}))
        await db.flush()

    async def get(self,db,*,capability_id,scope_id,secret_version):
        row=await db.get(MCPSecretRevision,secret_version)
        installation=None
        if row is not None and row.scope_id!=scope_id and row.capability_id==capability_id:
            installation=await db.get(CapabilityInstallation,capability_id)
        matching_scope=row is not None and revision_scope_matches(capability_id=capability_id,
            requested_scope=scope_id,revision_scope=row.scope_id,installation=installation)
        if (row is None or row.capability_id!=capability_id or not matching_scope
                or row.payload.get('format_version')!=1):
            raise MCPDefinitionError('MCP credential revision is unavailable',409)
        try:
            return PrivateMCPDefinition(capability_id,scope_id,secret_version,
                MCPConfiguration.model_validate(row.payload['client']),dict(row.payload['values']),
                MCPCard.model_validate(row.payload['card']))
        except Exception:
            raise MCPDefinitionError('MCP credential revision is invalid',409) from None
