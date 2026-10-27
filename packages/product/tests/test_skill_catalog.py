"""Product catalog works without AgentScope App or the runtime source tree."""
from pathlib import Path
import pytest
from workmate_product.skills.catalog import BuiltinCatalog, SkillCatalogError
from workmate_product.application.skill_artifacts import parse_skill_archive

@pytest.mark.asyncio
async def test_builtin_directory_catalog_and_install_payload_match():
    hub = BuiltinCatalog()
    page = await hub.list_skills('user', limit=100)
    assert len(page.cards) >= 15
    assert len({c.id for c in page.cards}) == len(page.cards)
    for card in page.cards:
        artifact = parse_skill_archive(await hub.archive('user', card.id))
        assert artifact.name == card.name
        assert artifact.description == card.description
        assert card.markdown is None
    first = await hub.list_skills('user', limit=1)
    second = await hub.list_skills('user', cursor=first.next_cursor, limit=1)
    assert first.cards[0].id != second.cards[0].id
    assert (await hub.list_skills('user', q='unlikely-search-xyz')).cards == []

@pytest.mark.asyncio
async def test_catalog_unknown_card_and_invalid_cursor_are_explicit():
    hub = BuiltinCatalog()
    with pytest.raises(SkillCatalogError) as missing:
        await hub.archive('user', '../secret')
    assert missing.value.status_code == 404
    with pytest.raises(SkillCatalogError) as invalid:
        await hub.list_skills('user', cursor='invalid')
    assert invalid.value.status_code == 400

@pytest.mark.asyncio
async def test_directory_discovery_is_dynamic_and_excludes_symlinks(tmp_path):
    folder = tmp_path/'new-workflow'
    folder.mkdir()
    (folder/'SKILL.md').write_text('---\nname: new-workflow\ndescription: New workflow\n---\nDo work.')
    (tmp_path/'linked').symlink_to(folder, target_is_directory=True)
    hub = BuiltinCatalog(tmp_path)
    assert [c.id for c in (await hub.list_skills('u')).cards] == ['new-workflow']
