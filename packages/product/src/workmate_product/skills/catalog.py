"""Product-owned catalog contracts and directory-discovered shipped Skills."""
from pathlib import Path
from pydantic import BaseModel, Field
import yaml
from ..application.skill_artifacts import discover_skill_directories, package_skill_directory, parse_skill_archive


class SkillCatalogError(ValueError):
    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class SkillCard(BaseModel):
    hub_id: str
    id: str
    name: str
    description: str = ''
    display_name: str | None = None
    tags: list[str] = Field(default_factory=list)
    version: str | None = None
    updated_at: float | None = None
    author: str | None = None
    icon_url: str | None = None
    installs: int | None = None
    downloads: int | None = None
    url: str | None = None
    markdown: str | None = None
    metadata: dict = Field(default_factory=dict)


class SkillHubPage(BaseModel):
    cards: list[SkillCard]
    next_cursor: str | None = None


def builtin_root() -> Path:
    return Path(__file__).with_name('builtin_skills')


def page_cards(cards, q=None, cursor=None, limit=20):
    try:
        offset = int(cursor or '0')
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise SkillCatalogError(400, 'Invalid catalog pagination') from exc
    if q:
        cards = [c for c in cards if q.casefold() in f'{c.name} {c.description}'.casefold()]
    end = offset + limit
    return SkillHubPage(cards=cards[offset:end], next_cursor=str(end) if end < len(cards) else None)


class BuiltinCatalog:
    hub_id = 'workmate-builtin'
    display_name = 'Workmate 内置技能'
    description = 'Workmate 提供的内置工作流技能。'
    icon_url = None

    def __init__(self, root: Path | None = None):
        self.root = root or builtin_root()

    def _source(self, card_id):
        source = discover_skill_directories(self.root).get(card_id)
        if source is None:
            raise SkillCatalogError(404, 'Skill not found')
        return source

    def _card(self, card_id, *, markdown=False):
        source = self._source(card_id)
        text = (source/'SKILL.md').read_text(encoding='utf-8')
        lines = text.splitlines()
        if not lines or lines[0] != '---' or '---' not in lines[1:]:
            raise SkillCatalogError(422, 'SKILL.md requires YAML frontmatter')
        metadata = yaml.safe_load('\n'.join(lines[1:lines.index('---', 1)]))
        if not isinstance(metadata, dict) or metadata.get('name') != card_id:
            raise SkillCatalogError(422, 'Skill name must match its directory')
        if not isinstance(metadata.get('description'), str) or not metadata['description'].strip():
            raise SkillCatalogError(422, 'Skill description is required')
        return SkillCard(hub_id=self.hub_id, id=card_id, name=card_id,
            display_name=metadata.get('display_name') or card_id,
            description=metadata['description'], tags=metadata.get('tags') or [],
            version=str(metadata['version']) if 'version' in metadata else None,
            markdown=text if markdown else None)

    async def list_skills(self, user_id, q=None, cursor=None, limit=20):
        return page_cards([self._card(name) for name in discover_skill_directories(self.root)], q,cursor,limit)

    async def get_skill(self, user_id, card_id):
        return self._card(card_id, markdown=True)

    async def archive(self, user_id, card_id, version=None):
        card = self._card(card_id)
        if version is not None and version != card.version:
            raise SkillCatalogError(409, 'Requested Skill version is unavailable')
        raw = package_skill_directory(self._source(card_id)).archive
        parse_skill_archive(raw)
        return raw
