"""Explicit catalog ownership for the API role composition root."""
from contextlib import AsyncExitStack, asynccontextmanager
from .catalog import BuiltinCatalog
from .skillhub_cn import SkillHubCN


@asynccontextmanager
async def skill_catalogs(*, skillhub_cn_enabled=False, skillhub_cn_base_url='https://api.skillhub.cn',
                         skillhub_cn_request_timeout_secs=30.0, skillhub_cn_max_retries=2):
    """Parameters map existing skillhub_cn settings; no new configuration source."""
    async with AsyncExitStack() as stack:
        catalogs={'workmate-builtin':BuiltinCatalog()}
        if skillhub_cn_enabled:
            hub=await stack.enter_async_context(SkillHubCN(base_url=skillhub_cn_base_url,
                timeout=skillhub_cn_request_timeout_secs,max_retries=skillhub_cn_max_retries))
            catalogs[hub.hub_id]=hub
        yield catalogs
