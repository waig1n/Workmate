"""Private immutable MCP revisions and public installation readiness pointers.

This preserves the existing restricted database storage model. It does not add
application-layer encryption or claim that database administrators cannot read
MCP credentials. Only opaque revision IDs cross into execution descriptors.
"""
from datetime import datetime
from sqlalchemy import CheckConstraint,DateTime,DDL,ForeignKey,String,event,func
from sqlalchemy.orm import Mapped,mapped_column
from .models import Base,_RUNTIME_JSON


class MCPSecretRevision(Base):
    __tablename__='mcp_secret_revisions'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    capability_id: Mapped[str]=mapped_column(String(36))
    scope_id: Mapped[str]=mapped_column(ForeignKey('scopes.id'))
    payload: Mapped[dict]=mapped_column(_RUNTIME_JSON)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())


class CapabilityMCPRevision(Base):
    __tablename__='capability_mcp_revisions'
    capability_id: Mapped[str]=mapped_column(ForeignKey('capability_installations.id',ondelete='CASCADE'),primary_key=True)
    secret_version: Mapped[str]=mapped_column(ForeignKey('mcp_secret_revisions.id'))


for operation in ('UPDATE','DELETE'):
    event.listen(MCPSecretRevision.__table__,'after_create',DDL(
        f'CREATE TRIGGER mcp_secret_revision_no_{operation.lower()} BEFORE {operation} ON mcp_secret_revisions '
        "BEGIN SELECT RAISE(ABORT, 'MCP revisions are immutable'); END"
    ).execute_if(dialect='sqlite'))
event.listen(MCPSecretRevision.__table__,'after_create',DDL(
    "CREATE FUNCTION reject_mcp_secret_revision_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN RAISE EXCEPTION 'MCP revisions are immutable'; END $$"
).execute_if(dialect='postgresql'))
event.listen(MCPSecretRevision.__table__,'after_create',DDL(
    'CREATE TRIGGER mcp_secret_revision_immutable BEFORE UPDATE OR DELETE ON mcp_secret_revisions '
    'FOR EACH ROW EXECUTE FUNCTION reject_mcp_secret_revision_mutation()'
).execute_if(dialect='postgresql'))
event.listen(MCPSecretRevision.__table__,'after_drop',DDL(
    'DROP FUNCTION reject_mcp_secret_revision_mutation()'
).execute_if(dialect='postgresql'))
