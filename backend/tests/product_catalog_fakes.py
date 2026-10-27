"""Small Product catalog doubles without AgentScope App execution methods."""
from io import BytesIO
from zipfile import ZipFile
import hashlib


class MemoryBlobStore:
    """Explicit verified Skill storage for HTTP fixtures, without network I/O."""
    def __init__(self):
        self.objects = {}

    async def put_verified(self, digest, raw):
        from workmate_product.skill_storage import key_for_digest
        key = key_for_digest(digest)
        assert hashlib.sha256(raw).hexdigest() == digest
        assert self.objects.setdefault(key, raw) == raw
        return key

    async def get_verified(self, digest, size_bytes):
        from workmate_product.skill_storage import key_for_digest
        from workmate_product.application.skill_artifacts import SkillArtifactError
        raw = self.objects.get(key_for_digest(digest))
        if raw is None or len(raw) != size_bytes or hashlib.sha256(raw).hexdigest() != digest:
            raise SkillArtifactError('Skill object integrity check failed')
        return raw


def skill_archive(name='uploaded-skill', description='Fixture', markdown='Fixture instructions'):
    data = BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('SKILL.md', f'---\nname: {name}\ndescription: {description}\n---\n{markdown}')
    return data.getvalue()


async def seed_skill_artifact(db, installation):
    """Give explicitly seeded Skill fixtures the immutable package native binding requires."""
    from workmate_product.application.skill_repository import record_skill_artifact
    from workmate_product.persistence.models import CapabilitySkillArtifact
    artifact = await record_skill_artifact(db, skill_archive(installation.name))
    db.add(CapabilitySkillArtifact(capability_id=installation.id, artifact_digest=artifact.digest))
    await db.flush()


class CatalogData:
    def __init__(self):
        self.skill_catalog = []
        self.mcp_catalog = []


class SkillCatalog:
    display_name = 'Test Skills'
    description = 'Test catalog'
    icon_url = None

    def __init__(self, data, hub_id):
        self.data, self.hub_id = data, hub_id

    async def list_skills(self, user_id, q=None, cursor=None, limit=40):
        from workmate_product.skills.catalog import SkillCard, page_cards
        return page_cards([SkillCard.model_validate({**card, 'hub_id': self.hub_id})
                           for card in self.data.skill_catalog], q, cursor, limit)

    async def get_skill(self, user_id, card_id, version=None):
        from workmate_product.skills.catalog import SkillCard, SkillCatalogError
        card = next((card for card in self.data.skill_catalog if card['id'] == card_id), None)
        if card is None:
            raise SkillCatalogError(404, 'Skill not found')
        return SkillCard.model_validate({**card, 'hub_id': self.hub_id})

    async def archive(self, user_id, card_id, version=None):
        card = await self.get_skill(user_id, card_id, version)
        return skill_archive(card.name, card.description or 'Fixture', card.markdown or 'Fixture instructions')


class MCPCatalog:
    hub_id = 'github'
    display_name = 'Test MCP'
    description = 'Test catalog'
    icon_url = None

    def __init__(self, data):
        self.data = data

    def _card(self, card):
        from workmate_product.mcp.definitions import MCPCard, HttpMCPConfig
        return MCPCard.model_validate({'hub_id': self.hub_id, 'is_stateful': False,
            'config_template': HttpMCPConfig(url='https://example.invalid/mcp'), **card})

    async def list_mcps(self, user_id, q=None, cursor=None, limit=40):
        from workmate_product.mcp.definitions import MCPHubPage
        return MCPHubPage(cards=[self._card(card) for card in self.data.mcp_catalog[:limit]])

    async def get_mcp(self, user_id, card_id):
        return self._card(next(card for card in self.data.mcp_catalog if card['id'] == card_id))
