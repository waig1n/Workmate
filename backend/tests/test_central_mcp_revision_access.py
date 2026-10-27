"""Historical pinned revisions stay usable only through their installation."""
from types import SimpleNamespace
import pytest
from workmate_product.mcp.secret_store import PostgresMCPSecretStore
from workmate_product.mcp.definitions import MCPDefinitionError
from workmate_product.persistence.models import CapabilityInstallation,MCPSecretRevision

class Database:
    def __init__(self,origin='dept',current='org'):
        self.installation=SimpleNamespace(id='cap',scope_id=current,capability_type='mcp',metadata_={'central_library_origin':{'scope_id':origin}})
        self.revision=SimpleNamespace(id='pinned',capability_id='cap',scope_id='dept',payload={
            'format_version':1,'values':{},'client':{'name':'remote','is_stateful':True,'mcp_config':{'type':'http_mcp','url':'https://example.invalid'}},
            'card':{'hub_id':'fixture','id':'remote','name':'remote','config_template':{'type':'http_mcp','url':'https://example.invalid'}}})
    async def get(self,model,identifier):
        return self.revision if model is MCPSecretRevision else self.installation

async def test_pinned_revision_survives_ownership_migration_without_mutation():
    db=Database()
    revision=await PostgresMCPSecretStore().get(db,capability_id='cap',scope_id='org',secret_version='pinned')
    assert revision.secret_version=='pinned'
    assert db.revision.scope_id=='dept'
    assert revision.scope_id=='org'

@pytest.mark.parametrize('origin,current,capability', [('other','org','cap'),('dept','other','cap'),('dept','org','other-cap')])
async def test_migration_provenance_cannot_authorize_unrelated_revision(origin,current,capability):
    with pytest.raises(MCPDefinitionError):
        await PostgresMCPSecretStore().get(Database(origin,current),capability_id=capability,scope_id='org',secret_version='pinned')
