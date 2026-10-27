"""MCP management belongs to organization administrators, not department roles."""
from types import SimpleNamespace
import pytest
from workmate_product.mcp import repository
from workmate_product.mcp.definitions import MCPDefinitionError

class DB:
    async def scalar(self,query):
        return SimpleNamespace(id='org',kind='organization')
    async def get(self,model,identifier):
        return SimpleNamespace(status='active')

@pytest.mark.parametrize('scope',['department','personal'])
async def test_non_organization_install_is_rejected(scope):
    with pytest.raises(MCPDefinitionError,match='organization scope') as error:
        await repository._managed_scope(DB(),'actor',scope)
    assert error.value.status_code==422

async def test_permission_is_checked_at_organization_and_default_scope_resolves(monkeypatch):
    checks=[]
    class Access:
        def __init__(self,db): pass
        async def has_permission(self,actor,scope,permission):
            checks.append((actor,scope,permission))
            return False
        async def accessible_capability_scope_ids(self,actor): return ['org']
    monkeypatch.setattr(repository,'AccessDecisionService',Access)
    with pytest.raises(MCPDefinitionError) as error:
        await repository._managed_scope(DB(),'department-admin',None)
    assert error.value.status_code==403
    assert checks==[('department-admin','org','mcp.manage')]
