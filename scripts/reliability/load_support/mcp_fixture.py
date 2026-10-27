"""Local STDIO MCP protocol fixture; no remote credentials or HTTP service."""
SERVER = '''from mcp.server.fastmcp import FastMCP
from pathlib import Path
import re
server=FastMCP('fixture')
@server.tool()
def generate(key:str,size:int)->str:
    assert re.fullmatch('[0-9a-f]{32}',key) and 0<size<=1048576
    root=Path('/workspace/output');root.mkdir(exist_ok=True)
    counter=root/(key+'.count')
    number=int(counter.read_text())+1 if counter.exists() else 1
    counter.write_text(str(number))
    pattern=key+'\\n'
    (root/(key+'.txt')).write_text((pattern*((size//len(pattern))+1))[:size])
    return 'fixture completed'
server.run(transport='stdio')
'''


async def seed_mcp(sessions):
    from sqlalchemy import select
    from workmate_product.persistence.models import Scope,ScopeMembership,CapabilityInstallation,CapabilityMCPRevision,AgentCapabilityBinding,ResourceShare
    from workmate_product.mcp.definitions import MCPCard,render_mcp
    from workmate_product.mcp.secret_store import PostgresMCPSecretStore
    async with sessions.begin() as db:
        root=await db.scalar(select(Scope).where(Scope.kind=='organization',Scope.parent_id.is_(None)))
        if await db.get(ScopeMembership,{'scope_id':root.id,'user_id':'fixture-user'}) is None:
            db.add(ScopeMembership(scope_id=root.id,user_id='fixture-user'))
        card=MCPCard(hub_id='fixture-fixture',name='fixture',auth='none',config_template={
            'type':'stdio_mcp','command':'python3','args':['-u','-c',SERVER]})
        db.add(CapabilityInstallation(id='fixture-mcp',capability_type='mcp',scope_id=root.id,name='fixture',created_by='fixture-user'))
        await db.flush()
        await PostgresMCPSecretStore().put(db,capability_id='fixture-mcp',scope_id=root.id,
            secret_version='11111111-2222-4333-8444-555555555555',rendered=render_mcp(card,{}),card=card)
        db.add_all([CapabilityMCPRevision(capability_id='fixture-mcp',secret_version='11111111-2222-4333-8444-555555555555'),
            ResourceShare(id='fixture-mcp-share',capability_id='fixture-mcp',target_scope_id=root.id,include_descendants=True),
            AgentCapabilityBinding(agent_id='fixture-agent',capability_installation_id='fixture-mcp')])
